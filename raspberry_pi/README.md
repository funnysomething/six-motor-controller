# Raspberry Pi 4 B programs

## Ultrasonic distance sensor on the Pi

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

`motor_control.py` controls all six motors simultaneously. The Pi owns L298N direction inputs; the ESP32 owns six enable/PWM inputs. Upload the matching firmware from the development computer with:

```sh
pio run -e six-motor-i2c -t upload
```

Follow the [complete motor and I2C wiring table](../README.md#pin-map-from-the-supplied-diagrams). The Pi diagram duplicates Motor6 labels; the code assumes module 3 IN1/IN2 is Motor5 and IN3/IN4 is Motor6. Edit the last two `DIRECTION_PINS` pairs if your wiring differs.

### Copy and configure

From the project directory on your development computer:

```sh
scp raspberry_pi/motor_control.py YOUR_USER@PI_ADDRESS:~/motor_control.py
ssh YOUR_USER@PI_ADDRESS
```

On the Pi:

```sh
sudo apt update
sudo apt install -y python3-gpiozero python3-lgpio python3-smbus2
sudo usermod -aG i2c,gpio "$USER"
sudo raspi-config
```

Enable **Interface Options > I2C**. Disable SPI: direction GPIO9/10/11 use the SPI0 pins. Remove other overlays using any of the twelve motor direction GPIOs. Reboot after changing interface configuration/group membership:

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

The controller detects the header GPIO chip by its label and opens it explicitly. It uses BCM numbering, even if the header controller appears as `/dev/gpiochip1` or `/dev/gpiochip4`. If discovery finds no accessible header chip or finds more than one, it stops before claiming any pins.

For `lgpio.error: 'unexpected error'`, run local diagnostics:

```sh
python3 ~/motor_control.py --diagnose
```

This prints the Pi model, kernel, available GPIO chips, and owners of the twelve motor GPIOs. It does not claim pins, change pin modes, or communicate with the ESP32. If needed, select the header chip explicitly, for example `python3 ~/motor_control.py --gpiochip 1 --gui` when diagnostics identifies `gpiochip1` as the header. Do not select a different chip merely to bypass an error.

GPIO9/10/11 overlap SPI0, and GPIO4 overlaps the common 1-Wire overlay. Disable conflicting interfaces/overlays in `raspi-config` or the boot configuration, close other motor/GPIO programs, then reboot after interface changes. If a chip cannot be opened due to permissions, run `sudo usermod -aG gpio,i2c "$USER"`, then log out and back in. Keep system `python3-gpiozero` and `python3-lgpio` packages current; if using a virtual environment, ensure it has access to the intended packages.

A `KeyError` at `pin = self.pins[info]` is normally a handled cache miss: GPIO Zero then constructs the pin. A subsequent error in `gpio_claim_input` can occur even for a `DigitalOutputDevice`, because the lgpio backend initially claims pins as inputs. The last exception is the failure to investigate. The controller now reports the motor number, BCM GPIO and chip when initialization fails, while preserving that original exception through cleanup. See [GPIO Zero pin cache](https://gpiozero.readthedocs.io/en/stable/_modules/gpiozero/pins/pi.html) and [lgpio backend](https://gpiozero.readthedocs.io/en/stable/_modules/gpiozero/pins/lgpio.html).

### Optional desktop GUI

The same script includes a small GUI. On the Pi desktop, install Tk and add `--gui`:

```sh
sudo apt install python3-tk
python3 ~/motor_control.py --gui
```

Use the six sliders or number boxes to select signed speeds, then click **Apply six speeds**. The **All motors** control and **Copy to six** button set all six fields together; click Apply to send them. **Last applied** shows the controller's last acknowledged speeds. **STOP ALL** (or Escape) clears the fields and requests zero speed for all motors. If a request is already running, Stop runs immediately after it completes. Closing the window stops the motors and releases the GPIOs.

The GUI keeps responding during I2C transactions and reversal pauses, and shows link failures while disabling further motion controls. Sliders only edit the pending speeds; they do not start motors until Apply is clicked. Bus/address options still work with `--gui`. Run it on the Pi's desktop or through SSH with X forwarding; an ordinary headless SSH session can use the default terminal mode. Only run one controller at a time.

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

Speeds are integers from -100 to 100. The controller automatically disables the affected PWM channels before switching direction and waits a 0.5-second coast interval for a reversal. Unchanged motors continue running.

Startup requires an acknowledged all-zero PWM command. A background thread refreshes the six-channel command every 0.2 seconds even while the prompt waits. If communication fails, the Pi lowers all twelve direction outputs, attempts an all-zero command, and refuses further motion until the program is restarted. The ESP32's one-second watchdog independently disables PWM and requires a zero command before rearming. Quit, Ctrl+C, EOF, SIGTERM and SSH hangup attempt to stop and release GPIO resources.

Only one process should own the motor outputs. For control from a vision/navigation program, import `I2CPwm` and `MotorController`; call `set_speeds` with six signed percentages, or `set_motor` with a motor number (1-6) and speed. Always call `close()` in `finally` before closing the I2C bus.

See the [project README](../README.md) for the frame protocol, hardware notes, legacy firmware targets, and validation commands.
