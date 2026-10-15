#!/usr/bin/env python3
"""Control six L298N motors: Pi direction GPIO, ESP32 PWM over I2C."""

import argparse
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
I2C_ADDRESS = 0x32
REVERSE_PAUSE = 0.5
HEARTBEAT_INTERVAL = 0.2
ACK_TIMEOUT = 0.3
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


class I2CPwm:
    """Raw I2C frames; no SMBus register or length bytes are added."""

    def __init__(self, bus, message_factory, address=I2C_ADDRESS):
        self.bus = bus
        self.message = message_factory
        self.address = address
        self.sequence = secrets.randbits(32)
        # Atomic snapshot (receipt time, sensor state, millimeters), updated
        # only by acknowledged motor transactions. Tk never touches the bus.
        self.ultrasonic_sample = None

    def set_percentages(self, values):
        values = tuple(values)
        if len(values) != MOTOR_COUNT or any(
                type(v) is not int or not 0 <= v <= 100 for v in values):
            raise ValueError("PWM needs six integer percentages from 0 to 100")
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        sequence = self.sequence.to_bytes(4, "little")
        frame = bytes((0xA5, 1)) + sequence + bytes(values)
        self.bus.i2c_rdwr(self.message.write(self.address, frame + bytes((checksum(frame),))))
        deadline = time.monotonic() + ACK_TIMEOUT
        while time.monotonic() < deadline:
            # Separate write/read transactions give the ESP32 callback time to
            # apply all six channels. Its first read may contain old status.
            time.sleep(0.005)
            reply = self.message.read(self.address, 19)
            self.bus.i2c_rdwr(reply)
            status = bytes(reply)
            if (len(status) >= 14 and status[:2] == b"\x5a\x01"
                    and status[2:6] == sequence and status[6] == 0
                    and status[7:13] == bytes(values)
                    and status[13] == checksum(status[:13])):
                if (len(status) == 19 and status[14] == 0xD1
                        and status[15] in (0, 1, 2, 3)
                        and status[18] == checksum(status[:18])):
                    mm = int.from_bytes(status[16:18], "little")
                    if status[15] != 1 or 20 <= mm <= 3000:
                        self.ultrasonic_sample = (time.monotonic(), status[15], mm)
                return
        raise OSError("ESP32 did not acknowledge applied PWM; check I2C/firmware")


class MotorController:
    """Serialize direction changes and heartbeat writes for all six motors."""

    def __init__(self, pwm, output_factory, start_heartbeat=True):
        self.pwm = pwm
        self.outputs = []
        self.speeds = (0,) * MOTOR_COUNT
        self.last_directions = [0] * MOTOR_COUNT
        self.stopped_at = [0.0] * MOTOR_COUNT
        self.fault = None
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.worker = None
        self.closed = False
        try:
            for pair in DIRECTION_PINS:
                for pin in pair:
                    self.outputs.append(output_factory(pin, initial_value=False))
            self.pwm.set_percentages(self.speeds)
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

    def _fail(self, error):
        # Latch the error so a background heartbeat can never restart motion.
        self.fault = error
        self.done.set()
        self._direction_off()
        self.speeds = (0,) * MOTOR_COUNT
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
                    self.pwm.set_percentages(tuple(abs(v) for v in self.speeds))
                except OSError as error:
                    self._fail(error)
                    print(f"\nMotor link failed; all directions disabled: {error}", file=sys.stderr)
                    return

    def set_speeds(self, values):
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
                    self.pwm.set_percentages(gated)
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
                self.pwm.set_percentages(tuple(abs(v) for v in values))
                self.speeds = values
                for i, direction in enumerate(directions):
                    if direction:
                        self.last_directions[i] = direction
            except BaseException as error:
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
            if self.closed:
                return
            try:
                self.pwm.set_percentages((0,) * MOTOR_COUNT)
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
        self.values = [tk.StringVar(root, value="0") for _ in range(MOTOR_COUNT)]
        self.group_value = tk.StringVar(root, value="0")
        self.applied = [tk.StringVar(root, value="0%") for _ in range(MOTOR_COUNT)]
        self.status = tk.StringVar(root, value="Ready. All motors stopped.")
        self.distance = tk.StringVar(root, value="Ultrasonic: waiting for ESP32 readings...")
        root.title("Six motor control")
        root.minsize(620, 450)
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
        ttk.Label(frame, textvariable=self.distance, font=("TkDefaultFont", 14)).grid(
            row=13, column=0, columnspan=4, sticky="w", pady=6)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda event: self.stop())
        root.after(50, self.poll)

    def copy_group(self):
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
        if self.busy or self.closing or self.controller.fault is not None:
            return
        try:
            values = tuple(int(variable.get()) for variable in self.values)
            if any(not -100 <= value <= 100 for value in values):
                raise ValueError
        except ValueError:
            self.status.set("Use integer speeds from -100 to 100 for all six motors.")
            return
        self.send(values)

    def send(self, values):
        self.busy = True
        self.set_controls(False)
        self.status.set("Stopping all motors..." if not any(values) else "Applying speeds...")

        def request():
            try:
                self.controller.set_speeds(values)
                self.results.put(None)
            except Exception as error:
                self.results.put(error)

        self.worker = threading.Thread(target=request, daemon=True)
        self.worker.start()

    def stop(self):
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
            widget.configure(state="normal" if enabled else "disabled")

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
            self.status.set(f"Motor link failed; directions disabled: {self.controller.fault}")
        self.update_distance()
        self.set_controls(not self.busy and not self.closing and self.controller.fault is None)
        self.root.after(50, self.poll)

    def update_distance(self):
        sample = getattr(self.controller, "ultrasonic_sample", None)
        if self.controller.fault is not None:
            text = "Ultrasonic: unavailable (motor link failed)"
        elif sample is None:
            text = "Ultrasonic: unavailable (waiting for sensor firmware)"
        else:
            received_at, state, mm = sample
            if time.monotonic() - received_at > 1.0:
                text = "Ultrasonic: stale (no recent readings)"
            elif state == 1:
                text = f"Ultrasonic: {mm / 10:.1f} cm"
            elif state == 2:
                text = "Ultrasonic: no underwater echo / outside 2-300 cm range"
            elif state == 3:
                text = "Ultrasonic: no valid UART response (check RX/TX, power and baud rate)"
            else:
                text = "Ultrasonic: waiting for first measurement..."
        self.distance.set(text)

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
            controller = MotorController(I2CPwm(bus, i2c_msg, args.address),
                                         make_output_factory(pin_factory, DigitalOutputDevice))
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
