#include <Arduino.h>
#include <Wire.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

namespace {
// GPIO numbers from the ESP32 screenshot, ordered Motor1 through Motor6.
constexpr uint8_t PWM_PINS[] = {4, 13, 12, 16, 17, 18};
constexpr size_t MOTOR_COUNT = sizeof(PWM_PINS);
constexpr uint8_t I2C_ADDRESS = 0x32, I2C_SDA = 23, I2C_SCL = 22;
constexpr uint32_t WATCHDOG_MS = 1000;
constexpr size_t COMMAND_SIZE = 13, STATUS_SIZE = 14;
constexpr uint8_t VERSION = 1, COMMAND_MAGIC = 0xA5, STATUS_MAGIC = 0x5A;
constexpr uint8_t NEEDS_STOP = 1, TIMED_OUT = 2;

SemaphoreHandle_t stateMutex;
uint8_t percentages[MOTOR_COUNT] = {};
uint8_t sequence[4] = {};
uint8_t flags = NEEDS_STOP;
uint32_t lastCommandAt = 0;

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
  xSemaphoreGive(stateMutex);
  reply[13] = checksum(reply, 13);
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
  Serial.begin(115200);
  for (size_t motor = 0; motor < MOTOR_COUNT; ++motor) {
    pinMode(PWM_PINS[motor], OUTPUT);
    digitalWrite(PWM_PINS[motor], LOW);
    ledcSetup(motor, 1000, 8);
    ledcAttachPin(PWM_PINS[motor], motor);
    ledcWrite(motor, 0);
  }
  stateMutex = xSemaphoreCreateMutex();
  if (!stateMutex) {
    Serial.println("State mutex failed; PWM remains disabled.");
    while (true) delay(1000);
  }
  Wire.onReceive(receiveCommand);
  Wire.onRequest(requestStatus);
  if (!Wire.begin(I2C_ADDRESS, I2C_SDA, I2C_SCL, 100000)) {
    Serial.println("I2C slave initialization failed; PWM remains disabled.");
    while (true) delay(1000);
  }
  // Classic ESP32 needs an initial slave TX buffer. The Pi polls past stale
  // replies until sequence, percentages and flags match the applied command.
  uint8_t reply[STATUS_SIZE];
  makeStatus(reply);
  Wire.slaveWrite(reply, sizeof(reply));
  Serial.println("Six-motor I2C PWM ready at 0x32. Direction is controlled by Pi.");
}

void loop() {
  bool expired = false;
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  if (flags == 0 && millis() - lastCommandAt >= WATCHDOG_MS) {
    const uint8_t stopped[MOTOR_COUNT] = {};
    applyPwm(stopped);
    flags = NEEDS_STOP | TIMED_OUT;
    expired = true;
  }
  xSemaphoreGive(stateMutex);
  if (expired) Serial.println("I2C watchdog: all PWM disabled; send all-zero to rearm.");
  delay(1);
}
