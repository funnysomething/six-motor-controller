#!/usr/bin/env python3
"""Control six L298N motors: Pi direction GPIO, ESP32 PWM over I2C."""

import argparse
from dataclasses import dataclass
from pathlib import Path
import platform
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time

# BCM GPIO pairs in (forward-high, reverse-high) order, Motor1 through Motor6.
# Physical pins: (37,35), (33,31), (29,23), (21,19), (15,13), (11,7).
# The screenshot repeats Motor6 on IN1/IN2: those belong to Motor5 here.
DIRECTION_PINS = ((26, 19), (13, 6), (5, 11), (9, 10), (22, 27), (17, 4))
MOTOR_COUNT = 6
PWM_GPIO_PINS = (4, 13, 12, 16, 17, 18)  # ESP32 firmware channel order.
DEFAULT_PWM_MAP = tuple(range(MOTOR_COUNT))
I2C_ADDRESS = 0x32
REVERSE_PAUSE = 0.5
HEARTBEAT_INTERVAL = 0.2
ACK_TIMEOUT = 0.3
ULTRASONIC_STALE_SECONDS = 1.0
DEPTH_MOTOR_PAIRS = ((0, 1), (2, 3), (4, 5))
DEPTH_FULL_SPEED_CM = 3.0
DEPTH_STOP_CM = 10.0
HEADER_CHIP_LABELS = {"pinctrl-bcm2835", "pinctrl-bcm2711", "pinctrl-rp1"}
MOTOR_GPIO_PINS = sorted({pin for pair in DIRECTION_PINS for pin in pair})


def gpio_text(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value).strip("\x00 \t\r\n")


def matching_gpio_names(chip):
    names = chip.get("gpio_names", {})
    return all(gpio_text(names.get(pin, "")).upper() == f"GPIO{pin}" for pin in MOTOR_GPIO_PINS)


def gpio_chip_description(chip):
    path = chip.get("path", f"/dev/gpiochip{chip['index']}")
    if "error" in chip:
        return f"{path}: cannot open/read chip metadata: {chip['error']}"
    name = gpio_text(chip.get("name", ""))
    label = gpio_text(chip.get("label", ""))
    return f"{path}: name={name!r}, label={label!r}, {chip['lines']} lines"


def scan_gpio_chips(lgpio, devices=None):
    """Inspect chip metadata without claiming GPIOs or changing their modes."""
    devices = Path("/dev").glob("gpiochip*") if devices is None else devices
    chips = []
    for device in devices:
        if not device.name.removeprefix("gpiochip").isdigit():
            continue
        chip = {"index": int(device.name[8:]), "path": str(device)}
        handle = None
        try:
            handle = lgpio.gpiochip_open(chip["index"])
            _, lines, name, label = lgpio.gpio_get_chip_info(handle)
            chip.update(lines=lines, name=gpio_text(name), label=gpio_text(label))
            chip["gpio_names"] = {}
            for pin in MOTOR_GPIO_PINS:
                if pin >= lines:
                    continue
                try:
                    _, offset, flags, line_name, consumer = lgpio.gpio_get_line_info(handle, pin)
                    chip["gpio_names"][pin] = gpio_text(line_name)
                except Exception as error:
                    chip["line_error"] = str(error)
                    break
        except Exception as error:
            chip["error"] = str(error)
        finally:
            if handle is not None:
                lgpio.gpiochip_close(handle)
        chips.append(chip)
    return sorted(chips, key=lambda chip: chip["index"])


def select_gpio_chip(chips, requested=None):
    # Explicit selection must not repeat the automatic label restriction.
    # Validate that the device exists, is accessible and contains every offset.
    if requested is not None:
        selected = next((chip for chip in chips if chip["index"] == requested), None)
        if selected is None:
            raise RuntimeError(f"/dev/gpiochip{requested} was not found. Run --diagnose.")
        if "error" in selected:
            raise RuntimeError(gpio_chip_description(selected) + "; run --diagnose.")
        if selected["lines"] <= max(MOTOR_GPIO_PINS):
            raise RuntimeError(
                f"/dev/gpiochip{requested} has only {selected['lines']} lines; "
                "motor directions require offsets through GPIO26. Run --diagnose.")
        # Reject a controller whose named lines contradict BCM GPIO numbering.
        for pin, name in selected.get("gpio_names", {}).items():
            if name and gpio_text(name).upper() != f"GPIO{pin}":
                raise RuntimeError(
                    f"/dev/gpiochip{requested} offset {pin} is {name!r}, not GPIO{pin}; "
                    "the BCM mapping is not verified. Run --diagnose.")
        return requested
    candidates = [chip for chip in chips if "error" not in chip
                  and chip["lines"] > max(MOTOR_GPIO_PINS)
                  and (gpio_text(chip["label"]) in HEADER_CHIP_LABELS or matching_gpio_names(chip))]
    # Prefer complete pin-name evidence if more than one label resembles a header.
    verified = [chip for chip in candidates if matching_gpio_names(chip)]
    if verified:
        candidates = verified
    # Compatibility symlinks can expose one controller under two device numbers.
    # Its kernel chip name is the same through both paths; prefer the lower index.
    controllers = {}
    for chip in sorted(candidates, key=lambda chip: chip["index"]):
        identity = gpio_text(chip.get("name", "")) or chip["index"]
        controllers.setdefault(identity, chip)
    candidates = list(controllers.values())
    if len(candidates) == 1:
        return candidates[0]["index"]
    raise RuntimeError(
        "Cannot identify one accessible Pi header GPIO chip. Run --diagnose; "
        "check gpio-group permissions and use --gpiochip N for the header chip.\n"
        + "\n".join(gpio_chip_description(chip) for chip in chips))


def read_motor_gpio_lines(lgpio, chip):
    handle = lgpio.gpiochip_open(chip)
    lines = []
    try:
        for motor, pair in enumerate(DIRECTION_PINS, 1):
            for pin in pair:
                _, offset, flags, name, consumer = lgpio.gpio_get_line_info(handle, pin)
                consumer = gpio_text(consumer)
                lines.append(dict(motor=motor, pin=pin, offset=offset, flags=flags,
                                  name=gpio_text(name), consumer=consumer,
                                  used=bool(flags & 1) or bool(consumer)))
    finally:
        lgpio.gpiochip_close(handle)
    return lines


def parse_gpio_fdinfo(text):
    chip = None
    pins = []
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        if key == "gpio-chip":
            chip = value.strip()
        elif key == "gpio-line" and value.strip().isdigit():
            pins.append(int(value.strip()))
    return chip, pins


def gpio_process_holders(chip_name, proc_root=Path("/proc")):
    """Read Linux GPIO fdinfo; never inspect process arguments or signal PIDs."""
    holders = []
    unreadable = 0
    for process in proc_root.glob("[0-9]*"):
        try:
            descriptors = list((process / "fdinfo").iterdir())
        except PermissionError:
            unreadable += 1
            continue
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                chip, pins = parse_gpio_fdinfo(descriptor.read_text())
                if chip != chip_name or not set(pins).intersection(MOTOR_GPIO_PINS):
                    continue
                name = (process / "comm").read_text().strip()
                holders.append(dict(pid=int(process.name), process=name,
                                    pins=sorted(set(pins).intersection(MOTOR_GPIO_PINS))))
            except PermissionError:
                unreadable += 1
            except OSError:
                continue
    return holders, unreadable


def gpio_boot_settings(paths=None):
    paths = (Path("/boot/firmware/config.txt"), Path("/boot/config.txt")) if paths is None else paths
    settings = []
    for path in paths:
        try:
            text = path.read_text()
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            content = line.split("#", 1)[0].strip()
            if content.startswith(("dtoverlay=", "dtparam=", "include ")):
                if content.startswith("include ") or any(word in content.lower()
                                                         for word in ("uart", "spi", "w1", "pwm", "gpio", "i2s")):
                    settings.append(f"{path}:{number}: {content}")
    return settings


def print_gpio_claim_details(chip_name):
    holders, unreadable = gpio_process_holders(chip_name)
    print("Processes holding motor GPIO lines (when exported by this kernel):")
    for holder in holders:
        print(f"  PID {holder['pid']} ({holder['process']}): "
              + ", ".join(f"GPIO{pin}" for pin in holder["pins"]))
    if not holders:
        print("  No readable process-specific owner found. Kernel drivers have no process PID;\n"
              "  older kernels may omit GPIO fdinfo. This does not mean the pins are free.")
    if unreadable:
        print("  Some process metadata was inaccessible. For a read-only check of other users,\n"
              "  run this script with sudo and --diagnose.")
    print("Configured boot settings to check (not proof of which drivers are active):")
    settings = gpio_boot_settings()
    for setting in settings:
        print("  " + setting)
    if not settings:
        print("  No matching settings found in the main boot config files.")
    try:
        result = subprocess.run(["pinctrl", "get", ",".join(map(str, MOTOR_GPIO_PINS))],
                                capture_output=True, text=True, timeout=3, check=False)
        if result.returncode == 0:
            print("Current pin functions (read-only pinctrl get):\n" + result.stdout.strip())
        else:
            print("pinctrl could not read pin functions: " + result.stderr.strip())
    except (OSError, subprocess.TimeoutExpired):
        print("pinctrl is unavailable; line metadata and boot settings are shown above.")


def ensure_motor_gpio_available(lgpio, chip):
    used = [line for line in read_motor_gpio_lines(lgpio, chip) if line["used"]]
    if used:
        claims = "\n".join(f"  Motor{line['motor']} GPIO{line['pin']}: "
                           + (line["consumer"] or "claimed; kernel supplied no consumer label")
                           for line in used)
        raise RuntimeError("Motor GPIOs already claimed; no direction pins were configured.\n"
                           + claims + "\nRun --diagnose for process/boot-setting clues. "
                           "Close the owning program or disable its conflicting overlay and reboot.")


def diagnose_gpio(requested=None):
    """Read-only local diagnostics; no ESP32/I2C connection is required."""
    try:
        model = Path("/proc/device-tree/model").read_text().rstrip("\x00\n")
    except OSError:
        model = "unknown (device-tree model unavailable)"
    print(f"Pi model: {model}\nKernel: {platform.release()}")
    try:
        import lgpio
    except ImportError:
        print("Install GPIO support: sudo apt install python3-lgpio python3-gpiozero")
        return 1
    chips = scan_gpio_chips(lgpio)
    if not chips:
        print("No /dev/gpiochip devices found.")
        return 1
    for chip in chips:
        print(gpio_chip_description(chip))
        if "error" in chip:
            continue
        if chip["lines"] <= max(MOTOR_GPIO_PINS):
            print("  Too few lines for the motor GPIO offsets.")
        elif matching_gpio_names(chip):
            print("  All twelve motor line names match BCM GPIO offsets.")
        elif gpio_text(chip["label"]) in HEADER_CHIP_LABELS:
            print("  Recognized Pi header label.")
        else:
            print("  Header not automatically verified; inspect the line names below.")
            for pin, name in chip.get("gpio_names", {}).items():
                print(f"    offset {pin}: {name!r}")
        if "line_error" in chip:
            print(f"  Cannot read line names: {chip['line_error']}")
    try:
        selected = select_gpio_chip(chips, requested)
    except RuntimeError as error:
        print(error)
        return 1
    print(f"Header GPIO chip: /dev/gpiochip{selected}")
    lines = read_motor_gpio_lines(lgpio, selected)
    for line in lines:
        state = "CLAIMED" if line["used"] else "unclaimed"
        owner = repr(line["consumer"]) if line["consumer"] else "<not supplied by kernel>"
        print(f"  Motor{line['motor']} GPIO{line['pin']}: line {line['offset']} ({line['name']}), "
              f"{state}, consumer={owner}, flags=0x{line['flags']:x}")
    used = [line for line in lines if line["used"]]
    if used:
        print("Claimed motor pins: " + ", ".join(f"GPIO{line['pin']}" for line in used))
        selected_chip = next(chip for chip in chips if chip["index"] == selected)
        print_gpio_claim_details(gpio_text(selected_chip.get("name", "")) or f"gpiochip{selected}")
    print("If a motor pin is owned by SPI, disable SPI in raspi-config and reboot.\n"
          "Also check old UART3/4/5 overlays: they overlap GPIO4/5, GPIO9, and GPIO13 on Pi 4.\n"
          "Close other motor programs. GPIO4 can also be claimed by a 1-Wire overlay.\n"
          "For permissions: sudo usermod -aG gpio,i2c \"$USER\", then log out and back in.")
    return 1 if used else 0


def make_gpio_factory(requested=None):
    try:
        import lgpio
        from gpiozero.pins.lgpio import LGPIOFactory
    except ImportError as error:
        raise RuntimeError("Install GPIO support: sudo apt install python3-lgpio python3-gpiozero") from error
    chip = select_gpio_chip(scan_gpio_chips(lgpio), requested)
    ensure_motor_gpio_available(lgpio, chip)
    print(f"Using header GPIO chip /dev/gpiochip{chip} (BCM numbering).")
    if requested is not None:
        print("Explicit chip selection: verify this device is the 40-pin header controller.")
    return LGPIOFactory(chip=chip)


def make_output_factory(pin_factory, output_type):
    def output(pin, initial_value=False):
        try:
            return output_type(pin, initial_value=initial_value, pin_factory=pin_factory)
        except Exception as error:
            motor = next(i for i, pair in enumerate(DIRECTION_PINS, 1) if pin in pair)
            raise RuntimeError(
                f"Cannot initialize Motor{motor} GPIO{pin} on gpiochip{pin_factory.chip}: {error}. "
                "Run --diagnose; check SPI/1-Wire overlays, other GPIO programs, "
                "permissions, and installed gpiozero/lgpio versions.") from error
    return output


def checksum(data):
    result = 0
    for value in data:
        result ^= value
    return result


def parse_pwm_map(value):
    """CLI channel numbers are 1-based, in Pi direction-motor order."""
    try:
        channels = tuple(int(channel.strip()) - 1 for channel in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use six comma-separated channel numbers, 1 through 6.") from error
    if sorted(channels) != list(DEFAULT_PWM_MAP):
        raise argparse.ArgumentTypeError("--pwm-map must contain each channel 1 through 6 exactly once.")
    return channels


class I2CPwm:
    """Raw I2C frames; no SMBus register or length bytes are added."""

    def __init__(self, bus, message_factory, address=I2C_ADDRESS, channel_map=DEFAULT_PWM_MAP):
        channel_map = tuple(channel_map)
        if (any(type(channel) is not int for channel in channel_map)
                or sorted(channel_map) != list(DEFAULT_PWM_MAP)):
            raise ValueError("PWM channel map must be a permutation of zero-based channels 0 through 5")
        self.bus = bus
        self.message = message_factory
        self.address = address
        self.channel_map = channel_map
        self.sequence = secrets.randbits(32)
        # Atomic tuple of (receipt time, sensor state, millimeters), updated
        # only by acknowledged motor transactions. Tk never touches the bus.
        self.ultrasonic_samples = (None, None, None)

    @property
    def ultrasonic_sample(self):
        return self.ultrasonic_samples[0]

    def set_percentages(self, values):
        values = tuple(values)
        if len(values) != MOTOR_COUNT or any(
                type(v) is not int or not 0 <= v <= 100 for v in values):
            raise ValueError("PWM needs six integer percentages from 0 to 100")
        # Direction GPIOs and GUI stay in logical motor order. Only PWM wire
        # magnitudes are reordered; acknowledgements must match that order too.
        wire_values = [0] * MOTOR_COUNT
        for motor, channel in enumerate(self.channel_map):
            wire_values[channel] = values[motor]
        values = tuple(wire_values)
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        sequence = self.sequence.to_bytes(4, "little")
        frame = bytes((0xA5, 1)) + sequence + bytes(values)
        self.bus.i2c_rdwr(self.message.write(self.address, frame + bytes((checksum(frame),))))
        deadline = time.monotonic() + ACK_TIMEOUT
        while time.monotonic() < deadline:
            # Separate write/read transactions give the ESP32 callback time to
            # apply all six channels. Its first read may contain old status.
            time.sleep(0.005)
            reply = self.message.read(self.address, 29)
            self.bus.i2c_rdwr(reply)
            status = bytes(reply)
            if (len(status) >= 14 and status[:2] == b"\x5a\x01"
                    and status[2:6] == sequence and status[6] == 0
                    and status[7:13] == bytes(values)
                    and status[13] == checksum(status[:13])):
                samples = list(self.ultrasonic_samples)
                received_at = time.monotonic()
                for sensor, offset, magic, check_start in ((0, 14, 0xD1, 0),
                                                          (1, 19, 0xD2, 19),
                                                          (2, 24, 0xD3, 24)):
                    if (len(status) >= offset + 5 and status[offset] == magic
                            and status[offset + 1] in (0, 1, 2, 3)
                            and status[offset + 4] == checksum(status[check_start:offset + 4])):
                        state = status[offset + 1]
                        mm = int.from_bytes(status[offset + 2:offset + 4], "little")
                        if state != 1 or 20 <= mm <= 3000:
                            samples[sensor] = (received_at, state, mm)
                self.ultrasonic_samples = tuple(samples)
                return
        raise OSError("ESP32 did not acknowledge applied PWM; check I2C/firmware")


@dataclass(frozen=True)
class DepthControl:
    """Proportional pair speeds: maximum at 3 cm, stopped at 10 cm."""

    pair_speeds: tuple = (100, 100, 100)

    def __post_init__(self):
        if (len(self.pair_speeds) != 3 or any(
                type(value) is not int or not -100 <= value <= 100
                for value in self.pair_speeds)):
            raise ValueError("Use three integer pair speeds from -100 to 100.")

    def speeds(self, samples, now):
        values = [0] * MOTOR_COUNT
        for sensor, pair in enumerate(DEPTH_MOTOR_PAIRS):
            sample = samples[sensor] if sensor < len(samples) else None
            if sample is None:
                continue
            at, state, mm = sample
            if state != 1 or not 0 <= now - at <= ULTRASONIC_STALE_SECONDS or not 20 <= mm <= 3000:
                continue
            fraction = max(0.0, min(1.0, (DEPTH_STOP_CM - mm / 10) /
                                        (DEPTH_STOP_CM - DEPTH_FULL_SPEED_CM)))
            speed = round(self.pair_speeds[sensor] * fraction)
            for motor in pair:
                values[motor] = speed
        return tuple(values)


class MotorController:
    """Serialize direction changes and heartbeat writes for all six motors."""

    def __init__(self, pwm, output_factory, start_heartbeat=True, imu=None, trace=False):
        self.pwm = pwm
        self.imu = imu
        self.outputs = []
        self.speeds = (0,) * MOTOR_COUNT
        self.last_directions = [0] * MOTOR_COUNT
        self.stopped_at = [0.0] * MOTOR_COUNT
        self.fault = None
        self.depth_control = None
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.worker = None
        self.closed = False
        self.trace = trace
        self.last_pwm_ack_at = None
        self.output_sample = None
        self.last_trace_at = float("-inf")
        self.last_trace_state = None
        try:
            for pair in DIRECTION_PINS:
                for pin in pair:
                    self.outputs.append(output_factory(pin, initial_value=False))
            self._write_pwm(self.speeds)
            self._record_outputs()
            if start_heartbeat:
                self.worker = threading.Thread(target=self._heartbeat, daemon=True)
                self.worker.start()
        except BaseException:
            self.done.set()
            if self.worker is not None and self.worker.is_alive():
                self.worker.join()
            for output in self.outputs:
                try:
                    output.off()
                except Exception:
                    pass
                try:
                    output.close()
                except Exception:
                    pass
            raise

    def _direction_off(self):
        for output in self.outputs:
            output.off()

    @property
    def ultrasonic_sample(self):
        return self.pwm.ultrasonic_sample

    @property
    def ultrasonic_samples(self):
        return self.pwm.ultrasonic_samples

    def _record_outputs(self):
        """Cache actual direction readback on the hardware worker, never Tk."""
        try:
            directions = tuple((int(bool(self.outputs[2 * motor].value)),
                                int(bool(self.outputs[2 * motor + 1].value)))
                               for motor in range(MOTOR_COUNT))
            error = ""
        except Exception as read_error:
            directions = None
            error = str(read_error)
        self.output_sample = (self.speeds, directions, error)
        now = time.monotonic()
        mode = "auto" if self.depth_control is not None else "manual"
        state = (mode, self.output_sample, self.fault is not None)
        if self.trace and (state != self.last_trace_state or now - self.last_trace_at >= 1):
            pins = (" ".join(f"{forward}{reverse}" for forward, reverse in directions)
                    if directions is not None else f"read error: {error}")
            age = (f"{now - self.last_pwm_ack_at:.2f}s" if self.last_pwm_ack_at is not None else "none")
            print(f"Motors {mode}: applied={self.speeds}; GPIO IN1/IN2={pins}; "
                  f"PWM ack age={age}; fault={self.fault}",
                  file=sys.stderr, flush=True)
            self.last_trace_at = now
            self.last_trace_state = state

    def _write_pwm(self, values):
        """Send once; communication failures propagate to the caller."""
        self.pwm.set_percentages(values)
        self.last_pwm_ack_at = time.monotonic()

    def _fail(self, error):
        # Latch the error so a background heartbeat can never restart motion.
        self.fault = error
        self.depth_control = None
        self.done.set()
        self._direction_off()
        self.speeds = (0,) * MOTOR_COUNT
        self._record_outputs()
        try:
            self.pwm.set_percentages(self.speeds)
        except OSError:
            pass  # The ESP32 also disables PWM after its one-second watchdog.

    def _heartbeat(self):
        while not self.done.wait(HEARTBEAT_INTERVAL):
            with self.lock:
                if self.done.is_set():
                    return
                try:
                    self._write_pwm(tuple(abs(v) for v in self.speeds))
                    if self.depth_control is not None:
                        self._update_depth_control()
                    self._record_outputs()
                    if self.imu is not None:
                        # Same worker and controller lock as PWM writes: no bus
                        # transactions from Tk, and one IMU component per cycle.
                        self.imu.poll_next()
                except Exception as error:
                    # Unexpected GPIO/IMU errors must not silently kill this
                    # thread and leave the GUI showing a frozen nonzero speed.
                    if self.fault is None:
                        self._fail(error)
                    print(f"\nMotor worker failed; all directions disabled: {error}", file=sys.stderr)
                    return

    def enable_depth_control(self, settings):
        if not isinstance(settings, DepthControl):
            raise ValueError("Provide DepthControl settings")
        with self.lock:
            self.set_speeds((0,) * MOTOR_COUNT)
            self.depth_control = settings

    def _update_depth_control(self):
        # Called under the controller lock after receiving fresh telemetry.
        # Coast over successive heartbeats instead of sleeping on a reversal,
        # so sensors continue updating and STOP can acquire the lock promptly.
        now = time.monotonic()
        requested = self.depth_control.speeds(self.ultrasonic_samples, now)
        values = list(requested)
        for motor, value in enumerate(values):
            direction = (value > 0) - (value < 0)
            if direction and self.last_directions[motor] not in (0, direction):
                if self.speeds[motor] or now - self.stopped_at[motor] < REVERSE_PAUSE:
                    values[motor] = 0
        values = tuple(values)
        if values != self.speeds:
            self._set_speeds(values)

    def set_speeds(self, values):
        """A manual command also exits automatic depth control."""
        with self.lock:
            self._set_speeds(values)
            self.depth_control = None

    def _set_speeds(self, values):
        """Set all six signed percentages; unchanged motors keep running."""
        values = tuple(values)
        if len(values) != MOTOR_COUNT or any(
                type(v) is not int or not -100 <= v <= 100 for v in values):
            raise ValueError("Provide six integer speeds from -100 to 100")
        with self.lock:
            if self.fault is not None or self.done.is_set():
                raise OSError("Controller stopped; restart it before commanding motion")
            directions = tuple((v > 0) - (v < 0) for v in values)
            current = tuple((v > 0) - (v < 0) for v in self.speeds)
            changed = [i for i in range(MOTOR_COUNT) if directions[i] != current[i]]
            try:
                if changed:
                    gated = tuple(0 if i in changed else abs(v) for i, v in enumerate(self.speeds))
                    # Confirm affected ENA/ENB outputs are zero before touching IN pins.
                    self._write_pwm(gated)
                    now = time.monotonic()
                    for i in changed:
                        if self.speeds[i] != 0:
                            self.stopped_at[i] = now
                        self.outputs[2 * i].off()
                        self.outputs[2 * i + 1].off()
                    pause = max((REVERSE_PAUSE - (now - self.stopped_at[i])
                                 for i in changed if directions[i] and self.last_directions[i]
                                 and directions[i] != self.last_directions[i]), default=0)
                    if pause > 0:
                        time.sleep(pause)
                    for i in changed:
                        if directions[i] > 0:
                            self.outputs[2 * i].on()
                        elif directions[i] < 0:
                            self.outputs[2 * i + 1].on()
                for i, direction in enumerate(directions):
                    if direction:
                        self.last_directions[i] = direction
                self._write_pwm(tuple(abs(v) for v in values))
                self.speeds = values
                self._record_outputs()
                return True
            except BaseException as error:
                if self.fault is None:
                    self._fail(error)
                raise

    def set_motor(self, motor, speed):
        if type(motor) is not int or not 1 <= motor <= MOTOR_COUNT:
            raise ValueError("Motor number must be 1 through 6")
        with self.lock:
            values = list(self.speeds)
            values[motor - 1] = speed
            self.set_speeds(values)

    def close(self):
        self.done.set()
        if self.worker is not None:
            self.worker.join()
        with self.lock:
            self.depth_control = None
            if self.closed:
                return
            try:
                self._direction_off()
                self._write_pwm((0,) * MOTOR_COUNT)
            finally:
                self._direction_off()
                self.speeds = (0,) * MOTOR_COUNT
                for output in self.outputs:
                    output.close()
                self.closed = True


HELP = """m 60                  all six motors forward at 60%
m -60                 all six motors reverse at 60%
m 1 60                Motor1 at 60%; keep the other five settings
m 60 -60 40 40 0 75    individual speeds for Motors 1 through 6
s                     stop/coast all six motors
help                  show commands
q                     stop and quit"""


def parse_command(text):
    words = text.strip().lower().split()
    if words in (["s"], ["help"], ["q"], ["quit"], ["exit"]):
        return words[0], ()
    if not words or words[0] != "m":
        return None
    try:
        values = tuple(int(word) for word in words[1:])
    except ValueError:
        return None
    if len(values) == 2:
        if 1 <= values[0] <= MOTOR_COUNT and -100 <= values[1] <= 100:
            return "motor", values
    elif len(values) in (1, MOTOR_COUNT) and all(-100 <= v <= 100 for v in values):
        return "all", values * MOTOR_COUNT if len(values) == 1 else values
    return None


def interrupted(signum, frame):
    raise KeyboardInterrupt


class MotorGui:
    """Small optional Tk interface; hardware requests never block Tk callbacks."""

    def __init__(self, root, controller):
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.controller = controller
        self.results = queue.Queue()
        self.worker = None
        self.busy = False
        self.stop_requested = False
        self.closing = False
        self.shutdown_requested = threading.Event()
        self.controls = []
        self.auto_enabled = tk.BooleanVar(root, value=False)
        self.depth_speeds = [tk.StringVar(root, value="100") for _ in DEPTH_MOTOR_PAIRS]
        self.values = [tk.StringVar(root, value="0") for _ in range(MOTOR_COUNT)]
        self.group_value = tk.StringVar(root, value="0")
        self.applied = [tk.StringVar(root, value="0%") for _ in range(MOTOR_COUNT)]
        self.status = tk.StringVar(root, value="Ready. All motors stopped.")
        self.output_status = tk.StringVar(root, value="Output feedback: waiting for motor controller...")
        self.distances = [tk.StringVar(root, value=f"Ultrasonic {sensor + 1}: waiting for ESP32...")
                          for sensor in range(3)]
        self.distance = self.distances[0]
        self.imu_values = {group: tk.StringVar(root, value=f"{label}: waiting for IMU...")
                           for group, label in (("accel", "Acceleration"),
                                                ("gyro", "Rotation rate"),
                                                ("mag", "Magnetic field"),
                                                ("barometer", "Temperature / pressure"))}
        self.imu_tilt = tk.StringVar(root, value="Tilt: waiting for acceleration...")
        root.title("Six motor control")
        root.minsize(920, 450)
        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Six motor control", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(frame, text="Set signed speeds, then Apply. Negative = reverse; zero = stop.").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 12))
        ttk.Label(frame, text="Motor").grid(row=2, column=0, sticky="w")
        ttk.Label(frame, text="Speed (%)").grid(row=2, column=1)
        ttk.Label(frame, text="Last applied").grid(row=2, column=3)

        def speed_widgets(row, value):
            scale = tk.Scale(frame, from_=-100, to=100, resolution=1, orient="horizontal",
                             showvalue=False, highlightthickness=0,
                             command=lambda position: value.set(str(int(float(position)))))
            # Separate the editable text from Tk's numeric scale variable so
            # incomplete/invalid text is handled by Apply, without Tcl errors.
            def sync_scale(*args):
                try:
                    speed = int(value.get())
                except ValueError:
                    return
                if -100 <= speed <= 100:
                    scale.set(speed)

            value.trace_add("write", sync_scale)
            scale.grid(row=row, column=1, sticky="ew", padx=10)
            entry = ttk.Spinbox(frame, from_=-100, to=100, textvariable=value, width=6)
            entry.grid(row=row, column=2, padx=(0, 14), pady=5)
            self.controls.extend((scale, entry))

        for motor, value in enumerate(self.values):
            row = motor + 3
            ttk.Label(frame, text=f"Motor {motor + 1}").grid(row=row, column=0, sticky="w")
            speed_widgets(row, value)
            ttk.Label(frame, textvariable=self.applied[motor], width=9, anchor="center").grid(
                row=row, column=3)
        ttk.Separator(frame).grid(row=9, column=0, columnspan=4, sticky="ew", pady=10)
        ttk.Label(frame, text="All motors").grid(row=10, column=0, sticky="w")
        speed_widgets(10, self.group_value)
        copy_button = ttk.Button(frame, text="Copy to six", command=self.copy_group)
        copy_button.grid(row=10, column=3)
        self.controls.append(copy_button)
        buttons = ttk.Frame(frame)
        buttons.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(12, 8))
        apply_button = ttk.Button(buttons, text="Apply six speeds", command=self.apply)
        apply_button.pack(side="left")
        self.controls.append(apply_button)
        tk.Button(buttons, text="STOP ALL", command=self.stop, bg="#b42318", fg="white",
                  activebackground="#8f1c13", activeforeground="white", padx=16).pack(
                      side="left", padx=12)
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")
        ttk.Label(frame, textvariable=self.status, wraplength=580).grid(
            row=12, column=0, columnspan=4, sticky="w", pady=6)
        for sensor, variable in enumerate(self.distances):
            ttk.Label(frame, textvariable=variable, font=("TkDefaultFont", 14),
                      wraplength=580).grid(
                row=13 + sensor, column=0, columnspan=4, sticky="w", pady=6)
        ttk.Label(frame, textvariable=self.output_status, wraplength=580).grid(
            row=16, column=0, columnspan=4, sticky="w", pady=(6, 0))
        auto_frame = ttk.LabelFrame(frame, text="Automatic depth control (3-10 cm)", padding=8)
        auto_frame.grid(row=2, column=4, rowspan=10, sticky="new", padx=(18, 0))
        self.auto_toggle = ttk.Checkbutton(auto_frame, text="Enable automatic control",
                                           variable=self.auto_enabled, command=self.toggle_auto)
        self.auto_toggle.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(auto_frame, text="Sensor / motor pair").grid(row=1, column=0, sticky="w")
        ttk.Label(auto_frame, text="Max speed (%)").grid(row=1, column=1)
        for sensor, pair in enumerate(DEPTH_MOTOR_PAIRS):
            ttk.Label(auto_frame, text=f"Ultrasonic {sensor + 1} / motors {pair[0] + 1}, {pair[1] + 1}").grid(
                row=sensor + 2, column=0, sticky="w", padx=(0, 12))
            entry = ttk.Spinbox(auto_frame, from_=-100, to=100,
                                textvariable=self.depth_speeds[sensor], width=8)
            entry.grid(row=sensor + 2, column=1, padx=6, pady=2)
            self.controls.append(entry)
        ttk.Label(auto_frame, text="3 cm or closer = max speed; 10 cm or farther = stop. "
                  "Speed decreases linearly between them. Negative max speed = reverse. "
                  "No good reading = pair stopped.",
                  wraplength=330).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        imu_frame = ttk.LabelFrame(frame, text="SEN0140 V2 IMU", padding=8)
        imu_frame.grid(row=12, column=4, rowspan=4, sticky="new", padx=(18, 0), pady=(8, 0))
        for row, variable in enumerate((*self.imu_values.values(), self.imu_tilt)):
            ttk.Label(imu_frame, textvariable=variable, wraplength=330).grid(
                row=row, column=0, sticky="w", pady=2)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda event: self.stop())
        root.after(50, self.poll)

    def copy_group(self):
        if self.auto_enabled.get() or self.busy or self.closing:
            return
        try:
            value = int(self.group_value.get())
            if not -100 <= value <= 100:
                raise ValueError
        except ValueError:
            self.status.set("Use an integer speed from -100 to 100.")
            return
        for variable in self.values:
            variable.set(value)
        self.status.set("Group speed copied. Click Apply to send it.")

    def apply(self):
        if self.auto_enabled.get() or self.busy or self.closing or self.controller.fault is not None:
            return
        try:
            values = tuple(int(variable.get()) for variable in self.values)
            if any(not -100 <= value <= 100 for value in values):
                raise ValueError
        except ValueError:
            self.status.set("Use integer speeds from -100 to 100 for all six motors.")
            return
        self.send(values)

    def toggle_auto(self):
        if self.busy or self.closing or self.controller.fault is not None:
            self.auto_enabled.set(getattr(self.controller, "depth_control", None) is not None)
            return
        if not self.auto_enabled.get():
            self.stop()
            return
        try:
            settings = DepthControl(tuple(int(value.get()) for value in self.depth_speeds))
        except ValueError as error:
            self.auto_enabled.set(False)
            self.status.set(f"Automatic control: {error}")
            return
        for variable in self.values:
            variable.set(0)
        self.send((0,) * MOTOR_COUNT, depth_control=settings)

    def send(self, values, depth_control=None):
        self.busy = True
        self.set_controls(False)
        self.status.set("Stopping all motors..." if not any(values) else "Applying speeds...")

        def request():
            try:
                if depth_control is None:
                    self.controller.set_speeds(values)
                else:
                    self.controller.enable_depth_control(depth_control)
                self.results.put(None)
            except Exception as error:
                self.results.put(error)

        self.worker = threading.Thread(target=request, daemon=True)
        self.worker.start()

    def stop(self):
        self.auto_enabled.set(False)
        for variable in self.values:
            variable.set(0)
        self.group_value.set(0)
        if self.controller.fault is not None:
            self.status.set(f"Motor link failed; directions disabled: {self.controller.fault}")
        elif self.busy:
            # No queued motion commands: stop is the next request after the
            # in-flight operation completes, including its reversal interval.
            self.stop_requested = True
            self.status.set("Stopping after the current request...")
        elif not self.closing:
            self.send((0,) * MOTOR_COUNT)

    def set_controls(self, enabled):
        for widget in self.controls:
            widget.configure(state="normal" if enabled and not self.auto_enabled.get() else "disabled")
        self.auto_toggle.configure(state="normal" if enabled else "disabled")

    def poll(self):
        if self.shutdown_requested.is_set() and not self.closing:
            self.close()
            if not self.busy:
                return
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.worker.join()
            if result is not None:
                self.auto_enabled.set(False)
                self.status.set(f"Command failed: {result}")
            else:
                self.status.set("All motors stopped." if not any(self.controller.speeds)
                                else "Speeds applied.")
            if self.closing:
                self.root.destroy()
                return
            if self.stop_requested:
                self.stop_requested = False
                if self.controller.fault is None:
                    self.send((0,) * MOTOR_COUNT)
        for variable, speed in zip(self.applied, self.controller.speeds):
            variable.set(f"{speed:+d}%")
        if self.controller.fault is not None:
            self.auto_enabled.set(False)
            self.status.set(f"Motor link failed; directions disabled: {self.controller.fault}")
        elif self.auto_enabled.get() and not self.busy and not self.closing:
            self.status.set("Automatic control active: proportional speed over 3-10 cm; no good reading stops its pair.")
        self.update_distance()
        self.update_imu()
        output_problem = self.update_outputs()
        if output_problem and self.controller.fault is None and not self.busy and not self.closing:
            self.status.set(output_problem)
        self.set_controls(not self.busy and not self.closing and self.controller.fault is None)
        self.root.after(50, self.poll)

    def update_outputs(self):
        sample = getattr(self.controller, "output_sample", None)
        if sample is None:
            return None
        speeds, directions, error = sample
        ack_at = getattr(self.controller, "last_pwm_ack_at", None)
        age = time.monotonic() - ack_at if ack_at is not None else None
        ack_text = f"{age:.1f} s ago" if age is not None else "unavailable"
        pins = (" ".join(f"M{motor + 1}={forward}{reverse}"
                         for motor, (forward, reverse) in enumerate(directions))
                if directions is not None else f"read error: {error}")
        self.output_status.set(f"PWM acknowledged: {ack_text}. Pi direction GPIO: {pins}\n"
                               "10 = forward; 01 = reverse; 00 = stopped. Feedback does not measure rotation.")
        if age is None or age > 1.0:
            return "Motor updates stale; the ESP32 watchdog may have stopped PWM. Check terminal output."
        expected = tuple((int(speed > 0), int(speed < 0)) for speed in speeds)
        if directions is None or directions != expected:
            return "Motor direction GPIO feedback disagrees with applied speeds. Check terminal output."
        return None

    def update_distance(self):
        samples = getattr(self.controller, "ultrasonic_samples",
                          (getattr(self.controller, "ultrasonic_sample", None), None))
        for sensor in range(len(self.distances)):
            sample = samples[sensor] if sensor < len(samples) else None
            text = self.distance_text(sample)
            self.distances[sensor].set(f"Ultrasonic {sensor + 1}: {text}")

    def distance_text(self, sample):
        if self.controller.fault is not None:
            text = "unavailable (motor link failed)"
        elif sample is None:
            text = "unavailable (waiting for sensor firmware)"
        else:
            received_at, state, mm = sample
            if time.monotonic() - received_at > ULTRASONIC_STALE_SECONDS:
                text = "stale (no recent readings)"
            elif state == 1:
                text = f"{mm / 10:.1f} cm"
            elif state == 2:
                text = "no underwater echo / outside 2-300 cm range"
            elif state == 3:
                text = "no valid UART response (check RX/TX, power and baud rate)"
            else:
                text = "waiting for first measurement..."
        return text

    def update_imu(self):
        imu = getattr(self.controller, "imu", None)
        labels = {"accel": "Acceleration", "gyro": "Rotation rate",
                  "mag": "Magnetic field", "barometer": "Temperature / pressure"}
        acceleration = None
        for group, variable in self.imu_values.items():
            reading = imu.readings[group] if imu is not None else None
            if self.controller.fault is not None:
                text = "unavailable (motor link failed; polling stopped)"
            elif imu is None:
                text = "disabled"
            elif reading is None:
                text = "waiting for sensor detection..."
            elif reading.error:
                text = f"unavailable ({reading.error})"
            elif time.monotonic() - reading.at > imu.STALE_SECONDS:
                text = "stale (no recent reading)"
            elif group == "barometer":
                temperature, pressure = reading.values
                text = f"{temperature:.2f} C / {pressure:.2f} hPa"
            else:
                x, y, z = reading.values
                if group == "mag":
                    text = f"X {x:d}  Y {y:d}  Z {z:d} raw counts"
                else:
                    unit = "g" if group == "accel" else "deg/s"
                    text = f"X {x:+.3f}  Y {y:+.3f}  Z {z:+.3f} {unit}"
                if group == "accel":
                    acceleration = reading.values
            variable.set(f"{labels[group]}: {text}")
        tilt = imu.tilt(acceleration) if acceleration is not None else None
        if tilt is None:
            self.imu_tilt.set("Tilt: unavailable (requires fresh acceleration)")
        else:
            roll, pitch = tilt
            self.imu_tilt.set(f"Tilt from acceleration: roll {roll:+.1f} deg, "
                              f"pitch {pitch:+.1f} deg (valid when nearly stationary)")

    def close(self):
        self.closing = True
        self.set_controls(False)
        self.status.set("Stopping and closing...")
        if not self.busy:
            self.root.destroy()  # main's finally stops motors before closing I2C.


def run_gui(controller):
    try:
        import tkinter as tk
    except ImportError as error:
        raise RuntimeError("Install GUI support: sudo apt install python3-tk") from error
    try:
        root = tk.Tk()
    except tk.TclError as error:
        raise RuntimeError("GUI needs a desktop display; run on the Pi desktop or use SSH X forwarding") from error
    app = None
    previous_handlers = {}
    try:
        app = MotorGui(root, controller)
        # Tk callbacks can swallow KeyboardInterrupt; request a normal GUI
        # shutdown from the next poll instead, without touching Tk in a handler.
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous_handlers[signum] = signal.signal(
                    signum, lambda signum, frame: app.shutdown_requested.set())
        root.mainloop()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        # Also cover Ctrl+C/SIGTERM while a hardware request is in flight.
        if app is not None and app.worker is not None:
            app.worker.join()
        try:
            root.destroy()
        except tk.TclError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus", type=int, default=1, help="I2C bus number (default: 1)")
    parser.add_argument("--address", type=lambda value: int(value, 0), default=I2C_ADDRESS,
                        help="ESP32 7-bit I2C address (default: 0x32)")
    parser.add_argument("--gui", action="store_true", help="open the optional six-motor desktop GUI")
    parser.add_argument("--no-imu", action="store_true", help="disable SEN0140 polling in the GUI")
    parser.add_argument("--trace-motors", action="store_true",
                        help="log applied PWM, direction GPIO readback and acknowledgement age")
    parser.add_argument("--pwm-map", type=parse_pwm_map, default=DEFAULT_PWM_MAP,
                        metavar="C1,C2,C3,C4,C5,C6",
                        help="ESP32 PWM channels for Pi motors 1-6 (default: 1,2,3,4,5,6)")
    parser.add_argument("--gpiochip", type=int, help="header GPIO chip index (default: detect by label)")
    parser.add_argument("--diagnose", action="store_true", help="inspect GPIO chips/pin owners without changing pins")
    args = parser.parse_args()
    if args.bus < 0 or not 0x08 <= args.address <= 0x77:
        parser.error("Use a nonnegative bus and a 7-bit address from 0x08 to 0x77")
    if args.gpiochip is not None and args.gpiochip < 0:
        parser.error("--gpiochip must be nonnegative")
    if args.diagnose:
        try:
            return diagnose_gpio(args.gpiochip)
        except Exception as error:
            print(f"GPIO diagnostic error: {error}", file=sys.stderr)
            return 1
    try:
        from gpiozero import DigitalOutputDevice
        from smbus2 import SMBus, i2c_msg
    except ImportError:
        print("Install dependencies: sudo apt install python3-gpiozero python3-smbus2", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    pin_factory = None
    try:
        pin_factory = make_gpio_factory(args.gpiochip)
        with SMBus(args.bus) as bus:
            imu = None
            if args.gui and not args.no_imu:
                if __package__:
                    from .sen0140 import Sen0140
                else:
                    from sen0140 import Sen0140
                imu = Sen0140(bus)
            if args.trace_motors or args.pwm_map != DEFAULT_PWM_MAP:
                print("Motor PWM mapping: " + ", ".join(
                    f"M{motor + 1}=ESP32 GPIO{PWM_GPIO_PINS[channel]} (channel {channel + 1})"
                    for motor, channel in enumerate(args.pwm_map)))
            controller = MotorController(I2CPwm(bus, i2c_msg, args.address, args.pwm_map),
                                         make_output_factory(pin_factory, DigitalOutputDevice),
                                         imu=imu, trace=args.trace_motors)
            try:
                if args.gui:
                    run_gui(controller)
                    return 1 if controller.fault is not None else 0
                print(f"Six motors ready; I2C /dev/i2c-{args.bus}, address 0x{args.address:02x}.")
                print(HELP)
                while controller.fault is None:
                    command = parse_command(input("motors> "))
                    if command is None:
                        print("Invalid command.\n" + HELP)
                        continue
                    action, values = command
                    if action in ("q", "quit", "exit"):
                        break
                    if action == "help":
                        print(HELP)
                        continue
                    if action == "motor":
                        controller.set_motor(*values)
                    else:
                        controller.set_speeds((0,) * MOTOR_COUNT if action == "s" else values)
                    print("Motors 1-6:", " ".join(f"{v:+d}%" for v in controller.speeds))
                if controller.fault is not None:
                    raise OSError(str(controller.fault))
            finally:
                controller.close()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped and exited.")
    except Exception as error:
        print(f"Motor controller error: {error}", file=sys.stderr)
        return 1
    finally:
        if pin_factory is not None:
            try:
                pin_factory.close()
            except Exception as error:
                print(f"GPIO cleanup error: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
