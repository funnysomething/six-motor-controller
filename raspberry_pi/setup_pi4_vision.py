#!/usr/bin/env python3
"""Create a separate Pi 4 vision environment and test an older PyTorch build."""

import argparse
import os
from pathlib import Path
import platform
import subprocess
import sys

try:
    from .check_vision_install import PROBES
except ImportError:
    from check_vision_install import PROBES

ROOT = Path(__file__).resolve().parent
ENVIRONMENT = ROOT / ".venv-pi4"
PINS = "torch==2.7.1\ntorchvision==0.22.1\nultralytics==8.4.174\n"


def execute(command, environment=None):
    print("Running:", " ".join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), check=True, env=environment)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, default=ENVIRONMENT,
                        help="new environment directory inside the repository")
    parser.add_argument("--cpu-only", action="store_true", help="use the official PyTorch CPU wheel index")
    args = parser.parse_args(argv)
    target = args.environment.expanduser().resolve()
    if not target.is_relative_to(ROOT.parent.resolve()):
        parser.error("--environment must be inside this repository")
    if sys.platform != "linux" or platform.machine() != "aarch64":
        print("Run this script on the 64-bit Raspberry Pi, not the Mac.", file=sys.stderr)
        return 1
    if sys.version_info[:2] != (3, 13):
        print("This compatibility trial targets your Python 3.13 installation.", file=sys.stderr)
        return 1
    if target.exists():
        print(f"{target} already exists. It was preserved. Use another --environment path before retrying.", file=sys.stderr)
        return 1
    try:
        # Use the current interpreter's base Python; preserve access to apt-installed camera bindings.
        execute([sys._base_executable, "-m", "venv", "--system-site-packages", target])
        python = target / "bin" / "python"
        constraints = target / "vision-constraints.txt"
        constraints.write_text(PINS)
        environment = dict(os.environ, PIP_CONSTRAINT=str(constraints))
        command = [python, "-m", "pip", "install", "--only-binary=:all:",
                   "torch==2.7.1", "torchvision==0.22.1"]
        if args.cpu_only:
            command += ["--index-url", "https://download.pytorch.org/whl/cpu"]
        execute(command, environment)
        # Each probe runs separately, so an illegal instruction returns a failing exit code.
        for label, code in PROBES:
            if label in ("PyTorch import", "PyTorch CPU convolution", "Torchvision import and CPU NMS"):
                print(f"\nTesting {label} with the replacement build...", flush=True)
                execute([python, "-u", "-c",
                         "import resource; resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); " + code], environment)
        execute([python, "-m", "pip", "install", "ultralytics==8.4.174"], environment)
        execute([python, "-c", "from ultralytics import YOLO; print('YOLO import passed')"], environment)
        (target / "vision-ready.txt").write_text(PINS)
        print("\nCompatibility probes passed. Full YOLO inference and NCNN export still need testing.")
        print("From the raspberry_pi folder, run:")
        print(f"source {target}/bin/activate")
        print("export PIP_CONSTRAINT=\"$VIRTUAL_ENV/vision-constraints.txt\"")
        print("python camera_test.py --mode yolo --model yolo26n.pt --preview --seconds 10")
        print("Then try: python compare_yolo.py --frames 10 --repeats 1 --cooldown 0")
        return 0
    except subprocess.CalledProcessError as error:
        reason = "SIGILL (Illegal instruction)" if error.returncode == -4 else f"exit code {error.returncode}"
        print(f"\nSetup/test failed: {reason}. Your original environment is unchanged.", file=sys.stderr)
        print("Share the last test label and output. The partial new environment was preserved.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped. Your original environment is unchanged.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
