# Six motors with Raspberry Pi direction and ESP32 PWM

For camera preview, dataset capture, training, model export, and detection, use
the [computer vision launcher scripts](scripts/vision/README.md).

The default `six-motor-i2c` firmware controls six brushed DC motors through PWM+DIR H-bridges. The Pi sets one direction GPIO per motor and sends all six PWM percentages to the ESP32 in one I2C command. Each motor has its own 1 kHz, 8-bit PWM channel, so all six can run together at independent speeds and directions. The new interface assumes DIR HIGH selects forward, DIR LOW selects reverse, and zero PWM stops the motor.

## Pin map from the supplied diagrams

Pi numbers in the code are **BCM GPIO numbers**, not physical header positions. ESP32 numbers are GPIO numbers, not J2/J3 connector positions.

| Motor | ESP32 PWM GPIO | Pi DIR GPIO (physical pin) |
| --- | --- | --- |
| 1 | 16 | 26 (37) |
| 2 | 17 | 19 (35) |
| 3 | 18 | 13 (33) |
| 4 | 4 | 6 (31) |
| 5 | 13 | 5 (29) |
| 6 | 12 | 11 (23) |

The table uses the confirmed wiring. The Pi defaults to PWM channel map **4,5,6,1,2,3** for logical Motors 1-6; firmware channels 1-6 remain ESP32 GPIOs 4,13,12,16,17,18. No command-line override or firmware change is needed for this mapping.

The DIR assignments follow the updated Pi diagram: Motors 1-6 connect to physical header pins 37, 35, 33, 31, 29 and 23 respectively. `DIRECTION_PINS` in `raspberry_pi/motor_control.py` lists their BCM GPIO numbers in motor order. Pi GPIO4, 9, 10, 17, 22 and 27 are no longer claimed for motor direction. Copy the updated Pi script when switching drivers; the ESP32 firmware and PWM mapping need no changes.

Connect Pi GPIO2 SDA1 (physical pin 3) to **ESP32 GPIO23 SDA**, and Pi GPIO3 SCL1 (physical pin 5) to **ESP32 GPIO22 SCL**. These ESP32 SDA/SCL assignments follow the screenshot and differ from the old accelerometer program. Connect Pi GND, ESP32 GND, all H-bridge grounds, and motor-supply negative together. The Pi is I2C master; the ESP32 is slave at **7-bit address 0x32**, using bus 1 at 100 kHz. I2C pull-ups must go to 3.3 V.

Connect each driver's PWM input to its ESP32 GPIO and DIR input to its Pi GPIO in the table. Use the new driver's specified motor outputs, power connections and any enable/sleep configuration. Keep PWM low during reset, using a 10 kohm pull-down if appropriate for the driver. Use an external motor supply and 3.3 V-compatible control inputs; follow the new module's voltage/current requirements. A LOW DIR signal selects reverse and does not disable the bridge.

The direction map occupies Pi SPI0 pin GPIO11; disable SPI and any overlays claiming that pin. The six-motor runtime uses ESP32 GPIO16/17 for PWM, so do not leave the old UART2 wiring connected there. It does not initialize the accelerometer or accept USB/UART motor commands. TX0/GPIO1 and RX0/GPIO3 now read the ultrasonic sensor as described below.

## Ultrasonic sensor in the GUI

The DFRobot SEN0598 IP68 underwater UART sensor connects to the ESP32; its readings travel to the Pi over the existing I2C link. The firmware supports the 300 cm model, with a 2-300 cm range. The 6 m SEN0599 uses the same UART format but has different range limits. Wire with power off:

- Red VCC wire to a regulated 5 V supply and black GND wire to shared ground. The sensor's specified supply range is 5-24 V; that is its power input, not an allowable ESP32 GPIO voltage.
- Yellow sensor RX wire to ESP32 TX0/GPIO1 through any required UART level translation.
- White sensor TX wire to ESP32 RX0/GPIO3 through any required UART level translation. UART is crossed TX-to-RX, not TX-to-TX.

The manufacturer's linked specifications do not state the UART logic voltage. Verify it before connecting: ESP32 GPIO accepts 3.3 V signals. For a 5 V UART, use a suitable 5 V/3.3 V UART level translator on both signal paths. The old HC-SR04 TRIG/ECHO wiring instructions do not apply. A resistor divider on an already 3.3 V sensor TX can make its signal too low to receive.

For a second sensor of the same model, connect its white TX wire to **ESP32 GPIO21/RX** and its yellow RX wire to **ESP32 GPIO19/TX**, with the same supply, shared ground, and appropriate signal level translation. This uses UART2 explicitly mapped to GPIO21/19. Sensor 1 remains on UART0 TX0/GPIO1 and RX0/GPIO3. For sensor 3, connect its white TX wire to **ESP32 GPIO25/RX** and its yellow RX wire to **ESP32 GPIO26/TX**, using the same supply, shared ground and signal level translation. UART1 is explicitly remapped to GPIO25/26. These are ESP32 pin numbers, not Pi GPIO numbers.

TX0/RX0 are UART0 pins. Serial debug logging is disabled, but ESP32 boot messages may still appear on TX0. On NodeMCU boards RX0 also connects to the onboard USB-to-serial transmitter: isolate that connection before letting the sensor drive RX0. Disconnect sensor 1 TX/RX for firmware uploads and restore the programming connection if isolated. Closing the serial monitor does not electrically disconnect the USB-to-serial chip. GPIO16/17 remain motor PWM outputs and cannot be used for either sensor. See the [Espressif GPIO reference](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/gpio.html).

Upload the updated `six-motor-i2c` firmware and copy the updated `raspberry_pi/motor_control.py` to the Pi. Run `python3 ~/motor_control.py --gui`. Three labeled readings, **Ultrasonic 1**, **Ultrasonic 2**, and **Ultrasonic 3**, appear automatically in centimeters, including while motors are stopped. Each sensor has independent UART timeout, range, and stale-data status; one disconnected sensor does not prevent readings from the others. Test with the sensors underwater, aimed at submerged obstacles within 2-300 cm; the manufacturer says readings in air are zero. The separate Pi GPIO test script is only for HC-SR04 sensors and cannot read these UART sensors.

After a 500 ms startup wait, the ESP32 sends `0x55` at 115200 baud, 8N1, cycling through all three sensors every 100 ms. Each sensor is requested every 300 ms to reduce interference between their pings. It parses `0xFF`, distance high byte, distance low byte, and the low byte of their sum. Distances are already millimeters, so no sound-speed conversion is needed. Replies are read without blocking the motor watchdog, with a 60 ms timeout and checksum validation for each UART. The Pi receives all three readings through its 200 ms motor heartbeat. The display distinguishes no underwater echo/outside 2-300 cm, no valid UART response, stale data, and a failed I2C link. This display does not automatically stop motors near obstacles.

Manufacturer references: [SEN0598 specifications and wire colors](https://wiki.dfrobot.com/sen0598/), [UART protocol](https://wiki.dfrobot.com/sen0598/docs/18120), [request command and underwater testing](https://wiki.dfrobot.com/sen0598/docs/18121).

## SEN0140 V2 IMU on the shared I2C bus

The Pi GUI reads the DFRobot SEN0140 V2 directly as additional I2C devices alongside the ESP32 at `0x32`. Connect IMU SDA to the existing SDA line, IMU SCL to SCL, and share ground. Use a 3.3 V-compatible supply and keep all bus pull-ups at 3.3 V. The SEN0140 board must stay dry, for example inside the vehicle enclosure; its BMP280 is an air barometer, not an underwater depth sensor.

Copy both `raspberry_pi/motor_control.py` and `raspberry_pi/sen0140.py` into the same directory on the Pi, then run `python3 ~/motor_control.py --gui`. No ESP32 firmware update is needed for the IMU. The GUI adds XYZ acceleration in g, XYZ angular rate in degrees/second, XYZ magnetic raw counts, BMP280 temperature in Celsius and pressure in hPa, and acceleration-based roll/pitch. Tilt is useful when nearly stationary. Magnetic readings are uncalibrated chip-axis counts; this interface does not claim fused yaw, velocity or position.

The driver detects ADXL345 at `0x53` or `0x1D`, ITG3200/3205 at `0x68` or `0x69`, VCM5883L at `0x0C` with QMC5883L/HMC5883L alternatives at `0x0D`/`0x1E`, and BMP280 at `0x76` or `0x77`. Each component is identified/configured independently. The older V1 BMP085 barometer is not supported. Missing components show unavailable and retry; readings without updates become stale after three seconds. Missing IMU components do not latch a motor fault. A failure of the shared bus that also prevents ESP32 acknowledgement still stops the motors.

The controller reads one IMU component after each 200 ms motor heartbeat, so each component normally refreshes about every 0.8 seconds after initialization. Motor commands and IMU access share the controller lock and worker; Tk only displays cached data. Use `--gui --no-imu` to disable IMU polling. Standalone terminal motor control does not initialize the IMU.

References: [DFRobot SEN0140 V2](https://wiki.dfrobot.com/sen0140), [DFRobot magnetometer driver](https://github.com/DFRobot/DFRobot_QMC5883), [ADXL345 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/adxl345.pdf), and [BMP280 compensation and specifications](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmp280-ds001.pdf).

## Build and run

Upload from the development computer:

```sh
pio run -e six-motor-i2c
pio run -e six-motor-i2c -t upload
```

`pio run` also builds this environment by default. On the Pi, enable I2C with `sudo raspi-config` (Interface Options > I2C), disable SPI if enabled, and reboot after configuration changes. Install and run:

```sh
sudo apt install python3-gpiozero python3-lgpio python3-smbus2
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

Values are integer percentages from -100 to 100. Positive sets DIR HIGH; negative sets DIR LOW; zero disables PWM and sets DIR LOW after acknowledgement. Actual forward rotation depends on motor wiring. All six channels run concurrently; PWM register and GPIO updates occur sequentially, not at exactly the same clock edge.

## Reversal and communication behavior

Before changing a motor's direction, the Pi requests zero PWM for the affected channels and waits for matching applied-status acknowledgement. It then sets DIR LOW with PWM disabled, waits the remaining part of a 0.5-second coast interval if reversing, selects the new direction, and sends the new six-channel PWM frame. Unaffected motors retain their settings. A 0.5-second interval is not a measurement of mechanical stopping time.

The Pi refreshes PWM every 0.2 seconds while waiting for commands. The ESP32 starts disabled and disables all PWM if no valid command arrives for one second. After reset or timeout it rejects nonzero commands until receiving an all-zero frame. Startup acknowledges zero PWM before configuring any DIR outputs. On an I2C error or acknowledgement timeout, the Pi holds DIR steady and makes up to five recovery attempts using only all-zero frames. It lowers DIR only after zero PWM is acknowledged. If the final fault-stop command also fails, it holds DIR for 1.1 seconds without further writes so the ESP32 watchdog can disable PWM before DIR is lowered or released. Recovery starts a new reversal coast interval after the zero acknowledgement. A successful recovery keeps the app running: manual commands must be applied again, while automatic depth control resumes from fresh sensor telemetry. Only exhausted retries latch a fault requiring a controller restart. See [I2C retries and recovery](raspberry_pi/README.md#i2c-retries-and-recovery). Quit, Ctrl+C, EOF, SIGTERM and SSH hangup attempt a stop and release the GPIOs. The driver determines whether zero PWM coasts or brakes; DIR alone cannot stop a motor.

Only run one motor controller at a time. Use the `MotorController.set_speeds((m1, m2, m3, m4, m5, m6))` API for coordinated control from another Pi program, and call `close()` in `finally` when finished.

## I2C protocol

The Pi uses raw `smbus2.i2c_rdwr` write and read operations with STOP between them, not SMBus register/block commands. Every command carries all six unsigned PWM magnitudes; direction is handled exclusively by Pi GPIO.

| Frame | Bytes in order |
| --- | --- |
| Command, 13 bytes | `0xA5`, version `1`, 32-bit little-endian sequence, six percentages `0..100` in ESP32 PWM-channel order, XOR of the preceding 12 bytes |
| Status, 14 bytes | `0x5A`, version `1`, applied sequence, flags, six applied percentages, XOR of the preceding 13 bytes |

PWM-channel order on the wire is ESP32 GPIOs 4,13,12,16,17,18. The Pi reorders logical motor commands using its default map 4,5,6,1,2,3 and validates acknowledgements against those reordered values. GUI and terminal motor numbers stay in the direction-pin order shown above.

The updated firmware appends a five-byte ultrasonic extension to status, giving 19 bytes total. Byte 14 is `0xD1`; byte 15 is sensor state `0` waiting, `1` valid, `2` no underwater echo/outside 2-300 cm, or `3` no valid UART reply before timeout. Bytes 16-17 hold unsigned little-endian distance in millimeters, zero when invalid. Byte 18 is the XOR of bytes 0-17. The original motor prefix and its checksum are unchanged. The Pi validates the motor acknowledgement before caching the extension, and a missing or corrupt extension cannot acknowledge an invalid motor frame. Older firmware can still acknowledge motors; the GUI reports sensor firmware unavailable. Cached readings expire after one second without valid telemetry.

The original two-sensor status is **24 bytes**. Bytes 0-18 keep the existing motor and sensor 1 format. Byte 19 is `0xD2`, byte 20 is sensor 2 state using the same codes, bytes 21-22 are its little-endian distance in millimeters, and byte 23 is the XOR of bytes 19-22. The two sensor blocks are checked independently, so corruption in one does not refresh its cached reading or discard the other. The GUI can still display sensor 1 with the older single-sensor firmware; sensor 2 reports unavailable until the firmware is updated.

Flag bit 0 means an all-zero command is required; bit 1 means watchdog timeout. Invalid size, version, checksum, or percentage rejects the entire command without refreshing the watchdog. Status is published after all six PWM writes. The Pi polls until sequence, flags and magnitudes match, handling the classic ESP32's possibly stale first response. Acknowledgement confirms applied PWM settings, not physical motion.

The three-sensor status is **29 bytes**, preserving bytes 0-23. Byte 24 is `0xD3`, byte 25 is sensor 3 state, bytes 26-27 are its little-endian distance in millimeters, and byte 28 is the XOR of bytes 24-27. Sensor 3 is checked independently and reports unavailable when using older one- or two-sensor firmware. Update the ESP32 firmware and Pi GUI script to receive all three readings.

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
