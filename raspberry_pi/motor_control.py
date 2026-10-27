#!/usr/bin/env python3
"""Control six PWM+DIR H-bridges: Pi direction GPIO, ESP32 PWM over I2C."""

import argparse
import math
from pathlib import Path
import platform
import queue
import signal
import subprocess
import sys
import threading
import time

# BCM DIR GPIOs, Motor1 through Motor6: HIGH forward, LOW reverse.
# Physical pins: 37, 35, 33, 31, 29, 23, per the updated Pi diagram.
DIRECTION_PINS = (26, 19, 13, 6, 5, 11)
MOTOR_COUNT = 6
PWM_GPIO_PINS = (4, 13, 12, 16, 17, 18)  # ESP32 firmware channel order.
# Confirmed wiring: logical Pi motors 1-6 use firmware PWM channels 4,5,6,1,2,3.
DEFAULT_PWM_MAP = (3, 4, 5, 0, 1, 2)
I2C_ADDRESS = 0x32
ULTRASONIC_STALE_SECONDS = 1.0
HEADER_CHIP_LABELS = {"pinctrl-bcm2835", "pinctrl-bcm2711", "pinctrl-rp1"}
MOTOR_GPIO_PINS = sorted(DIRECTION_PINS)


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
        for motor, pin in enumerate(DIRECTION_PINS, 1):
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
            print("  All six motor line names match BCM GPIO offsets.")
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
          "Also check old UART3/5 overlays: they overlap DIR GPIO5 and GPIO13 on Pi 4.\n"
          "Close other motor programs.\n"
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
            motor = DIRECTION_PINS.index(pin) + 1
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
    if sorted(channels) != list(range(MOTOR_COUNT)):
        raise argparse.ArgumentTypeError("--pwm-map must contain each channel 1 through 6 exactly once.")
    return channels


# Keep the public import location stable for programs importing motor_control.
if __package__:
    from .control_transport import I2CPwm
    from .control_runtime import MotorController
    from .stabilization import VehicleConfig
else:
    from control_transport import I2CPwm
    from control_runtime import MotorController
    from stabilization import VehicleConfig


HELP = """m 60                  all six motors forward at 60%
m -60                 all six motors reverse at 60%
m 1 60                Motor1 at 60%; keep the other five settings
m 60 -60 40 40 0 75    individual speeds for Motors 1 through 6
s                     stop all six motors (zero PWM)
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

    def __init__(self, root, controller, camera=None):
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.controller = controller
        self.camera = camera
        self.camera_image = None
        self.camera_seen = None
        self.results = queue.Queue()
        self.worker = None
        self.busy = False
        self.stop_requested = False
        self.closing = False
        self.shutdown_requested = threading.Event()
        self.controls = []
        self.auto_enabled = tk.BooleanVar(root, value=False)
        self.clearance_target = tk.StringVar(root, value="")
        self.heading_target = tk.StringVar(root, value="")
        self.target_notice = tk.StringVar(root, value="Enabling captures current clearance and heading.")
        self.stabilization_status = tk.StringVar(root, value="Stabilization stopped")
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
        root.title("Six motor control and Testudo camera")
        root.minsize(920, 450)
        shell = ttk.Frame(root)
        shell.grid(sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(shell, padding=(16, 8))
        toolbar.grid(row=0, column=0, sticky="ew")
        tk.Button(toolbar, text="STOP ALL MOTORS", command=self.stop, bg="#b42318", fg="white",
                  activebackground="#8f1c13", activeforeground="white", padx=16).pack(side="left")
        ttk.Label(toolbar, textvariable=self.status, wraplength=650).pack(side="left", padx=12)
        self.tabs = ttk.Notebook(shell)
        self.tabs.grid(row=1, column=0, sticky="nsew")
        frame = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(frame, text="Motors and sensors")
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
        auto_frame = ttk.LabelFrame(frame, text="Clearance and attitude stabilization", padding=8)
        auto_frame.grid(row=2, column=4, rowspan=10, sticky="new", padx=(18, 0))
        self.auto_toggle = ttk.Checkbutton(auto_frame, text="Enable automatic control",
                                          variable=self.auto_enabled, command=self.toggle_auto)
        self.auto_toggle.grid(row=0, column=0, columnspan=2, sticky="w")
        config = getattr(controller, "config", None)
        setup = (f"Downward sensor {config.depth_sensor}; PWM cap {config.pwm_limit:g}%; "
                 f"reversal {config.reversal_seconds:g} s; verified={config.verified}"
                 if config else "Load a verified --vehicle-config to enable stabilization.")
        ttk.Label(auto_frame, text=setup, wraplength=380).grid(row=1, column=0, columnspan=2, sticky="w", pady=6)
        for row, label, variable in ((2, "Clearance target (cm)", self.clearance_target),
                                      (3, "Heading target (deg)", self.heading_target)):
            ttk.Label(auto_frame, text=label).grid(row=row, column=0, sticky="w")
            ttk.Entry(auto_frame, textvariable=variable, width=10).grid(row=row, column=1, pady=3)
        ttk.Button(auto_frame, text="Apply targets", command=self.apply_targets).grid(row=4, column=0, sticky="w")
        ttk.Button(auto_frame, text="Recapture heading", command=self.recapture_heading).grid(row=4, column=1)
        ttk.Label(auto_frame, textvariable=self.target_notice, wraplength=380).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(auto_frame, textvariable=self.stabilization_status, wraplength=380).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(auto_frame, text="M1–2 yaw; M3–5 clearance, roll and pitch; M6 stopped. "
                  "Roll/pitch targets are calibrated level. Compass faults retain yaw damping. "
                  "Clearance is height above the bottom, not depth below the surface.",
                  wraplength=380).grid(row=7, column=0, columnspan=2, sticky="w")
        imu_frame = ttk.LabelFrame(frame, text="SEN0140 V2 IMU", padding=8)
        imu_frame.grid(row=12, column=4, rowspan=4, sticky="new", padx=(18, 0), pady=(8, 0))
        for row, variable in enumerate((*self.imu_values.values(), self.imu_tilt)):
            ttk.Label(imu_frame, textvariable=variable, wraplength=330).grid(
                row=row, column=0, sticky="w", pady=2)
        camera_frame = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(camera_frame, text="Testudo camera")
        self.camera_tab = camera_frame
        self.camera_status = tk.StringVar(root, value="Camera disabled (--no-camera)." if camera is None
                                         else "Camera ready to start.")
        ttk.Label(camera_frame, text="Testudo detection", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(camera_frame, textvariable=self.camera_status, wraplength=820).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=8)
        self.camera_preview = ttk.Label(camera_frame, text="Waiting for camera...", anchor="center")
        self.camera_preview.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=8)
        camera_frame.columnconfigure(0, weight=1)
        camera_frame.rowconfigure(2, weight=1)
        camera_buttons = ttk.Frame(camera_frame)
        camera_buttons.grid(row=3, column=0, sticky="w")
        self.camera_start = ttk.Button(camera_buttons, text="Start camera", command=self.start_camera)
        self.camera_start.pack(side="left")
        self.camera_stop = ttk.Button(camera_buttons, text="Stop camera", command=self.stop_camera)
        self.camera_stop.pack(side="left", padx=8)
        if camera is None:
            self.camera_start.configure(state="disabled")
        self.camera_stop.configure(state="disabled")
        ttk.Label(camera_frame, text="Detection shows Testudo boxes and confidence. "
                  "It does not command motors. STOP ALL MOTORS remains available on either tab.",
                  wraplength=820).grid(row=4, column=0, columnspan=2, sticky="w", pady=12)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda event: self.stop())
        root.after(50, self.poll)
        if camera is not None:
            root.after(0, self.start_camera)

    def start_camera(self):
        if self.camera is not None and not self.closing:
            self.camera_seen = None
            self.camera.start()
            self.update_camera()

    def stop_camera(self):
        if self.camera is not None:
            self.camera.stop()
            self.update_camera()

    def update_camera(self):
        if self.camera is None:
            return
        import tkinter as tk

        self.camera.poll()
        running = self.camera.running
        stopping = self.camera.stopping_at is not None
        self.camera_start.configure(state="disabled" if running or self.closing else "normal")
        self.camera_stop.configure(state="normal" if running and not stopping and not self.closing else "disabled")
        text = self.camera.status
        latest = self.camera.latest
        if not running or stopping:
            self.camera_preview.configure(image="", text=text)
            self.camera_image = None
        elif latest is not None:
            received_at, message = latest
            age = time.monotonic() - received_at
            if age > 3:
                text = f"Camera feed stale ({age:.0f} s since last frame). {text}"
                self.camera_preview.configure(image="", text="Camera feed stale; waiting for frames...")
                self.camera_image = None
            elif received_at != self.camera_seen:
                try:
                    photo = tk.PhotoImage(master=self.root, data=message["image"], format="PPM")
                    self.camera_preview.configure(image=photo, text="")
                    self.camera_image = photo
                    self.camera_seen = received_at
                except (tk.TclError, KeyError) as error:
                    text = f"Camera preview error: {error}"
        self.camera_status.set(text)

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
        if (self.auto_enabled.get() or self.busy or self.closing
                or getattr(self.controller, "recovering", False) or self.controller.fault is not None):
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
        if (self.busy or self.closing or getattr(self.controller, "recovering", False)
                or self.controller.fault is not None):
            self.auto_enabled.set(getattr(self.controller, "automatic", False))
            return
        if not self.auto_enabled.get():
            self.stop()
            return
        for variable in self.values:
            variable.set(0)
        self.send((0,) * MOTOR_COUNT, automatic=True)

    def apply_targets(self):
        try:
            clearance = float(self.clearance_target.get()) / 100.
            stabilizer = getattr(self.controller, "stabilizer", None)
            heading = (float(self.heading_target.get()) if stabilizer is not None
                       and stabilizer.yaw_mode == "heading" else None)
            self.controller.set_targets(clearance, heading)
            self.target_notice.set("Targets accepted; changes are slew limited.")
        except (ValueError, OSError) as error:
            self.target_notice.set(str(error))

    def recapture_heading(self):
        try:
            self.controller.recapture_heading()
            self.heading_target.set(f"{self.controller.stabilizer.heading_target:.1f}")
            self.target_notice.set("Heading recaptured.")
        except (ValueError, OSError) as error:
            self.target_notice.set(str(error))

    def send(self, values, automatic=False):
        self.busy = True
        self.set_controls(False)
        self.status.set("Enabling stabilization..." if automatic else
                        "Stopping all motors..." if not any(values) else "Requesting speeds...")

        def request():
            try:
                if automatic:
                    self.controller.enable_stabilization()
                else:
                    self.controller.set_speeds(values)
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
            self.status.set(f"Motor link failed; PWM stopped: {self.controller.fault}")
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
                self.status.set("Request accepted; awaiting motor acknowledgement.")
                stabilizer = getattr(self.controller, "stabilizer", None)
                if getattr(self.controller, "automatic", False):
                    self.clearance_target.set(f"{stabilizer.target*100:.1f}")
                    self.heading_target.set(f"{stabilizer.heading_target:.1f}")
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
            self.status.set(f"Motor link failed; PWM stopped: {self.controller.fault}")
        elif not self.closing and getattr(self.controller, "link_notice", ""):
            self.status.set(self.controller.link_notice)
        elif self.auto_enabled.get() and not self.busy and not self.closing:
            if getattr(self.controller, "automatic", False):
                self.status.set("Automatic clearance and attitude control active.")
            else:
                self.auto_enabled.set(False)
                self.status.set(getattr(self.controller, "control_notice", "Automatic control stopped"))
        detail = getattr(self.controller, "control_notice", "Stabilization unavailable")
        attitude = getattr(self.controller, "attitude", None)
        if attitude is not None:
            magnetic_age = (f"{max(0., time.monotonic()-attitude.last_magnetic_at):.1f} s"
                            if attitude.last_magnetic_at is not None else "never")
            detail += (f"\nMagnetic data {'accepted' if attitude.magnetic_ok else 'unavailable/rejected'}; "
                       f"last trusted: {magnetic_age}.")
        stabilizer = getattr(self.controller, "stabilizer", None)
        if stabilizer is not None and stabilizer.allocator.limited:
            detail += "\nThrust saturated; roll/pitch take priority over vertical effort."
        self.stabilization_status.set(detail)
        self.update_distance()
        self.update_imu()
        self.update_camera()
        output_problem = self.update_outputs()
        if (output_problem and self.controller.fault is None and not self.busy
                and not self.closing and not getattr(self.controller, "recovering", False)):
            self.status.set(output_problem)
        self.set_controls(not self.busy and not self.closing and self.controller.fault is None
                          and not getattr(self.controller, "recovering", False))
        self.root.after(50, self.poll)

    def update_outputs(self):
        sample = getattr(self.controller, "output_sample", None)
        if sample is None:
            return None
        speeds, directions, error = sample
        ack_at = getattr(self.controller, "last_pwm_ack_at", None)
        age = time.monotonic() - ack_at if ack_at is not None else None
        ack_text = f"{age:.1f} s ago" if age is not None else "unavailable"
        pins = (" ".join(f"M{motor + 1}={direction}"
                         for motor, direction in enumerate(directions))
                if directions is not None else f"read error: {error}")
        requested = getattr(self.controller, "requested", speeds)
        coasting = getattr(self.controller, "coasting", (False,)*6)
        pending = ", ".join(str(i+1) for i, value in enumerate(coasting) if value) or "none"
        self.output_status.set(f"Requested: {requested}. Reversing motors: {pending}\nPWM acknowledged: {ack_text}. Pi direction GPIO: {pins}\n"
                               "DIR 1 = forward; DIR 0 = reverse; zero PWM = stopped. Feedback does not measure rotation.")
        if age is None or age > 1.0:
            return "Motor updates stale; the ESP32 watchdog may have stopped PWM. Check terminal output."
        expected = tuple(int(speed > 0) for speed in speeds)
        if directions is None or any(speed and actual != wanted for speed, actual, wanted
                                     in zip(speeds, directions, expected)):
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
        elif getattr(self.controller, "recovering", False):
            text = "unavailable (I2C reconnecting)"
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
        if self.camera is not None:
            self.camera.stop()
        self.set_controls(False)
        self.status.set("Stopping and closing...")
        if not self.busy:
            self.root.destroy()  # main's finally stops motors before closing I2C.

def run_gui(controller, camera=None):
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
        app = MotorGui(root, controller, camera=camera)
        # Tk callbacks can swallow KeyboardInterrupt; request a normal GUI
        # shutdown from the next poll instead, without touching Tk in a handler.
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous_handlers[signum] = signal.signal(
                    signum, lambda signum, frame: app.shutdown_requested.set())
        root.mainloop()
    finally:
        if camera is not None:
            camera.stop()
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
    parser.add_argument("--vehicle-config", type=Path, help="verified vehicle calibration/configuration JSON")
    parser.add_argument("--control-log", type=Path, help="write raw IMU and control diagnostic JSONL")
    parser.add_argument("--no-imu", action="store_true", help="disable SEN0140 polling in the GUI")
    parser.add_argument("--no-camera", action="store_true", help="disable automatic camera startup in the GUI")
    parser.add_argument("--camera-backend", choices=("auto", "imx500", "ncnn", "pytorch"), default="auto",
                        help="auto selects IMX500 when its export exists, otherwise trained best.pt on the Pi CPU")
    parser.add_argument("--camera-model", type=Path, help="trained .pt file or camera model export directory")
    parser.add_argument("--camera-python", help="camera worker Python (default: matching repo vision environment)")
    parser.add_argument("--camera-conf", type=float, default=0.5, help="camera detection confidence threshold")
    parser.add_argument("--camera-fps", type=int, default=16, help="requested camera FPS")
    parser.add_argument("--trace-motors", action="store_true",
                        help="log applied PWM, direction GPIO readback and acknowledgement age")
    parser.add_argument("--pwm-map", type=parse_pwm_map, default=DEFAULT_PWM_MAP,
                        metavar="C1,C2,C3,C4,C5,C6",
                        help="ESP32 PWM channels for Pi motors 1-6 (default: 4,5,6,1,2,3)")
    parser.add_argument("--gpiochip", type=int, help="header GPIO chip index (default: detect by label)")
    parser.add_argument("--diagnose", action="store_true", help="inspect GPIO chips/pin owners without changing pins")
    args = parser.parse_args()
    if args.bus < 0 or not 0x08 <= args.address <= 0x77:
        parser.error("Use a nonnegative bus and a 7-bit address from 0x08 to 0x77")
    if args.gpiochip is not None and args.gpiochip < 0:
        parser.error("--gpiochip must be nonnegative")
    if not math.isfinite(args.camera_conf) or not 0 <= args.camera_conf <= 1 or args.camera_fps <= 0:
        parser.error("--camera-conf must be 0-1 and --camera-fps must be positive")
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
    camera = None
    control_log = None
    try:
        config = VehicleConfig.load(args.vehicle_config) if args.vehicle_config else None
        if args.control_log:
            control_log = args.control_log.open("a", buffering=1)
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
                                         imu=imu, trace=args.trace_motors, config=config, log=control_log)
            try:
                if args.gui:
                    if not args.no_camera:
                        if __package__:
                            from .camera_control import CameraConfig, CameraSession
                        else:
                            from camera_control import CameraConfig, CameraSession
                        camera = CameraSession(CameraConfig(args.camera_backend, args.camera_model,
                                                            args.camera_python, args.camera_conf, args.camera_fps))
                    run_gui(controller, camera=camera)
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
                    print("Requested Motors 1-6:", " ".join(f"{v:+d}%" for v in controller.requested))
                if controller.fault is not None:
                    raise OSError(str(controller.fault))
            finally:
                try:
                    controller.close()
                finally:
                    if camera is not None:
                        camera.close()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped and exited.")
    except Exception as error:
        print(f"Motor controller error: {error}", file=sys.stderr)
        return 1
    finally:
        if control_log is not None:
            control_log.close()
        if pin_factory is not None:
            try:
                pin_factory.close()
            except Exception as error:
                print(f"GPIO cleanup error: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
