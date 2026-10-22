# Raspberry Pi 4 B programs

Use the [computer vision launcher scripts](../scripts/vision/README.md) to select
camera preview, capture, NCNN/PyTorch CPU detection, or IMX500 camera AI detection.
The launchers select the Python environment automatically.

For Testudo training images, use the separate [dataset capture program](dataset_capture/README.md).
It supports the same Picamera2/IMX500 camera, desktop preview, and timed collection over SSH.

## Ultrasonic distance sensor on the Pi

For the GUI sensor connected to **ESP32 TX0/GPIO1 and RX0/GPIO3 over I2C**, follow [Ultrasonic sensor in the GUI](../README.md#ultrasonic-sensor-in-the-gui). That section covers the SEN0598 300 cm underwater UART sensor, signal voltage levels, and UART0 programming connections. Upload the updated firmware and copy the updated motor controller script; the GUI displays distance automatically. The direct-Pi HC-SR04 instructions below do not apply to the underwater UART sensor.

`ultrasonic_test.py` reads a four-pin HC-SR04-style sensor directly from the Pi. It needs no ESP32 firmware or serial link. The default BCM GPIO pins do not overlap the six-motor direction GPIOs below.

With power disconnected, wire:

- Sensor VCC → Pi 5V, physical pin 2, for a standard 5V HC-SR04.
- Sensor GND → Pi GND, physical pin 6.
- Sensor TRIG → Pi GPIO23, physical pin 16.
- Sensor ECHO → a 330 Ω resistor → Pi GPIO24, physical pin 18.
- Pi GPIO24 → a 470 Ω resistor → Pi GND. The two resistors reduce the sensor's 5V ECHO signal to about 3V.

The divider is required for a 5V ECHO output; Pi GPIO inputs accept 3.3V. A 3.3V-compatible HC-SR04P can connect ECHO directly. See the [GPIO Zero wiring guide](https://gpiozero.readthedocs.io/en/stable/api_input.html#distancesensor-hc-sr04) and [Pi 4 header pinout](https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-datasheet.pdf).

Copy and run the test on the Pi:

```sh
scp raspberry_pi/ultrasonic_test.py YOUR_USER@PI_ADDRESS:~/ultrasonic_test.py
ssh YOUR_USER@PI_ADDRESS
sudo apt install python3-gpiozero
python3 ~/ultrasonic_test.py
```

The script prints distance in centimeters every 0.5 seconds. Use Ctrl+C to stop or `--once` for one reading. Use `--trigger` and `--echo` with BCM GPIO numbers if you choose different pins. A reading at the 4 m limit is reported as no echo or out of range. Do not connect the sensor's 5V ECHO directly to Pi GPIO.

## Six-motor I2C controller

`motor_control.py` controls all six motors simultaneously. The Pi owns one DIR input per motor; the ESP32 owns six PWM inputs. DIR HIGH selects forward, DIR LOW selects reverse, and zero PWM stops the motor. Upload the matching firmware from the development computer with:

```sh
pio run -e six-motor-i2c -t upload
```

Follow the [complete motor and I2C wiring table](../README.md#pin-map-from-the-supplied-diagrams). The updated diagram assigns DIR GPIOs BCM 26, 19, 13, 6, 5 and 11 to Motors 1-6 respectively (physical header pins 37, 35, 33, 31, 29 and 23). Pi GPIO4, 9, 10, 17, 22 and 27 are no longer claimed for motor direction. Copy the updated Pi script when switching drivers; the existing ESP32 firmware works unchanged.

### Copy and configure

From the project directory on your development computer:

```sh
scp raspberry_pi/motor_control.py raspberry_pi/sen0140.py YOUR_USER@PI_ADDRESS:~/
ssh YOUR_USER@PI_ADDRESS
```

On the Pi:

```sh
sudo apt update
sudo apt install -y python3-gpiozero python3-lgpio python3-smbus2
sudo usermod -aG i2c,gpio "$USER"
sudo raspi-config
```

Enable **Interface Options > I2C**. Disable SPI: direction GPIO11 uses a SPI0 pin. Remove other overlays using any of the six motor direction GPIOs. Reboot after changing interface configuration/group membership:

```sh
sudo reboot
```

Reconnect with SSH, then run:

```sh
python3 ~/motor_control.py
```

The default device is `/dev/i2c-1`, with ESP32 address `0x32`. `--bus` selects another bus; `--address 0x32` selects a different address only if you also change `I2C_ADDRESS` in the firmware. The Pi SDA pin is GPIO2 (physical 3), connected to ESP32 GPIO23; SCL is Pi GPIO3 (physical 5), connected to ESP32 GPIO22. All grounds must be shared and I2C pull-ups must be at 3.3 V.

This controller replaces the earlier UART script. UART presets/options are no longer used. GPIO16/17 on the ESP32 are now PWM outputs, so disconnect the old UART2 wiring before using this firmware.

### GPIO startup errors

The controller detects the header GPIO chip by a known label or by verifying all six motor line names against their BCM offsets, and opens it explicitly. It handles labels returned as bytes or with padding, and recognizes device aliases reporting the same kernel chip name. It uses BCM numbering, even if the header controller appears as `/dev/gpiochip1` or `/dev/gpiochip4`. If discovery cannot select a single accessible header chip, it prints each chip's metadata and stops before claiming any pins.

For `lgpio.error: 'unexpected error'`, run local diagnostics:

```sh
python3 ~/motor_control.py --diagnose
```

This prints the Pi model, kernel, chip names/labels/line counts, and the reason each chip was accepted or rejected. Once a chip is selected it also prints the owners of the six motor GPIOs. It does not claim pins, change pin modes, or communicate with the ESP32. Device files existing does not identify the header; an eight-line chip cannot cover the motor offsets through GPIO26.

If you have identified the header chip, select it explicitly. For example, use `python3 ~/motor_control.py --diagnose --gpiochip 4` to inspect chip 4 and `python3 ~/motor_control.py --gpiochip 4 --gui` to run on it. Explicit selection bypasses the exact-label requirement, but still checks device access, line count, and conflicting line names. An unknown-label chip with unnamed lines can be selected explicitly only when you have confirmed it is the header controller. The chip number in these examples must match your Pi; do not try arbitrary chip numbers to bypass an error.

DIR GPIO11 overlaps SPI0. GPIO4 is no longer used for motor direction. Disable conflicting interfaces/overlays in `raspi-config` or the boot configuration, close other motor/GPIO programs, then reboot after interface changes. If a chip cannot be opened due to permissions, run `sudo usermod -aG gpio,i2c "$USER"`, then log out and back in. Keep system `python3-gpiozero` and `python3-lgpio` packages current; if using a virtual environment, ensure it has access to the intended packages.

If a pin is claimed but has no consumer label, the diagnostic prints `consumer=<not supplied by kernel>` instead of implying the owner is known. It searches readable `/proc` GPIO descriptor metadata for process IDs/names, shows relevant settings from the main boot config files, and reads current pin functions with `pinctrl get` when available. Older kernels may omit descriptor metadata, kernel drivers have no owning process PID, and permissions may prevent inspecting other users. `sudo python3 ~/motor_control.py --diagnose` is a read-only way to inspect those processes. The diagnostic exits with status 1 when motor pins are claimed; normal startup also checks all six GPIOs and stops before configuring any direction outputs if a conflict is found.

On Pi 4, the old UART presets can explain this exact set of overlaps: UART3 uses GPIO4/5, UART4 uses GPIO8/9, and UART5 uses GPIO12/13. Check for `dtoverlay=uart3`, `dtoverlay=uart4`, and `dtoverlay=uart5` left from UART testing, including any included config files. If these interfaces are no longer needed, comment out their overlay lines in `/boot/firmware/config.txt` (or `/boot/config.txt` on older systems) and reboot. Keep I2C enabled for the ESP32. SPI and 1-Wire are other possible conflicts; the GPIO numbers alone do not identify the owner. The [official overlay reference](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README) documents these mappings.

A `KeyError` at `pin = self.pins[info]` is normally a handled cache miss: GPIO Zero then constructs the pin. A subsequent error in `gpio_claim_input` can occur even for a `DigitalOutputDevice`, because the lgpio backend initially claims pins as inputs. The last exception is the failure to investigate. The controller now reports the motor number, BCM GPIO and chip when initialization fails, while preserving that original exception through cleanup. See [GPIO Zero pin cache](https://gpiozero.readthedocs.io/en/stable/_modules/gpiozero/pins/pi.html) and [lgpio backend](https://gpiozero.readthedocs.io/en/stable/_modules/gpiozero/pins/lgpio.html).

### Optional desktop GUI

The same script includes a small GUI. On the Pi desktop, install Tk and add `--gui`:

```sh
sudo apt install python3-tk
python3 ~/motor_control.py --gui
```

Use the six sliders or number boxes to select signed speeds, then click **Apply six speeds**. The **All motors** control and **Copy to six** button set all six fields together; click Apply to send them. **Last applied** shows the controller's last acknowledged speeds. **STOP ALL** (or Escape) clears the fields and requests zero speed for all motors. If a request is already running, Stop runs immediately after it completes. Closing the window stops the motors and releases the GPIOs.

The GUI keeps responding during I2C transactions and reversal pauses, and shows link failures while disabling further motion controls. Sliders only edit the pending speeds; they do not start motors until Apply is clicked. Bus/address options still work with `--gui`. Run it on the Pi's desktop or through SSH with X forwarding; an ordinary headless SSH session can use the default terminal mode. Only run one controller at a time.

#### Camera detection in the motor GUI

Use the updated `motor_control.py`, `camera_control.py` and `sen0140.py` from the
same checkout. Keep the full repository on the Pi so the trained model and vision
environments can be found. From the repo root, run:

```sh
sudo apt install python3-tk python3-gpiozero python3-lgpio python3-smbus2
python3 raspberry_pi/motor_control.py --gui
```

The **Testudo camera** tab starts detection automatically, with a live preview,
boxes, confidence and the active inference backend. **Motors and sensors** retains
the motor, ultrasonic and IMU controls. **STOP ALL MOTORS** stays visible on both
tabs. Camera detection does not command motors. **Start camera** and **Stop camera**
operate independently of motor controls; closing the GUI stops both.

Automatic selection uses the camera's AI processor when
`testudo_training/runs/testudo_imx500/weights/best_imx_model/` contains
`packerOut.zip` and `labels.txt`. Otherwise it runs the trained
`testudo_training/runs/testudo_imx500/weights/best.pt` on the **Pi CPU**. This is the
same trained YOLO11n model; a `.pt` file cannot run directly on the camera chip.
The GUI shows which backend is active. No conversion or retraining runs at GUI
startup. To require camera-side inference and report a missing export:

```sh
python3 raspberry_pi/motor_control.py --gui --camera-backend imx500
```

To use the checkpoint directly, including when it is in another directory:

```sh
python3 raspberry_pi/motor_control.py --gui \
  --camera-model testudo_training/runs/testudo_imx500/weights/best.pt
```

`--camera-model` also accepts a complete IMX500 or NCNN export directory. Use
`--camera-backend ncnn` to select the existing `best_ncnn_model` export beside
`best.pt`. The NCNN input size comes from its metadata; PyTorch uses the training
size of 640. `--camera-conf 0.4` adjusts confidence and `--camera-fps 16` requests
camera FPS; actual processing speed needs measurement on the Pi.

The camera worker automatically selects `.venv-imx500` for camera AI or
`.venv-pi4`/`.venv` for CPU detection. PyTorch prefers `.venv-pi4-cpu` when its
`vision-ready.txt` validation marker exists. `--camera-python /path/to/bin/python`
overrides this choice. If no matching repo environment exists, it uses the GUI's
Python interpreter. Camera/native libraries are imported only in that separate
process; a model-load failure, missing camera or native crash is reported in the
camera tab without stopping the motor GUI. A stale preview is cleared after three
seconds without frames. Motor shutdown completes before waiting for camera cleanup.

For initial CPU setup, use [the Pi CPU setup instructions](../scripts/vision/README.md#raspberry-pi-cpu-detection).
For camera AI dependencies and conversion, use [the IMX500 workflow](../testudo_training/IMX500.md).
Only one program can own the camera; close standalone camera previews before
starting this GUI. Use `--no-camera` for a motor-only GUI with no camera imports
or processes. If you run a copied script outside the repo, copy `camera_control.py`
alongside it and specify both `--camera-model` and `--camera-python` explicitly.
Camera loading and simultaneous motor/camera operation still require Pi hardware
testing; local tests use simulated devices.

The GUI shows **Ultrasonic 1**, **Ultrasonic 2**, and **Ultrasonic 3** using the same I2C heartbeat, including when motors are stopped. Sensor 1 uses ESP32 RX0/GPIO3 and TX0/GPIO1; sensor 2 uses ESP32 RX/GPIO21 and TX/GPIO19; sensor 3 uses ESP32 RX/GPIO25 and TX/GPIO26. Connect each sensor TX to its ESP32 RX, and sensor RX to ESP32 TX, with the voltage translation described in the main wiring guide. Each reading independently shows centimeters, no underwater echo/outside 2-300 cm, no valid UART response, or stale readings. An I2C link failure marks all three unavailable and disables the motor directions. In manual mode, sensor readings do not change motor speeds. Update both the firmware and Pi script for three readings; older firmware leaves unsupported sensors unavailable.

#### Automatic depth control

Set the limits in the **Automatic depth control** panel, then check **Enable automatic control**. It starts disabled each time the GUI opens. Each ultrasonic sensor controls two motors independently:

| Sensor | Motors |
| --- | --- |
| Ultrasonic 1 | 1 and 2 |
| Ultrasonic 2 | 3 and 4 |
| Ultrasonic 3 | 5 and 6 |

Each pair has editable **Min (cm)**, **Max (cm)**, and **Max speed (%)** fields. Minimum speed is always **0%**. For a valid reading at or below the minimum distance, the pair uses its maximum speed; at or above the maximum distance, it stops. Between those limits, speed decreases linearly: `speed = max_speed * clamp((max_distance - distance_cm) / (max_distance - min_distance), 0, 1)`, rounded to an integer percentage.

Defaults remain **3 cm**, **10 cm**, and **100%**: 3 cm gives 100%, 6.5 cm gives 50%, and 10 cm gives 0%. Distance limits can differ for each sensor, accept decimal centimeters within 2-300 cm, and must have minimum less than maximum. Maximum speed is an integer from -100 to 100; a negative value reverses the pair, and zero disables it. Edit settings with automatic mode disabled, then enable it to apply them. Invalid settings leave automatic mode disabled and display an error.

Missing sensors, waiting responses, no echo, invalid UART responses, out-of-range readings, or telemetry older than one second command **zero** for that pair. Fresh valid readings resume automatic control. The other pairs continue responding to their own sensors. Control runs in the background I2C heartbeat about every 0.2 seconds, with the existing reversal coast interval preserved. An I2C interruption stops all motors while the controller retries the connection. Automatic control resumes only from fresh sensor telemetry after recovery; exhausted retries latch a fault and require restarting the program.

Enabling automatic mode first stops any manually running motors. Manual speed editing is disabled while automatic control is active; **Last applied** continues showing actual commanded speeds. Uncheck the option or press **STOP ALL** / Escape to leave automatic mode and stop all six motors. Closing the GUI also stops all motors. Copy the updated `motor_control.py` to the Pi to use this mode; existing three-sensor firmware needs no changes.

#### Troubleshooting applied speed without rotation

**Last applied** reports the acknowledged PWM command, which does not measure motor rotation. The **Output feedback** line also shows acknowledgement age and cached readback of the actual Pi direction GPIOs: `1` selects forward and `0` selects reverse; zero acknowledged PWM means stopped. DIR alone cannot indicate a stop. A direction mismatch or acknowledgements older than one second produces a status warning; the ESP32 watchdog may have disabled PWM after a stalled worker. Unexpected background GPIO/IMU exceptions now latch a visible fault and attempt an all-motor stop instead of silently ending the heartbeat.

For a trace in the terminal, run the updated script with:

```sh
python3 ~/motor_control.py --gui --trace-motors
```

Enable automatic mode with a valid reading at or below that pair's minimum distance (3 cm by default). With the default maximum speeds, the affected pair should report `100` in `applied`, `1` for each motor's `GPIO DIR`, and a recent `PWM ack age`. The trace prints on state changes and about once per second while the worker runs. Compare it with a manual command at the same percentage and direction, and include any reconnect or worker error messages when diagnosing a failure. Use `--no-imu` alongside these options to isolate a suspected IMU polling stall.

With the default limits, the 3-10 cm curve still uses the full 0-100% PWM range. If a motor requires 52% to start, for example, a 50% automatic command may leave it stopped; that does not explain a motor staying stopped at 100%. Minimum speed remains zero; this mode does not add a starting pulse.

#### All six motors work together, but subsets do not

This pattern can indicate that PWM enable wires and Pi DIR pins belong to different motor channels. When all six receive equal speeds, an ordering mismatch is hidden. When only a pair is commanded, its direction inputs may be active while its actual enable inputs receive zero. Shared or accidentally connected enable lines are another possibility. A valid I2C acknowledgement and Pi GPIO readback cannot establish the connections at the H-bridge inputs.

To distinguish these cases in manual mode, run all six at the same working speed, then set only Motor 1 to zero and Apply. If two physical motors stop, the direction and PWM assignments likely refer to different motors. If all six stop, check for connected/shared enable lines. With power disconnected, trace each driver's PWM input to the ESP32 wire for that motor's DIR pin. Each PWM input needs its own ESP32 signal; keep the shared ground connection.

The Pi script supports `--pwm-map C1,C2,C3,C4,C5,C6` to correct a confirmed ordering mismatch without changing firmware. Entries identify the ESP32 PWM channel used by each Pi motor, in Motor 1-6 direction order. Channel numbers in this option are **1-based**:

| Channel | ESP32 GPIO |
| --- | --- |
| 1 | 4 |
| 2 | 13 |
| 3 | 12 |
| 4 | 16 |
| 5 | 17 |
| 6 | 18 |

The confirmed default is **`4,5,6,1,2,3`**, which connects logical Pi motors 1-6 to ESP32 GPIOs **16,17,18,4,13,12** respectively. Run normally without an override:

```sh
python3 ~/motor_control.py --gui
```

Use `--pwm-map` only to override this default for different wiring; every channel must appear exactly once. The mapping applies to manual control, automatic pairs, heartbeat, reversals, and stop commands. Status acknowledgement is checked against the mapped PWM frame. The GUI still uses Pi motor order. A map changes channel assignment; it cannot correct enable wires that are electrically connected to each other.

### SEN0140 V2 IMU readings

The GUI automatically reads the SEN0140 V2 on the same I2C bus as the ESP32. Copy `sen0140.py` alongside `motor_control.py`, then run with `--gui`. No firmware update is needed. Acceleration, gyro rotation rate, magnetic raw counts, barometer temperature/pressure, and acceleration-based roll/pitch appear below the ultrasonic readings. Each missing component reports its own error and retries, and stale values are cleared from the display. Use `--gui --no-imu` to disable it. See the [IMU wiring and component addresses](../README.md#sen0140-v2-imu-on-the-shared-i2c-bus). Keep the board dry; the barometer does not measure underwater depth.

### Terminal commands

```text
m 60                  all six forward at 60%
m -60                 all six reverse at 60%
m 1 60                Motor1 at 60%, preserving other motors
m 60 -60 40 40 0 75    independent signed speeds for Motors 1-6
s                     stop all motors
help                  show commands
q                     stop and quit
```

Speeds are integers from -100 to 100. The controller acknowledges zero PWM before claiming DIR at startup or switching direction, and waits a 0.5-second coast interval for a reversal. Zero PWM stops the motor; the driver determines whether that coasts or brakes. Unchanged motors continue running.

Startup requires an acknowledged all-zero PWM command. A background thread refreshes the six-channel command every 0.2 seconds even while the prompt waits. Quit, Ctrl+C, EOF, SIGTERM and SSH hangup attempt to stop and release GPIO resources.

### I2C retries and recovery

An I2C read/write error or applied-status timeout holds all DIR outputs steady and clears cached ultrasonic readings. The controller then makes up to **five recovery attempts**, sending only all-zero PWM commands, with delays of **0.1, 0.2, 0.4, 0.5, and 0.5 seconds** before the attempts. Each command still requires a matching acknowledgement within 0.3 seconds. DIR is lowered only after zero PWM is acknowledged, and the reversal coast interval starts from that acknowledgement. Startup and shutdown zero commands use the same retry path. Invalid user input is not retried.

The GUI stays responsive and shows **I2C reconnecting** while motion controls are disabled. A successful acknowledged zero command restores the link without restarting the app and rearms an ESP32 that rebooted or tripped its one-second watchdog. Interrupted manual commands are discarded: all motors stay stopped until you click Apply again. Automatic mode remains enabled and resumes on a later heartbeat only when fresh valid sensor telemetry supports motion. A pending STOP exits automatic mode and takes priority over further motion commands.

If all five recovery attempts fail, the Pi latches a fault, makes a final best-effort zero command, and refuses further motion until the program is restarted. If that command also fails, it holds DIR for 1.1 seconds without further writes, allowing the ESP32's one-second watchdog to disable PWM before lowering or releasing DIR. LOW DIR is reverse, so lowering it cannot serve as an emergency stop.

Only one process should own the motor outputs. For control from a vision/navigation program, import `I2CPwm` and `MotorController`; call `set_speeds` with six signed percentages, or `set_motor` with a motor number (1-6) and speed. Always call `close()` in `finally` before closing the I2C bus.

See the [project README](../README.md) for the frame protocol, hardware notes, legacy firmware targets, and validation commands.
