# Six motors with Raspberry Pi direction and ESP32 PWM

The default `six-motor-i2c` firmware controls six brushed DC motors through three L298N modules. The Pi sets twelve direction GPIO outputs and sends all six PWM percentages to the ESP32 in one I2C command. Each motor has its own 1 kHz, 8-bit PWM channel, so all six can run together at independent speeds and directions.

## Pin map from the supplied diagrams

Pi numbers in the code are **BCM GPIO numbers**, not physical header positions. ESP32 numbers are GPIO numbers, not J2/J3 connector positions.

| Motor | L298N module/channel | ESP32 PWM GPIO | Pi forward input GPIO (physical pin) | Pi reverse input GPIO (physical pin) |
| --- | --- | --- | --- | --- |
| 1 | Module 1, A: ENA / IN1 / IN2 | 4 | 26 (37) | 19 (35) |
| 2 | Module 1, B: ENB / IN3 / IN4 | 13 | 13 (33) | 6 (31) |
| 3 | Module 2, A: ENA / IN1 / IN2 | 12 | 5 (29) | 11 (23) |
| 4 | Module 2, B: ENB / IN3 / IN4 | 16 | 9 (21) | 10 (19) |
| 5 | Module 3, A: ENA / IN1 / IN2 | 17 | 22 (15) | 27 (13) |
| 6 | Module 3, B: ENB / IN3 / IN4 | 18 | 17 (11) | 4 (7) |

**Diagram assumption:** all four direction inputs on module 3 are labeled Motor6 in the Pi screenshot. This implementation assigns its IN1/IN2 pair to Motor5 and IN3/IN4 to Motor6, consistent with the other two modules. If wired oppositely, swap the last two pairs in `DIRECTION_PINS` in `raspberry_pi/motor_control.py`.

Connect Pi GPIO2 SDA1 (physical pin 3) to **ESP32 GPIO23 SDA**, and Pi GPIO3 SCL1 (physical pin 5) to **ESP32 GPIO22 SCL**. These ESP32 SDA/SCL assignments follow the screenshot and differ from the old accelerometer program. Connect Pi GND, ESP32 GND, all three L298N grounds, and motor-supply negative together. The Pi is I2C master; the ESP32 is slave at **7-bit address 0x32**, using bus 1 at 100 kHz. I2C pull-ups must go to 3.3 V.

Remove **both ENA and ENB jumpers on each of the three modules** so the ESP32 can control the enable inputs. Connect channel A motors to OUT1/OUT2 and channel B motors to OUT3/OUT4. Add a 10 kohm pull-down on each ENA/ENB input to keep the motors disabled during ESP32 reset. Use an external motor supply and follow the module's instructions for its separate 5 V logic/regulator jumper. GPIO signals are 3.3 V; do not connect motor power or the L298N 5 V terminal to a GPIO/3V3 rail.

The direction map occupies Pi SPI0 pins GPIO9/10/11; disable SPI and any overlays claiming those pins. The existing ultrasonic sensor's GPIO23/24 defaults do not overlap this motor map. The six-motor runtime uses ESP32 GPIO16/17 for PWM, so do not leave the old UART2 wiring connected there. It does not initialize the accelerometer or accept USB/UART motor commands. The diagram's other serial labels are unused.

## Build and run

Upload from the development computer:

```sh
pio run -e six-motor-i2c
pio run -e six-motor-i2c -t upload
```

`pio run` also builds this environment by default. On the Pi, enable I2C with `sudo raspi-config` (Interface Options > I2C), disable SPI if enabled, and reboot after configuration changes. Install and run:

```sh
sudo apt install python3-gpiozero python3-smbus2
python3 raspberry_pi/motor_control.py
```

See [Pi setup and commands](raspberry_pi/README.md#six-motor-i2c-controller) for copying the script and access permissions.

For the optional desktop GUI, install `python3-tk` on the Pi and run `python3 raspberry_pi/motor_control.py --gui`. It has six signed-speed sliders, a group speed control, Apply, Stop All, and applied-speed/link status. See [GUI instructions](raspberry_pi/README.md#optional-desktop-gui). The terminal controller remains the default.

```text
m 60                  # all six at +60%
m 60 -60 40 40 0 75    # Motors 1-6, each with its own signed speed
m 2 -50               # change only Motor2; preserve the other five
s                     # stop all six
q                     # stop and quit
```

Values are integer percentages from -100 to 100. Positive raises the forward input; negative raises the reverse input; zero lowers both direction inputs and disables PWM. Actual forward rotation depends on motor wiring. All six channels run concurrently; PWM register and GPIO updates occur sequentially, not at exactly the same clock edge.

## Reversal and communication behavior

Before changing a motor's direction, the Pi requests zero PWM for the affected channels and waits for matching applied-status acknowledgement. It then lowers those direction inputs, waits the remaining part of a 0.5-second coast interval if reversing, selects the new direction, and sends the new six-channel PWM frame. Unaffected motors retain their settings. A 0.5-second interval is not a measurement of mechanical stopping time.

The Pi refreshes PWM every 0.2 seconds while waiting for commands. The ESP32 starts disabled and disables all PWM if no valid command arrives for one second. After reset or timeout it rejects nonzero commands until receiving an all-zero frame. An I2C failure latches a Pi-side fault, lowers all direction outputs, and attempts to disable PWM; restart the controller to resume. Quit, Ctrl+C, EOF, SIGTERM and SSH hangup attempt a stop and release the GPIOs. A stop coasts rather than brakes.

Only run one motor controller at a time. Use the `MotorController.set_speeds((m1, m2, m3, m4, m5, m6))` API for coordinated control from another Pi program, and call `close()` in `finally` when finished.

## I2C protocol

The Pi uses raw `smbus2.i2c_rdwr` write and read operations with STOP between them, not SMBus register/block commands. Every command carries all six unsigned PWM magnitudes; direction is handled exclusively by Pi GPIO.

| Frame | Bytes in order |
| --- | --- |
| Command, 13 bytes | `0xA5`, version `1`, 32-bit little-endian sequence, six percentages `0..100` in Motor1-6 order, XOR of the preceding 12 bytes |
| Status, 14 bytes | `0x5A`, version `1`, applied sequence, flags, six applied percentages, XOR of the preceding 13 bytes |

Flag bit 0 means an all-zero command is required; bit 1 means watchdog timeout. Invalid size, version, checksum, or percentage rejects the entire command without refreshing the watchdog. Status is published after all six PWM writes. The Pi polls until sequence, flags and magnitudes match, handling the classic ESP32's possibly stale first response. Acknowledgement confirms applied PWM settings, not physical motion.

## Validation and sources

Run the host controller tests with `python -m unittest discover -s test -p "test_*.py"`. Firmware compilation and mocked controller tests do not replace testing the I2C timing, GPIO wiring and motors on the actual Pi/ESP32.

- [Espressif Arduino I2C slave API](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/i2c.html)
- [Raspberry Pi GPIO documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)
- [smbus2 raw I2C API](https://smbus2.readthedocs.io/en/latest/)
- [GPIO Zero digital outputs](https://gpiozero.readthedocs.io/en/stable/api_output.html)
- [ST L298 datasheet](https://www.st.com/resource/en/datasheet/l298.pdf)

---

The original single-motor USB/IMU and UART firmware remain available through their explicitly selected environments below. Their pin maps and wiring differ from the six-motor runtime.

## Legacy single-motor programs

Controls one brushed DC motor through channel A of an L298N module and prints LIS2DH/LIS2DH12 acceleration over USB serial. The sensor does not control the motor. Uses the existing NodeMCU-32S target for a classic ESP32. GPIO assignments may need adjustment for a different development board.

## Wiring

Disconnect power before wiring. Use GPIO numbers printed on the board, not header positions.

- ESP32 GPIO25 to L298N ENA. Remove the ENA jumper so PWM controls this input. Add a 10 kΩ resistor from ENA to GND to keep the motor disabled during reset.
- ESP32 GPIO26 to L298N IN1.
- ESP32 GPIO27 to L298N IN2.
- Motor wires to L298N OUT1 and OUT2.
- External motor supply positive to the L298N motor supply terminal, often marked +12V or VS. Choose voltage and current for your motor and module, allowing for driver voltage drop and motor stall current. Do not power the motor from the ESP32.
- Motor supply negative, L298N GND, ESP32 GND and accelerometer GND must connect together.
- ESP32 3V3 to accelerometer VCC.
- ESP32 GPIO22 to accelerometer SDA.
- ESP32 GPIO23 to accelerometer SCL.

Power the ESP32 by USB. The L298N also needs its logic supply: follow your module's instructions for the separate 5V regulator jumper. That jumper is different from ENA. Do not connect the L298N 5V terminal to ESP32 3V3 or a USB-powered ESP32 power rail. Keep I²C pull-ups at 3.3V. If the sensor exposes CS, hold CS high for I²C; hold SA0 low for address 0x18 or high for 0x19. Breakout boards often handle these connections already. INT pins are unused.

This assumes a complete L298N module with flyback diodes, not a bare L298N chip. Secure the motor before testing. The stop command lets it coast; it is not a mechanical brake. A 0.5-second reversal pause does not guarantee that every motor has stopped spinning.

## Build and use

1. Build with `pio run -e nodemcu-32s`.
2. Connect the ESP32 by USB, then use `pio run -e nodemcu-32s -t upload`.
3. Open the serial monitor at 115200 baud with newline line endings. With the CLI, use `pio device monitor -e nodemcu-32s`. Press reset to see startup messages if needed.
4. Send `m 60` to run forward at 60%. Send `s` to stop. Send `m -60` to run in reverse at 60%. Send `m 0` to stop as well.

Motor commands accept integer percentages from -100 to 100. Positive values run forward, negative values run in reverse, and zero stops. Direction names depend on motor wiring. Small percentages may not start the motor. The motor starts stopped and keeps its last setting until it receives `s` or `m 0`, the ESP32 resets, or power is removed. On a direction change, the firmware stops first; wait at least 0.5 seconds and resend the command to reverse.

Acceleration prints five times per second as `ax`, `ay`, and `az` in g, alongside the current signed motor percentage. The sensor runs at 100 Hz in high-resolution ±2g mode. Readings include gravity, so a stationary, level sensor should show about ±1g on the vertical axis and near zero on the other two. These are acceleration readings, not speed or position.

The firmware probes I²C addresses 0x18 and 0x19 and checks the device identity. If the sensor is absent or a read fails, it prints an error. Motor commands remain available independently. After correcting wiring with power off and reconnecting, reset or send `retry` to stop the motor and initialize the sensor again. Send `help` to show commands.

The V2.0 breakout manufacturer is unconfirmed. The register interface follows the LIS2DH/LIS2DH12 family; verify the module's pin labels before connecting it. No external sensor library is required.

## Motor-only program

`src/motor_only.cpp` runs the same motor controls without initializing I²C or reading the IMU. It uses ENA GPIO25, IN1 GPIO26 and IN2 GPIO27 for the same single motor on channel A. Serial commands and the reversal pause are unchanged.

Select **motor-only** in PlatformIO's Project Tasks to build or upload it, or run:

```sh
pio run -e motor-only
pio run -e motor-only -t upload
pio device monitor -e motor-only
```

At 115200 baud, send `m 60`, `m -60`, or `s` followed by Enter. The motor starts stopped. No accelerometer is required. The motion dashboard has no acceleration data in this mode.

The original program remains in `src/main.cpp`. Use `pio run -e nodemcu-32s -t upload` to restore motor plus IMU operation. Each environment compiles only its own source file, avoiding duplicate `setup()` and `loop()` definitions. These are legacy environments; the default environment is now `six-motor-i2c`.

## UART motor commands

The motor-only program also accepts commands over UART2 at **115200 baud, 8 data bits, no parity, 1 stop bit**, without flow control. Connect a 3.3V UART controller or USB-to-TTL serial adapter:

- Controller TX → ESP32 GPIO16 RX.
- Controller RX → ESP32 GPIO17 TX.
- Controller GND → ESP32 GND, shared with L298N GND and motor supply negative.

Use 3.3V signal levels, not 5V TTL or RS-232 voltages. With the ESP32 powered through USB, leave the adapter's power pin disconnected. Motor pins remain ENA GPIO25, IN1 GPIO26 and IN2 GPIO27. UART goes to the ESP32, not directly to the L298N.

Upload the `motor-only` environment again after this change. Send `help\n`, `m 60\n`, `m -60\n`, or `s\n`; `\n` means an actual newline byte. Replies return on the same serial connection that received the command. USB serial remains available with its own command buffer. Both inputs control the same motor, and the latest valid command wins. Only one controller should send motor commands at a time. Send `s`, wait at least 0.5 seconds, then send the opposite direction to reverse.

The external UART uses a separate adapter port on your computer. Open that adapter at 115200 baud for UART2, or the ESP32's onboard USB serial port for the original USB commands. Each port can be opened by only one monitor at a time.

## Live motion dashboard

Run `python3 -m http.server 8765 --bind 127.0.0.1 --directory tools` from this project, then open http://127.0.0.1:8765/motion.html in desktop Chrome or Edge. Close the PlatformIO serial monitor first so the browser can own the port. Click **Connect ESP32** and select the USB serial device. The dashboard uses the existing firmware output, so no firmware change or upload is needed.

The viewer shows a 20-second XYZ acceleration plot, vector magnitude, and a gravity-direction indicator. It reads serial data only and sends no motor commands. Tilt estimates are useful when the sensor is nearly stationary; acceleration alone cannot track position or yaw. A disconnected or stalled feed is marked explicitly. Click Disconnect before reopening the PlatformIO serial monitor.

## References

- [ST LIS2DH12 datasheet](https://www.st.com/resource/en/datasheet/lis2dh12.pdf)
- [DFRobot LIS2DH module documentation](https://wiki.dfrobot.com/sen0224)
- [ST L298 datasheet](https://www.st.com/resource/en/datasheet/l298.pdf)

Compilation verifies the firmware target. Motor operation, sensor readings and wiring require testing on the actual hardware.
