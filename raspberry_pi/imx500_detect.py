#!/usr/bin/env python3
"""Run an exported Testudo YOLO11n detector on the IMX500 AI processor."""

import argparse
import math
import os
from pathlib import Path
import signal
import sys
import time


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, type=Path,
                        help="IMX export containing packerOut.zip and labels.txt, not NCNN")
    parser.add_argument("--conf", type=float, default=0.5)
    parser.add_argument("--fps", type=int, default=16, help="requested camera FPS, not an inference guarantee")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--headless", action="store_true", help="log detections without a preview window")
    args = parser.parse_args(argv)
    args.model_dir = args.model_dir.expanduser().resolve()
    for name in ("packerOut.zip", "labels.txt"):
        if not (args.model_dir / name).is_file():
            parser.error(f"Missing {name} in {args.model_dir}. Use an IMX500 export, not NCNN or .pt.")
    if not math.isfinite(args.conf) or not 0 <= args.conf <= 1:
        parser.error("--conf must be between 0 and 1")
    if args.fps <= 0 or args.seconds <= 0:
        parser.error("--fps and --seconds must be greater than zero")
    if not args.headless and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        parser.error("preview requires the Pi desktop; use --headless over SSH")
    return args


def run(args, AiCamera, Model, MODEL_TYPE, COLOR_FORMAT, postprocess, Annotator):
    labels = (args.model_dir / "labels.txt").read_text().splitlines()
    if len(labels) != 1 or labels[0].strip().casefold() != "testudo":
        raise ValueError("Expected exactly one Testudo label in labels.txt; check the trained model export")

    class TestudoModel(Model):
        def __init__(self):
            super().__init__(model_file=str(args.model_dir / "packerOut.zip"),
                             model_type=MODEL_TYPE.CONVERTED,
                             color_format=COLOR_FORMAT.RGB, preserve_aspect_ratio=False)
            self.labels = labels

        def post_process(self, tensors):
            return postprocess(tensors)

    device = AiCamera(frame_rate=args.fps, timeout=args.seconds, headless=args.headless)
    print("Deploying YOLO11n to the IMX500. Initial packaging/firmware loading can take minutes.", flush=True)
    model = TestudoModel()
    device.deploy(model)
    annotator = None if args.headless else Annotator()
    frames = 0
    next_log = 0
    print("Network inference runs on the CAMERA; result decoding and preview run on the PI. "
          "Ctrl+C stops.", flush=True)
    with device as stream:
        begin = time.monotonic()
        for frame in stream:
            frames += 1
            detections = frame.detections
            selected = detections[detections.confidence >= args.conf]
            captions = [f"{labels[int(class_id)]} {float(score):.2f}"
                        for _, score, class_id, _ in selected]
            now = time.monotonic()
            if now >= next_log:
                # A camera image may reuse detections; don't call display rate inference FPS.
                print(f"Stream {frames / max(now - begin, 0.001):.1f} FPS | "
                      f"objects: {', '.join(captions) or 'none'}", flush=True)
                next_log = now + 1
            if annotator is not None:
                annotator.annotate_boxes(frame, selected, labels=captions)
                frame.display()
    return 0


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    args = parse_args(argv)
    for name in ("SIGTERM", "SIGHUP"):
        signal.signal(getattr(signal, name), interrupted)
    try:
        from modlib.apps import Annotator
        from modlib.devices import AiCamera
        from modlib.models import COLOR_FORMAT, MODEL_TYPE, Model
        from modlib.models.post_processors import pp_od_yolo_ultralytics
        return run(args, AiCamera, Model, MODEL_TYPE, COLOR_FORMAT, pp_od_yolo_ultralytics, Annotator)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"IMX500 detection failed: {error}\nSee testudo_training/IMX500.md for Sony modlib setup.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
