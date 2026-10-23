#!/usr/bin/env python3
"""Isolated Testudo camera inference and latest-frame transport for the motor GUI."""

import argparse
import base64
from collections import deque
from contextlib import redirect_stdout
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import traceback


ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = ROOT / "testudo_training/runs/testudo_imx500/weights"
MESSAGE_PREFIX = "TESTUDO_CAMERA "


@dataclass(frozen=True)
class CameraConfig:
    backend: str = "auto"
    model: Path | None = None
    python: str | None = None
    confidence: float = 0.5
    fps: int = 16

    def resolve(self):
        if self.backend not in ("auto", "imx500", "ncnn", "pytorch"):
            raise ValueError("Camera backend must be auto, imx500, ncnn or pytorch")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1 or self.fps <= 0:
            raise ValueError("Camera confidence must be 0-1 and FPS must be positive")
        model = Path(self.model).expanduser().resolve() if self.model else None
        backend = self.backend
        if backend == "auto":
            candidate = model or WEIGHTS / "best_imx_model"
            backend = ("imx500" if (candidate / "packerOut.zip").is_file() else
                       "pytorch" if model is None or model.suffix == ".pt" else "ncnn")
        model = model or WEIGHTS / {"imx500": "best_imx_model", "ncnn": "best_ncnn_model",
                                    "pytorch": "best.pt"}[backend]
        if backend == "pytorch":
            if model.suffix != ".pt" or not model.is_file():
                raise ValueError(f"Missing trained PyTorch weights: {model}")
            return backend, model
        required = (("packerOut.zip", "labels.txt") if backend == "imx500" else
                    ("model.ncnn.param", "model.ncnn.bin", "metadata.yaml"))
        missing = [name for name in required if not (model / name).is_file()]
        if missing:
            raise ValueError(f"Missing {', '.join(missing)} in {model}. "
                             "Use a complete IMX500 or NCNN export of the trained Testudo model.")
        return backend, model

    def command(self):
        backend, model = self.resolve()
        python = self.python
        if python is None:
            repaired = ROOT / "raspberry_pi/.venv-pi4-cpu/vision-ready.txt"
            environments = ([".venv-imx500"] if backend == "imx500" else
                            ([".venv-pi4-cpu"] if backend == "pytorch" and repaired.is_file() else [])
                            + [".venv-pi4", ".venv"])
            python = next((str(ROOT / "raspberry_pi" / env / "bin/python") for env in environments
                           if (ROOT / "raspberry_pi" / env / "bin/python").is_file()), sys.executable)
        return [str(python), "-u", str(Path(__file__).resolve()), "--worker", "--backend", backend,
                "--model", str(model), "--conf", str(self.confidence), "--fps", str(self.fps)], backend


class CameraSession:
    """No camera/native imports in the motor process; one cached frame, no backlog."""

    def __init__(self, config):
        self.config = config
        self.process = None
        self.reader = None
        self.latest = None
        self.status = "Camera stopped."
        self.error = None
        self.logs = deque(maxlen=6)
        self.stopping_at = None
        self.killed = False

    @property
    def running(self):
        # Wait for the previous reader to finish before reusing session state.
        return self.process is not None and (self.process.poll() is None or
                                            self.reader is not None and self.reader.is_alive())

    def start(self):
        if self.running:
            return
        self.latest = None
        self.error = None
        self.logs.clear()
        self.stopping_at = None
        self.killed = False
        try:
            command, backend = self.config.command()
            self.status = ("Loading IMX500 model; first deployment can take minutes." if backend == "imx500"
                           else f"Loading {backend} on Pi CPU (IMX500 export not selected).")
            env = dict(os.environ, OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2")
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, encoding="utf-8", errors="replace", env=env, **options)
            self.reader = threading.Thread(target=self._read, args=(self.process,), daemon=True)
            self.reader.start()
        except (OSError, ValueError) as error:
            self.error = str(error)
            self.status = f"Camera unavailable: {error}"

    def _read(self, process):
        try:
            for line in process.stdout:
                if not line.startswith(MESSAGE_PREFIX):
                    if line.strip():
                        self.logs.append(line.strip()[-500:])
                    continue
                try:
                    message = json.loads(line[len(MESSAGE_PREFIX):])
                    if message["type"] == "frame":
                        message["image"] = base64.b64decode(message["image"], validate=True)
                        self.latest = (time.monotonic(), message)
                        self.status = message["status"]
                    elif message["type"] == "error":
                        self.error = message["text"]
                        self.status = f"Camera failed: {self.error}"
                    elif message["type"] == "status":
                        self.status = message["text"]
                except (ValueError, KeyError, TypeError) as error:
                    self.logs.append(f"Invalid camera message: {error}")
            code = process.wait()
            if self.stopping_at is not None:
                self.status = "Camera stopped."
            elif self.error is None:
                self.error = f"Camera worker exited ({code}). " + " | ".join(self.logs)
                self.status = self.error
        finally:
            process.stdout.close()

    def stop(self):
        """Request termination without waiting on the Tk thread."""
        if self.process is not None and self.process.poll() is None and self.stopping_at is None:
            self.stopping_at = time.monotonic()
            self.status = "Stopping camera..."
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass  # The worker finished between poll() and terminate().

    def poll(self):
        if (self.stopping_at is not None and self.process.poll() is None and not self.killed
                and time.monotonic() - self.stopping_at >= 3):
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            self.killed = True

    def close(self):
        """Called after the motor controller is closed, never while stopping motors."""
        self.stop()
        if self.process is not None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.reader is not None:
            self.reader.join(timeout=1)


def encode_preview(image, cv2):
    """BGR array to a bounded RGB PPM image that Tk can display without Pillow."""
    height, width = image.shape[:2]
    scale = min(480 / width, 360 / height, 1)
    if scale < 1:
        image = cv2.resize(image, (max(1, int(width * scale)), max(1, int(height * scale))))
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    height, width = rgb.shape[:2]
    return f"P6\n{width} {height}\n255\n".encode("ascii") + rgb.tobytes()


def run_cpu(args, publish):
    import cv2
    import torch
    from picamera2 import Picamera2
    from ultralytics import YOLO

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    cv2.setNumThreads(1)
    size = 640
    if args.backend == "ncnn":
        import yaml
        metadata = yaml.safe_load((args.model / "metadata.yaml").read_text())
        if metadata.get("names") not in ({0: "testudo"}, {"0": "testudo"}, ["testudo"]):
            raise ValueError("Expected a single Testudo class in NCNN metadata.yaml")
        size = metadata["imgsz"]
    model = YOLO(str(args.model), task="detect")
    if model.names not in ({0: "testudo"}, {"0": "testudo"}, ["testudo"]):
        raise ValueError("Expected a single Testudo class in the trained model")
    camera = Picamera2()
    started = False
    try:
        # Picamera2 RGB888 arrays have BGR byte order, as expected by Ultralytics.
        camera.configure(camera.create_video_configuration(
            main={"size": (640, 480), "format": "RGB888"},
            controls={"FrameRate": args.fps}, buffer_count=4))
        camera.start()
        started = True
        while True:
            image = camera.capture_array("main")
            result = model.predict(image, imgsz=size, conf=args.conf, device="cpu",
                                   verbose=False, rect=False, max_det=30)[0]
            captions = [f"Testudo {float(score):.0%}" for score in result.boxes.conf.tolist()]
            backend = "NCNN / Pi CPU" if args.backend == "ncnn" else "YOLO11n PyTorch / Pi CPU"
            publish(result.plot(), captions, backend, cv2)
    finally:
        try:
            if started:
                camera.stop()
        finally:
            camera.close()


def run_imx500(args, publish, status=None):
    import cv2
    from modlib.apps import Annotator
    from modlib.devices import AiCamera
    from modlib.models import COLOR_FORMAT, MODEL_TYPE, Model
    from modlib.models.post_processors import pp_od_yolo_ultralytics
    if __package__:
        from .imx500_detect import run
    else:
        from imx500_detect import run

    def on_frame(frame, captions):
        image = frame.image
        if frame.color_format == COLOR_FORMAT.RGB:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        publish(image, captions, "IMX500 / camera AI", cv2)

    options = argparse.Namespace(model_dir=args.model, fps=args.fps, conf=args.conf,
                                 seconds=None, headless=False)
    return run(options, AiCamera, Model, MODEL_TYPE, COLOR_FORMAT,
               pp_od_yolo_ultralytics, Annotator, on_frame=on_frame,
               on_status=status, reuse_packaged=True)


def worker_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--backend", choices=("imx500", "ncnn", "pytorch"), required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--conf", type=float, default=0.5)
    parser.add_argument("--fps", type=int, default=16)
    args = parser.parse_args(argv)
    transport = sys.stdout

    def emit(message):
        transport.write(MESSAGE_PREFIX + json.dumps(message) + "\n")
        transport.flush()

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupted)
    begin = None
    frames = 0
    last_preview = float("-inf")

    def publish(image, captions, backend, cv2):
        nonlocal frames, last_preview, begin
        frames += 1
        now = time.monotonic()
        if begin is None:
            begin = now
        if now - last_preview < 0.125:
            return
        last_preview = now
        fps = (frames - 1) / max(now - begin, 0.001)
        emit(dict(type="frame", image=base64.b64encode(encode_preview(image, cv2)).decode("ascii"),
                  status=f"{backend} | Stream {fps:.1f} FPS | " + (", ".join(captions) or "No Testudo detected")))

    try:
        # Keep library progress/logging separate from the framed GUI protocol.
        with redirect_stdout(sys.stderr):
            CameraConfig(args.backend, args.model, confidence=args.conf, fps=args.fps).resolve()
            if args.backend == "imx500":
                run_imx500(args, publish, status=lambda value: emit(dict(type="status", text=value)))
            else:
                run_cpu(args, publish)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        traceback.print_exc()
        emit(dict(type="error", text=f"{error}. See camera setup in raspberry_pi/README.md."))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(worker_main())
