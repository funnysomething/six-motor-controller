#!/usr/bin/env python3
"""Export trained YOLO11n weights on Linux for the IMX500 camera processor."""

import argparse
from pathlib import Path
import platform
import sys


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", required=True, type=Path, help="trained YOLO11n best.pt")
    parser.add_argument("--data", required=True, type=Path, help="local dataset YAML for quantization calibration")
    parser.add_argument("--imgsz", type=int, default=640)
    args = parser.parse_args(argv)
    for name in ("weights", "data"):
        path = getattr(args, name).expanduser().resolve()
        if not path.is_file():
            parser.error(f"{name} does not exist: {path}")
        setattr(args, name, path)
    if args.imgsz <= 0 or args.imgsz % 32:
        parser.error("--imgsz must be a positive multiple of 32")
    return args


def main(argv=None):
    args = parse_args(argv)
    if platform.system() != "Linux":
        print("IMX500 export requires Linux and Java 17+. Train YOLO11n on your Mac, "
              "then copy best.pt and the calibration dataset to Linux. "
              "See testudo_training/IMX500.md.", file=sys.stderr)
        return 1
    try:
        from ultralytics import YOLO
        from ultralytics.data.utils import check_det_dataset
        check_det_dataset(str(args.data), autodownload=False)
        model = YOLO(str(args.weights), task="detect")
        print("Exporting for the camera AI processor. YOLO26 and NCNN are unsupported here. "
              "Conversion may install additional dependencies and take time.", flush=True)
        exported = model.export(format="imx", data=str(args.data), imgsz=args.imgsz,
                                device="cpu", nms=True)
        print(f"IMX500 export: {exported}\nCopy the complete export directory to the Pi.", flush=True)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"IMX500 export failed: {error}\nUse trained YOLO11n weights on Linux with Java 17+.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
