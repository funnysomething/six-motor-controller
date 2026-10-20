#!/usr/bin/env python3
"""Export an existing trained checkpoint to NCNN without repeating training."""

import argparse
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", required=True, type=Path)
    parser.add_argument("--imgsz", type=int, default=640)
    args = parser.parse_args(argv)
    weights = args.weights.expanduser().resolve()
    if not weights.is_file():
        parser.error(f"weights do not exist: {weights}")
    if args.imgsz <= 0 or args.imgsz % 32:
        parser.error("--imgsz must be a positive multiple of 32")
    # Ultralytics exports beside best.pt. Preserve any prior model at another input size.
    destination = weights.parent / (weights.stem + "_ncnn_model")
    if destination.exists():
        parser.error(f"Export already exists: {destination}. Preserve it and export from a "
                     "checkpoint copied to a different directory inside the repo.")
    try:
        from ultralytics import YOLO
        result = YOLO(str(weights), task="detect").export(format="ncnn", imgsz=args.imgsz, device="cpu")
        print(f"NCNN export: {result}")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"NCNN export failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
