#!/usr/bin/env python3
"""Install a compatible Matplotlib inside the separate IMX500 environment."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import subprocess
import sys

ENVIRONMENT = Path(__file__).resolve().parent / ".venv-imx500"


def main():
    if Path(sys.prefix).resolve() != ENVIRONMENT.resolve():
        print("Run ./scripts/vision/repair-imx500-plotting.sh so only .venv-imx500 is modified.",
              file=sys.stderr)
        return 1
    try:
        try:
            installed = version("matplotlib")
        except PackageNotFoundError:
            installed = "not installed"
        print(f"Camera Python: {sys.executable}\nMatplotlib before repair: {installed}", flush=True)
        # Shadow inherited apt packages locally; do not uninstall OS packages or
        # let the resolver replace NumPy, OpenCV, Torch, or Sony modlib.
        subprocess.run([
            sys.executable, "-m", "pip", "install", "--ignore-installed", "--no-deps",
            "--only-binary=:all:", "--index-url", "https://pypi.org/simple", "matplotlib==3.9.4",
            "pyparsing>=2.3.1",
        ], check=True)
        checks = [
            ("Matplotlib render", "import matplotlib; matplotlib.use('Agg'); "
             "import matplotlib._docstring as docs; import matplotlib.pyplot as plt; "
             "from io import BytesIO; "
             "print(matplotlib.__version__, matplotlib.__file__, docs.__file__, flush=True); "
             "assert hasattr(docs, 'dedent_interpd'); "
             "fig, ax = plt.subplots(); ax.plot([0,1],[0,1]); "
             "fig.savefig(BytesIO(), format='png'); plt.close(fig)"),
            ("Sony annotation import", "from modlib.apps import Annotator; "
             "print('Sony annotation import passed', flush=True)"),
        ]
        for label, code in checks:
            print(f"Checking {label}...", flush=True)
            subprocess.run([sys.executable, "-u", "-c", code], check=True)
        print("Plotting checks passed. Retry ./scripts/vision/detect-imx500.sh", flush=True)
        return 0
    except subprocess.CalledProcessError as error:
        print(f"Plotting repair/check failed with status {error.returncode}. "
              "Preserve the traceback above; other vision environments were not modified.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
