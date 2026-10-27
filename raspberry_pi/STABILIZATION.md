# BlueROV1 clearance and attitude control

The six motors use the original BlueROV1 layout, with Motor 6 underneath the
vehicle. Motor labels match the supplied top-view diagram. This system holds
height above a locally horizontal bottom, level roll/pitch, and a captured magnetic
heading when the compass is trustworthy. It does not estimate general XYZ position
or navigate autonomously. The other two ultrasonic sensors remain telemetry-only.

## Install and start

Keep the **whole repository** on the Pi. `motor_control.py` now imports the control
transport, runtime and stabilization modules from the same directory; copying only
the old standalone script is insufficient. Use Python 3.10 or newer.

```sh
sudo apt install python3-venv python3-tk python3-lgpio python3-gpiozero python3-smbus2
python3 -m venv --system-site-packages .venv-control
.venv-control/bin/python -m pip install -r raspberry_pi/requirements-control.txt
cp raspberry_pi/vehicle.example.json vehicle.local.json
.venv-control/bin/python raspberry_pi/motor_control.py --gui --no-camera \
  --vehicle-config vehicle.local.json --control-log control.jsonl
```

The example is intentionally **unverified**, so manual operation and recordings
work but automatic control refuses to start. Manual six-motor commands bypass the
automatic PWM ceiling and retain the full -100 to 100 range.

Build/upload `six-motor-i2c` firmware from this branch before automatic operation:

```sh
pio run -e six-motor-i2c
pio run -e six-motor-i2c -t upload --upload-port /dev/ttyUSB0
```

Replace the port with the actual device. PlatformIO otherwise auto-detects the
port; use an explicit override when more than one board is connected. No upload
occurs during tests. Status-v1 firmware remains usable
for manual control with the updated Pi application; automatic control requires
status v2. Old Pi applications must be upgraded together with this firmware.

## Vehicle setup and calibration

Body axes are **X forward, Y right, Z down**. Positive roll lowers the right side,
positive pitch raises the nose, positive yaw turns right. Upward effort increases
bottom clearance. GUI angles and gyro rates are degrees and degrees/second.
Configuration offsets and internal clearance are metres; GUI clearance is cm.

Before setting `verified: true` in your local configuration:

1. Identify all six motors with individual manual commands. Verify diagram labels
   and PWM wiring. `--pwm-map 4,5,6,1,2,3` remains the default electrical map.
2. Set `motor_polarity` so a positive geometric command pushes Motors 1–2 forward
   and Motors 3–5 upward. Motor 6 is manual-only in this stage.
3. Select the single downward-facing `depth_sensor`, numbered 1–3. Set
   `sensor_offset_m` from the control reference point to its transducer, in body
   axes. The beam must point along body +Z. The other sensors do not command motors.
4. Determine each chip's `body_rotation`. These are proper orthonormal 3x3
   rotations, applied after bias/sensitivity calibration. Verify all three axes
   physically; different chips need not have matching board-axis conventions.
   At rest and level, calibrated acceleration must read approximately `(0,0,-1)` g.
5. Collect the separate recordings below with automatic control disabled. Stop
   motors for calibration. Keep the IMU dry inside a suitable enclosure.
6. Apply the calibration results, verify level roll/pitch, and set the small
   `level_roll_deg` and `level_pitch_deg` trim offsets if required.
7. Verify the actual motor/H-bridge reversal behavior and measured motor response.
   Keep `reversal_seconds: 0.5` initially. Do not shorten it based on software tests.
8. Mark `verified` true only after completing these checks, then restart the app.

Record separate JSONL files using `--control-log`:

- **Gyro:** at least five seconds stationary, preferably after temperature settles.
- **Accelerometer:** hold all six faces stationary for several seconds each.
- **Magnetometer:** slowly rotate the fully assembled vehicle through varied 3-D
  orientations, with motors stopped and away from environmental magnetic material.

Fit these recordings into a **new** configuration file:

```sh
.venv-control/bin/python -m raspberry_pi.calibrate_control \
  --config vehicle.local.json --output vehicle.calibrated.json \
  --gyro-log stationary.jsonl --accel-log six-faces.jsonl --mag-log rotations.jsonl
```

The tool fits gyro bias, diagonal accelerometer bias/scale, and a full ellipsoid
magnetic hard/soft-iron correction. It checks coverage/residuals and preserves axis
transforms. Magnetic calibration normalizes undisturbed field magnitude to 1.
It always resets both verification flags. It never drives motors or certifies
physical mounting. It refuses to overwrite an existing output file.

`motor_gain` and `reverse_gain` multiply geometric effort for each motor before
PWM limiting. They default to 1 and can correct measured forward/reverse response
imbalance. The 20% default automatic ceiling may be below motor starting duty;
raise it deliberately during commissioning if needed. No starting pulse or forced
minimum duty is added. Initial integral gains are zero. Increase gains and limits
only during controlled tuning; example gains are not a hydrodynamic calibration.

## Heading and magnetic interference

Water does not stop Earth's magnetic field from being sensed. Local motors,
power wiring, magnetic hardware and steel can distort it. A stationary calibration
cannot remove arbitrary motor-current-dependent interference.

`magnetic_verified` defaults false. Clearance/leveling can run with gyro-based yaw
rate damping even when magnetic heading is unavailable. To mark it true:

- Hold the submerged vehicle at a known fixed heading, independently observed.
- Log raw magnetometer and heading with each motor running in both directions and
  representative combinations, including reversals, under actual water load.
- Check induced heading error, not only field magnitude. Require error within 5°
  throughout the intended operating envelope.
- If it fails, relocate the compass away from power hardware, potentially into a
  separate sealed module. Recalibrate and retest. PWM-based interference
  compensation is not implemented; useful current-based compensation would need
  additional measurements and characterization.

Leveling uses a six-axis Fusion filter that **never receives magnetic data**.
Heading is propagated using calibrated gyro rates, then corrected slowly only by
accepted tilt-compensated magnetic samples. Configurable gates default to 20%
field-magnitude deviation, 15° heading innovation, and 10° inter-sample heading step.
These checks cannot identify every plausible persistent distorted field. Hardware
interference tests remain mandatory for heading hold.

A compass fault stops magnetic correction immediately. A brief gyro-only bridge
lasts at most one second since the last trusted compass sample. Thereafter the
controller uses zero-yaw-rate damping while clearance and leveling continue. This
limits rotation but cannot prevent long-term heading drift. Magnetic data cannot
silently reactivate heading hold. After three healthy seconds, click **Recapture
heading** to capture the current heading. This also avoids a sudden turn back to a
potentially wrong old target. The application gate is independent of Fusion's
magnetic recovery behavior.

## Operation and tuning

Start stopped. **Enable automatic control** captures current corrected clearance
and, when healthy for three seconds, current heading. Otherwise yaw starts in
damping mode. Roll/pitch target calibrated level. Motor 6 remains zero.

Edit clearance in cm and magnetic heading in degrees, then **Apply targets**.
Targets slew at 5 cm/s and 5°/s by default. In yaw-damping mode only clearance is
changed; recapture heading before editing heading targets. **STOP ALL**, Escape,
unchecking automatic control, manual API commands, and closing cancel automatic
control and pending reversals. A fault or I2C recovery never silently resumes it.

The initial geometric mix is:

```text
M1 = -yaw                  M2 = +yaw
M3 = 0.45*up - 0.5*roll + 0.5*pitch
M4 = 0.45*up + 0.5*roll + 0.5*pitch
M5 = up - pitch            M6 = 0
```

Allocation preserves roll/pitch before common vertical effort during saturation;
yaw is limited independently. Actual balance depends on geometry and center of
mass. Calibrate response before interpreting these normalized factors as force.

Tune gyro-rate damping first, angle hold second, clearance hold third, and combined
operation last. Test ±10° orientation and ±10 cm clearance disturbances in both
directions. Acceptance targets after tuning are ±5° roll/pitch and ±10° healthy
heading within 10 seconds, and ±5 cm clearance within 15 seconds, without sustained
oscillation. Simulation tests validate signs and data flow, not pool performance.

## Timing and fault behavior

One worker owns I2C and DIR. It targets 50 Hz accelerometer/gyro acquisition,
10 Hz magnetometer, 1 Hz barometer, and 20 Hz control/PWM commands. The UART sensors
remain staggered at one request per 100 ms, each sensor every 300 ms. The native
I2C calls themselves are subject to Linux driver timing, not hard real-time bounds.

Automatic control stops on IMU age >100 ms, control/scheduler deadline misses,
invalid/no-echo vertical range, range age >1 s, tilt >20°, estimator reset, or link
failure. Invalid/no-echo samples stop control immediately when received. A stale
range eventually stops control even if motor acknowledgements keep arriving.
Magnetometer-only faults use the degraded policy above. BMP280 is telemetry only;
it measures enclosure air pressure, not underwater depth.

Reversal acknowledges zero PWM, coasts that motor for 0.5 s, then changes DIR.
Other motors and sensors continue updating. Stop/latest targets cancel obsolete
reversal requests. Integrators freeze during actuator constraints and saturation.

I2C recovery sends only zero frames, up to five attempts with exponential backoff.
DIR is held steady until zero is acknowledged. Exhausted recovery latches a fault
and holds DIR for another 1.1 s without writes, allowing the independent ESP32
one-second watchdog to disable PWM before DIR is lowered. Startup requires zero
acknowledgement. Shutdown attempts zero and waits for the watchdog on failure.

JSONL diagnostics contain monotonic `at` timestamps, raw chip readings, requested
and acknowledged motor values, reversal states, saturation, orientation, magnetic
health, acknowledgement time and fault messages. Logging/tracing can affect
scheduling; a detected overrun disables automatic motion. Stop logging cleanly
before fitting calibration data. Do not use logs as proof of measured motor RPM.

## Protocol v2

Commands remain 13 bytes: `A5 01`, uint32 little-endian sequence, six unsigned PWM
percentages in firmware channel order, then XOR of preceding bytes.

Status is exactly 32 bytes. Bytes 0–13 retain the motor-prefix structure, with
status version **2** instead of 1. The motor-prefix checksum covers bytes 0–12.
Each sensor has a six-byte independent block at offsets 14, 20, and 26:

```text
magic D1/D2/D3
bits 7..2: modulo-64 measurement sequence; bits 1..0: sensor state
uint16 little-endian distance in millimetres
measurement age in 10 ms units, rounded conservatively upward
XOR of the preceding five block bytes
```

Age 255 means unknown or expired. Sequence advances on each completed measurement
or timeout, including unchanged distances. It wraps after 64 updates. The Pi also
subtracts command-to-ack latency conservatively. Repeated sequences do not refresh
sample time, even in a fresh motor acknowledgement. The three blocks are checked
independently. There is no additional SMBus register or byte-count prefix.

## Verification and references

```sh
.venv-control/bin/python -m unittest discover -s test -p 'test_*.py'
pio run -e six-motor-i2c
```

Tests include real Fusion execution, deterministic simulated ESP32 transactions,
nonblocking reversals, checksum/sequence/age faults, compass degradation, pool-like
small-angle disturbance simulation, calibration fitting, real Tk widgets when a
display is available, and host compilation of firmware range encoding/decoding.
No test starts real motors. Repeat timing measurements on the actual Pi under
camera load before enabling stabilization in water.

References: [BlueROV1 mixer](https://github.com/ArduPilot/ardupilot/blob/master/libraries/AP_Motors/AP_Motors6DOF.cpp),
[frame discussion](https://discuss.bluerobotics.com/t/thruster-configuration-in-bluerov1/22055),
[Fusion](https://github.com/xioTechnologies/Fusion),
[compass interference](https://ardupilot.org/copter/docs/common-compass-setup-advanced.html),
[depth versus bottom tracking](https://ardupilot.org/sub/docs/modes.html).
