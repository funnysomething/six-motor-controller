#!/usr/bin/env python3
"""Launch repository vision configurations without manually activating environments."""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import shlex
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
TRAIN_ENV = ROOT / "testudo_training/.venv"
PI_ENVS = [ROOT / "raspberry_pi/.venv-pi4", ROOT / "raspberry_pi/.venv"]
IMX_ENV = ROOT / "raspberry_pi/.venv-imx500"
DATA = ROOT / "datasets/testudo/data.yaml"
RUNS = ROOT / "testudo_training/runs"
PROFILES = (
    "setup-training", "setup-pi-cpu", "setup-imx500", "preview", "capture", "capture-negatives",
    "train-yolo26", "train-yolo26-fast", "train-yolo11", "export-ncnn", "export-imx500",
    "detect-ncnn", "detect-yolo26", "detect-yolo11", "detect-imx500", "benchmark",
)


def option(arguments, flag, default):
    """Read the final override so preflight checks use the actual selected model."""
    value = default
    for index, arg in enumerate(arguments):
        if arg == flag and index + 1 < len(arguments):
            value = arguments[index + 1]
        elif arg.startswith(flag + "="):
            value = arg.split("=", 1)[1]
    return value


def repo_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def environment_python(role, dry_run):
    if role == "training":
        candidates = [TRAIN_ENV]
    elif role == "imx":
        candidates = [IMX_ENV]
    elif role == "export":
        candidates = [TRAIN_ENV, *PI_ENVS]
    else:
        candidates = PI_ENVS
    for env in candidates:
        python = env / "bin/python"
        if python.is_file():
            return python
    if dry_run:
        return candidates[0] / "bin/python"
    raise RuntimeError(f"No {role} environment found. Expected {candidates[0]}. "
                       "See scripts/vision/README.md for setup.")


def require(path, dry_run):
    if not dry_run and not path.exists():
        raise RuntimeError(f"Missing {path}. Copy, train, or export this asset first. "
                           "Use --model/--model-dir/--weights to select another existing model.")


def ncnn_size(directory):
    """Read the exported input size without importing training/native dependencies."""
    metadata = directory / "metadata.yaml"
    if not metadata.is_file():
        return None
    lines = metadata.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.startswith("imgsz:"):
            value = line.partition(":")[2].strip()
            if value:
                sizes = value.strip("[]").split(",")
            else:
                sizes = []
                for following in lines[index + 1:]:
                    if not following.startswith("- ") and not following.startswith("  - "):
                        break
                    sizes.append(following.strip().removeprefix("- "))
            try:
                numbers = [int(size.strip()) for size in sizes]
            except ValueError:
                return None
            if len(numbers) == 2 and numbers[0] == numbers[1] and numbers[0] > 0:
                return numbers[0]
    return None


def build_command(profile, extra, dry_run=False):
    system_python = "/usr/bin/python3" if platform.system() == "Linux" else "python3"
    if profile == "preview":
        return [system_python, str(ROOT / "raspberry_pi/camera_test.py"), "--mode", "camera",
                "--preview", "--seconds", "120", *extra]
    if profile.startswith("capture"):
        command = [system_python, str(ROOT / "raspberry_pi/dataset_capture/capture.py"),
                   "--preview", "--manual"]
        if profile == "capture-negatives":
            command += ["--kind", "negative"]
        return command + extra
    if profile.startswith("train-"):
        fast = profile == "train-yolo26-fast"
        model = "yolo11n.pt" if profile == "train-yolo11" else "yolo26n.pt"
        defaults = {"train-yolo26": "testudo", "train-yolo26-fast": "testudo_fast",
                    "train-yolo11": "testudo_imx500"}
        device = "mps" if platform.system() == "Darwin" and platform.machine() == "arm64" else "cpu"
        return [str(environment_python("training", dry_run)), str(ROOT / "testudo_training/train.py"),
                "--data", str(DATA), "--model", model, "--device", device,
                "--imgsz", "320" if fast else "640", "--batch", "16" if fast else "4",
                "--epochs", "50" if fast else "100", "--name", defaults[profile], *extra]
    if profile == "export-ncnn":
        weights = repo_path(option(extra, "--weights", str(RUNS / "testudo-3/weights/best.pt")))
        require(weights, dry_run)
        # Export is separate so completed training is never repeated just to convert weights.
        script = ROOT / "testudo_training/export_ncnn.py"
        return [str(environment_python("export", dry_run)), str(script),
                "--weights", str(weights), "--imgsz", "640", *extra]
    if profile == "export-imx500":
        if not dry_run and platform.system() != "Linux":
            raise RuntimeError("IMX500 export requires Linux and Java 17+. Copy/clone the whole repo "
                               "to Linux, then run this same script there.")
        weights = repo_path(option(extra, "--weights", str(RUNS / "testudo_imx500/weights/best.pt")))
        require(weights, dry_run)
        return [str(environment_python("export", dry_run)), str(ROOT / "testudo_training/export_imx500.py"),
                "--weights", str(weights), "--data", str(DATA), "--imgsz", "640", *extra]
    if profile == "detect-imx500":
        model = repo_path(option(extra, "--model-dir", str(RUNS / "testudo_imx500/weights/best_imx_model")))
        for filename in ("packerOut.zip", "labels.txt"):
            require(model / filename, dry_run)
        return [str(environment_python("imx", dry_run)), str(ROOT / "raspberry_pi/imx500_detect.py"),
                "--model-dir", str(model), "--seconds", "120", *extra]
    if profile == "benchmark":
        return [str(environment_python("pi", dry_run)), str(ROOT / "raspberry_pi/compare_yolo.py"),
                "--imgsz", "320", "--frames", "60", "--repeats", "2", *extra]
    defaults = {
        "detect-ncnn": RUNS / "testudo-3/weights/best_ncnn_model",
        "detect-yolo26": RUNS / "testudo-3/weights/best.pt",
        "detect-yolo11": RUNS / "testudo_imx500/weights/best.pt",
    }
    model = repo_path(option(extra, "--model", str(defaults[profile])))
    require(model, dry_run)
    size = ncnn_size(model) if model.is_dir() else None
    requested_size = option(extra, "--imgsz", str(size or 640))
    if size and str(size) != requested_size:
        raise RuntimeError(f"NCNN export input is {size}; --imgsz {requested_size} does not match. "
                           "Re-export at the desired size into a separate model directory.")
    return [str(environment_python("pi", dry_run)), str(ROOT / "raspberry_pi/camera_test.py"),
            "--mode", "yolo", "--model", str(model), "--imgsz", requested_size,
            "--preview", "--seconds", "120", *extra]


def execute(command, dry_run, log_path=None):
    print(f"Working directory: {ROOT}\n{shlex.join(map(str, command))}", flush=True)
    if dry_run:
        return 0
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONFAULTHANDLER"] = "1"
    # Respect the existing Pi compatibility environment's dependency pins.
    constraints = Path(command[0]).parent.parent / "vision-constraints.txt"
    if constraints.is_file():
        environment["PIP_CONSTRAINT"] = str(constraints)
    else:
        environment.pop("PIP_CONSTRAINT", None)
    if log_path is None:
        result = subprocess.call(command, cwd=ROOT, env=environment)
    else:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Saving export output to {log_path}", flush=True)
        with log_path.open("x") as log:
            log.write(f"Working directory: {ROOT}\n{shlex.join(map(str, command))}\n")
            log.flush()
            with subprocess.Popen(command, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1) as process:
                try:
                    for line in process.stdout:
                        print(line, end="", flush=True)
                        log.write(line)
                        log.flush()
                    result = process.wait()
                except KeyboardInterrupt:
                    # The terminal sends Ctrl+C to the whole foreground group. Give
                    # Python time to finish, then terminate a stuck converter.
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    raise
            log.write(f"\nProcess return code: {result}\n")
    if result < 0:
        number = -result
        try:
            name = signal.Signals(number).name
        except ValueError:
            name = str(number)
        print(f"Process terminated by {name} (signal {number}).", file=sys.stderr, flush=True)
        if number == signal.SIGKILL:
            print("SIGKILL may mean the kernel ran out of memory, or another process killed it. "
                  "Check: sudo dmesg -T | tail -n 80", file=sys.stderr, flush=True)
        elif number in (signal.SIGSEGV, signal.SIGILL, signal.SIGABRT):
            print("A native dependency crashed. Preserve the export log and check the package "
                  "versions and CPU architecture before reinstalling anything.", file=sys.stderr, flush=True)
        return 128 + number
    if result:
        print(f"Process exited with status {result}.", file=sys.stderr, flush=True)
    return result


def setup(profile, dry_run):
    if profile == "setup-pi-cpu":
        if not dry_run and (platform.system() != "Linux" or platform.machine() != "aarch64"):
            raise RuntimeError("Run setup-pi-cpu on the 64-bit Raspberry Pi.")
        commands = [
            ["sudo", "apt", "update"],
            ["sudo", "apt", "install", "-y", "imx500-all", "python3-venv", "python3-picamera2", "python3-opencv"],
        ]
        if not (PI_ENVS[0] / "bin/python").is_file():
            commands.append(["/usr/bin/python3", str(ROOT / "raspberry_pi/setup_pi4_vision.py")])
        commands.append([str(PI_ENVS[0] / "bin/python"), str(ROOT / "raspberry_pi/setup_ncnn.py")])
    elif profile == "setup-imx500":
        if not dry_run and platform.system() != "Linux":
            raise RuntimeError("Run setup-imx500 on the Raspberry Pi, not macOS.")
        commands = [
            ["sudo", "apt", "update"],
            ["sudo", "apt", "install", "-y", "imx500-all", "python3-venv", "python3-opencv", "python3-picamera2"],
            ["python3", "-m", "venv", "--system-site-packages", str(IMX_ENV)],
            [str(IMX_ENV / "bin/python"), "-m", "pip", "install", "--upgrade", "pip"],
            [str(IMX_ENV / "bin/python"), "-m", "pip", "install",
             "git+https://github.com/SonySemiconductorSolutions/aitrios-rpi-application-module-library.git"],
        ]
    else:
        commands = [
            ["python3", "-m", "venv", str(TRAIN_ENV)],
            [str(TRAIN_ENV / "bin/python"), "-m", "pip", "install", "-r",
             str(ROOT / "testudo_training/requirements.txt")],
        ]
    for command in commands:
        if "venv" in command and "-m" in command and (Path(command[-1]) / "bin/python").is_file():
            print(f"Reusing environment: {command[-1]}", flush=True)
            continue
        result = execute(command, dry_run)
        if result:
            return result
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("profile", choices=PROFILES)
    parser.add_argument("--dry-run", action="store_true", help="print commands without running/installing anything")
    args, extra = parser.parse_known_args(argv)
    try:
        if args.profile.startswith("setup-"):
            if extra:
                parser.error("setup profiles do not accept extra arguments")
            return setup(args.profile, args.dry_run)
        log_path = None
        if args.profile.startswith("export-"):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            log_path = ROOT / "testudo_training/export_logs" / f"{args.profile}_{stamp}.log"
        return execute(build_command(args.profile, extra, args.dry_run), args.dry_run, log_path)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError) as error:
        print(f"Vision launcher: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
