#include <Arduino.h>
#include <Wire.h>
#include "underwater_uart.h"
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

namespace {
// Firmware PWM channel order; the Pi maps logical motors to these channels.
constexpr uint8_t PWM_PINS[] = {4, 13, 12, 16, 17, 18};
constexpr size_t MOTOR_COUNT = sizeof(PWM_PINS);
constexpr uint8_t I2C_ADDRESS = 0x32, I2C_SDA = 23, I2C_SCL = 22;
constexpr uint32_t WATCHDOG_MS = 1000;
constexpr size_t COMMAND_SIZE = 13, STATUS_SIZE = 29;
constexpr uint8_t ULTRASONIC_TX = 1, ULTRASONIC_RX = 3; // TX0, RX0.
constexpr uint8_t ULTRASONIC_2_TX = 19, ULTRASONIC_2_RX = 21;
constexpr uint8_t ULTRASONIC_3_TX = 26, ULTRASONIC_3_RX = 25;
constexpr size_t SENSOR_COUNT = 3;
// Rotate pings to reduce interference: each sensor is requested every 300 ms.
constexpr uint32_t ULTRASONIC_SLOT_MS = 100;
HardwareSerial *const sensorPorts[] = {&Serial, &Serial2, &Serial1};
UnderwaterUartSensor sensors[SENSOR_COUNT];
constexpr uint8_t VERSION = 1, COMMAND_MAGIC = 0xA5, STATUS_MAGIC = 0x5A;
constexpr uint8_t NEEDS_STOP = 1, TIMED_OUT = 2;

SemaphoreHandle_t stateMutex;
uint8_t percentages[MOTOR_COUNT] = {};
uint8_t sequence[4] = {};
uint8_t flags = NEEDS_STOP;
uint32_t lastCommandAt = 0;
uint16_t distanceMm[SENSOR_COUNT] = {};
// 0 = waiting, 1 = valid, 2 = no underwater echo/range, 3 = no valid UART reply.
uint8_t ultrasonicState[SENSOR_COUNT] = {};

uint8_t checksum(const uint8_t *data, size_t count) {
  uint8_t result = 0;
  for (size_t i = 0; i < count; ++i) result ^= data[i];
  return result;
}

// Caller holds stateMutex. Each motor has its own continuously running PWM.
void applyPwm(const uint8_t *values) {
  for (size_t motor = 0; motor < MOTOR_COUNT; ++motor) {
    ledcWrite(motor, (values[motor] * 255U + 50U) / 100U);
    percentages[motor] = values[motor];
  }
}

void makeStatus(uint8_t *reply) {
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  reply[0] = STATUS_MAGIC;
  reply[1] = VERSION;
  memcpy(reply + 2, sequence, sizeof(sequence));
  reply[6] = flags;
  memcpy(reply + 7, percentages, MOTOR_COUNT);
  reply[14] = 0xD1; // Optional distance extension; keep the v1 motor prefix.
  reply[15] = ultrasonicState[0];
  reply[16] = distanceMm[0] & 0xFF;
  reply[17] = distanceMm[0] >> 8;
  reply[19] = 0xD2;
  reply[20] = ultrasonicState[1];
  reply[21] = distanceMm[1] & 0xFF;
  reply[22] = distanceMm[1] >> 8;
  reply[24] = 0xD3;
  reply[25] = ultrasonicState[2];
  reply[26] = distanceMm[2] & 0xFF;
  reply[27] = distanceMm[2] >> 8;
  xSemaphoreGive(stateMutex);
  reply[13] = checksum(reply, 13);
  reply[18] = checksum(reply, 18);
  reply[23] = checksum(reply + 19, 4); // Independently checked sensor 2 block.
  reply[28] = checksum(reply + 24, 4); // Independently checked sensor 3 block.
}

void publishUltrasonic(size_t sensor) {
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  distanceMm[sensor] = sensors[sensor].distance;
  ultrasonicState[sensor] = sensors[sensor].state;
  xSemaphoreGive(stateMutex);
}

void sampleUltrasonic() {
  static uint32_t requestedAt = 0;
  static size_t nextSensor = 0;
  static bool started = false;
  const uint32_t now = millis();
  // The sensor needs up to 500 ms after power-on before accepting requests.
  if (!started) {
    if (now < 500) return;
    started = true;
  }
  for (size_t sensor = 0; sensor < SENSOR_COUNT; ++sensor) {
    if (sensors[sensor].poll(*sensorPorts[sensor], now)) publishUltrasonic(sensor);
  }
  if (now - requestedAt >= ULTRASONIC_SLOT_MS) {
    sensors[nextSensor].request(*sensorPorts[nextSensor], now);
    nextSensor = (nextSensor + 1) % SENSOR_COUNT;
    requestedAt = now;
  }
}

void receiveCommand(int count) {
  uint8_t frame[COMMAND_SIZE];
  size_t received = 0;
  while (Wire.available()) {
    const uint8_t value = Wire.read();
    if (received < COMMAND_SIZE) frame[received] = value;
    ++received;
  }
  if (count != COMMAND_SIZE || received != COMMAND_SIZE ||
      frame[0] != COMMAND_MAGIC || frame[1] != VERSION ||
      frame[12] != checksum(frame, 12)) return;
  bool allStopped = true;
  for (size_t motor = 0; motor < MOTOR_COUNT; ++motor) {
    if (frame[6 + motor] > 100) return;
    if (frame[6 + motor] != 0) allStopped = false;
  }
  // Wire callbacks run in the I2C task, not an ISR. Serialize with watchdog.
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  // Reset/link loss requires an acknowledged all-zero command before rearming.
  if (flags != 0 && !allStopped) {
    xSemaphoreGive(stateMutex);
    return;
  }
  applyPwm(frame + 6);
  memcpy(sequence, frame + 2, sizeof(sequence));
  lastCommandAt = millis();
  flags = 0;
  xSemaphoreGive(stateMutex);
}

void requestStatus() {
  uint8_t reply[STATUS_SIZE];
  makeStatus(reply);
  Wire.write(reply, sizeof(reply));
}
} // namespace

void setup() {
  // UART0 belongs exclusively to the underwater sensor, with no debug logs.
  Serial.begin(115200, SERIAL_8N1, ULTRASONIC_RX, ULTRASONIC_TX);
  // Explicit mapping avoids Serial2's default GPIO16/17 motor PWM pins.
  Serial2.begin(115200, SERIAL_8N1, ULTRASONIC_2_RX, ULTRASONIC_2_TX);
  // Remap UART1 away from the classic ESP32's flash-connected default pins.
  Serial1.begin(115200, SERIAL_8N1, ULTRASONIC_3_RX, ULTRASONIC_3_TX);
  for (size_t motor = 0; motor < MOTOR_COUNT; ++motor) {
    pinMode(PWM_PINS[motor], OUTPUT);
    digitalWrite(PWM_PINS[motor], LOW);
    ledcSetup(motor, 1000, 8);
    ledcAttachPin(PWM_PINS[motor], motor);
    ledcWrite(motor, 0);
  }
  stateMutex = xSemaphoreCreateMutex();
  if (!stateMutex) {
    while (true) delay(1000);
  }
  Wire.onReceive(receiveCommand);
  Wire.onRequest(requestStatus);
  if (!Wire.begin(I2C_ADDRESS, I2C_SDA, I2C_SCL, 100000)) {
    while (true) delay(1000);
  }
  // Classic ESP32 needs an initial slave TX buffer. The Pi polls past stale
  // replies until sequence, percentages and flags match the applied command.
  uint8_t reply[STATUS_SIZE];
  makeStatus(reply);
  Wire.slaveWrite(reply, sizeof(reply));
}

void loop() {
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  if (flags == 0 && millis() - lastCommandAt >= WATCHDOG_MS) {
    const uint8_t stopped[MOTOR_COUNT] = {};
    applyPwm(stopped);
    flags = NEEDS_STOP | TIMED_OUT;
  }
  xSemaphoreGive(stateMutex);
  sampleUltrasonic();
  delay(1);
}
