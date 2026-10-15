#!/usr/bin/env python3
"""Train a Testudo detector for Pi CPU or IMX500 deployment."""

import argparse
from pathlib import Path
import sys


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path, help="labeled dataset's data.yaml")
    parser.add_argument("--model", choices=("yolo26n.pt", "yolo11n.pt"), default="yolo26n.pt",
                        help="YOLO26n for NCNN/Pi CPU; YOLO11n for IMX500 camera AI")
    parser.add_argument("--epochs", type=positive_int, default=100)
    parser.add_argument("--imgsz", type=positive_int, default=640)
    parser.add_argument("--batch", type=positive_int, default=8)
    parser.add_argument("--device", default="cpu", help="cpu, mps for Apple Silicon, or 0 for CUDA GPU")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent / "runs")
    parser.add_argument("--name", default="testudo")
    parser.add_argument("--test", action="store_true", help="evaluate best weights on the held-out test split")
    parser.add_argument("--export-ncnn", action="store_true", help="export the trained best.pt for Pi deployment")
    parser.add_argument("--export-imgsz", type=positive_int, help="NCNN input size, defaults to --imgsz")
    args = parser.parse_args(argv)
    if args.workers < 0:
        parser.error("--workers must be zero or greater")
    if args.imgsz % 32 or (args.export_imgsz and args.export_imgsz % 32):
        parser.error("image sizes must be multiples of 32")
    args.data = args.data.expanduser().resolve()
    if not args.data.is_file():
        parser.error(f"dataset YAML does not exist: {args.data}")
    return args


def run(args, YOLO, check_dataset):
    # Refuse dataset YAML download instructions; the user's local data must exist.
    dataset = check_dataset(str(args.data), autodownload=False)
    if dataset["names"] != {0: "testudo"}:
        raise ValueError("Expected exactly one class: names: {0: testudo}. Check your label export.")
    if args.test and not dataset.get("test"):
        raise ValueError("--test needs a held-out test split in data.yaml")
    model = YOLO(args.model, task="detect")
    model.train(
        data=str(args.data), epochs=args.epochs, imgsz=args.imgsz, batch=args.batch,
        device=args.device, workers=args.workers, project=str(args.project.expanduser().resolve()),
        name=args.name, exist_ok=False, seed=0, save=True,
    )
    best = Path(model.trainer.best)
    if not best.is_file():
        raise RuntimeError(f"Training did not produce best weights: {best}")
    print(f"Trained weights: {best.resolve()}", flush=True)
    trained = YOLO(str(best), task="detect")
    if args.test:
        trained.val(data=str(args.data), split="test", imgsz=args.imgsz,
                    batch=args.batch, device=args.device, workers=args.workers, rect=False)
    if args.export_ncnn:
        export = trained.export(format="ncnn", imgsz=args.export_imgsz or args.imgsz, device="cpu")
        print(f"NCNN model directory: {export}", flush=True)
        print("Copy the entire directory to the Pi and use the same --imgsz for inference.", flush=True)
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        from ultralytics import YOLO
        from ultralytics.data.utils import check_det_dataset
        return run(args, YOLO, check_det_dataset)
    except ImportError as error:
        print(f"Missing training dependency: {error}. Install testudo_training/requirements.txt "
              "in a training virtual environment.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"Training/evaluation/export failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
