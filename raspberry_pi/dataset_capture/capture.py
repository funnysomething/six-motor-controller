#!/usr/bin/env python3
"""Collect unannotated Testudo training images with the Pi CSI camera."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
from uuid import uuid4


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-cameras", action="store_true")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "sessions",
                        help="parent directory; every run creates a new session")
    parser.add_argument("--kind", choices=("positive", "negative"), default="positive",
                        help="positive: statue present; negative: statue removed")
    parser.add_argument("--environment", choices=("dry", "underwater"), default="dry")
    parser.add_argument("--notes", default="", help="background, viewpoint, lighting, etc.")
    parser.add_argument("--width", type=positive_int, default=1280)
    parser.add_argument("--height", type=positive_int, default=960)
    parser.add_argument("--fps", type=positive_float, default=15)
    parser.add_argument("--interval", type=positive_float, default=2,
                        help="seconds between automatic saves, default 2")
    parser.add_argument("--warmup", type=positive_float, default=3,
                        help="exposure settling time before collection")
    parser.add_argument("--max-images", type=positive_int, help="stop after this many saves")
    parser.add_argument("--seconds", type=positive_float, help="stop after this many seconds, after warmup")
    parser.add_argument("--preview", action="store_true", help="Pi desktop window with keyboard controls")
    parser.add_argument("--manual", action="store_true", help="start paused; requires --preview")
    parser.add_argument("--quality", type=int, default=95, help="JPEG quality, 1-100")
    args = parser.parse_args(argv)
    if args.camera < 0:
        parser.error("--camera must be zero or greater")
    if not 1 <= args.quality <= 100:
        parser.error("--quality must be between 1 and 100")
    if args.manual and not args.preview:
        parser.error("--manual requires --preview")
    if args.preview and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        parser.error("--preview needs a Pi desktop display; omit it over SSH")
    return args


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


def run(args, Picamera2, cv2):
    cameras = Picamera2.global_camera_info()
    print("Detected cameras:", json.dumps(cameras, default=str), flush=True)
    if not cameras:
        raise RuntimeError(
            "Picamera2 detected no CSI cameras. Run rpicam-hello --list-cameras on the Pi. "
            "If that also reports no cameras, check the IMX500 packages, boot configuration, "
            "and ribbon connection with power disconnected. If rpicam lists a camera, "
            "compare with system python3 outside your virtual environment. "
            "See dataset_capture/README.md troubleshooting."
        )
    if args.list_cameras:
        return 0
    if args.camera >= len(cameras):
        raise RuntimeError(f"Camera index {args.camera} is unavailable; choose an index from the list above.")

    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid4().hex[:8]
    session = args.output.expanduser().resolve() / f"{session_id}_{args.environment}_{args.kind}"
    images = session / "images"
    images.mkdir(parents=True, exist_ok=False)
    manifest = {
        "session_id": session_id, "started_utc": datetime.now(timezone.utc).isoformat(),
        "status": "starting", "class_name": "testudo", "kind": args.kind,
        "environment": args.environment, "notes": args.notes,
        "camera": cameras[args.camera],
        "arguments": vars(args), "image_count": 0,
        "annotation_status": "unlabeled",
    }
    write_json(session / "session.json", manifest)
    camera = None
    started = False
    count = 0
    status = "failed"
    begin = None
    print(f"Session: {session}\nKind: {args.kind}. Ctrl+C stops capture.", flush=True)
    try:
        camera = Picamera2(args.camera)
        camera.configure(camera.create_video_configuration(
            # Picamera2 RGB888 arrays contain BGR bytes, as expected by OpenCV.
            main={"size": (args.width, args.height), "format": "RGB888"},
            controls={"FrameRate": args.fps}, buffer_count=4,
        ))
        manifest["camera_configuration"] = camera.camera_configuration()
        write_json(session / "session.json", manifest)
        print("Starting camera; initial IMX500 firmware loading may take several minutes.", flush=True)
        camera.start()
        started = True
        time.sleep(args.warmup)
        begin = time.monotonic()
        next_save = begin
        automatic = not args.manual
        status = "completed"
        if args.preview:
            print("Window keys: Space/S save, P pause/resume automatic capture, Q quit.", flush=True)
        with (session / "frames.jsonl").open("a", buffering=1) as records:
            while True:
                if args.seconds and time.monotonic() - begin >= args.seconds:
                    break
                request = camera.capture_request()
                try:
                    # Own the memory before releasing the camera's reusable buffer.
                    frame = request.make_array("main").copy()
                    metadata = request.get_metadata()
                finally:
                    request.release()
                if frame is None or frame.size == 0:
                    raise RuntimeError("Camera returned an empty frame")
                key = -1
                if args.preview:
                    display = frame.copy()
                    scale = min(1.0, 960 / display.shape[1])
                    if scale < 1:
                        display = cv2.resize(display, (int(display.shape[1] * scale),
                                                       int(display.shape[0] * scale)))
                    label = f"{args.kind} | saved {count} | {'AUTO' if automatic else 'PAUSED'}"
                    cv2.putText(display, label, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                                0.65, (0, 255, 0), 2)
                    cv2.imshow("Testudo dataset - Space/S save, P pause, Q quit", display)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (ord("q"), ord("Q"), 27):
                        break
                    if cv2.getWindowProperty("Testudo dataset - Space/S save, P pause, Q quit",
                                             cv2.WND_PROP_VISIBLE) < 1:
                        break
                    if key in (ord("p"), ord("P")):
                        automatic = not automatic
                        next_save = time.monotonic() + args.interval
                now = time.monotonic()
                if key in (32, ord("s"), ord("S")) or (automatic and now >= next_save):
                    name = f"{session_id}_{count + 1:06d}.jpg"
                    path = images / name
                    temporary = images / (path.stem + ".tmp.jpg")
                    try:
                        if not cv2.imwrite(str(temporary), frame, [cv2.IMWRITE_JPEG_QUALITY, args.quality]):
                            raise RuntimeError(f"Could not save {path}; check disk space and permissions")
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
                    # Record each successful image immediately, including matched request metadata.
                    records.write(json.dumps({
                        "image": f"images/{name}", "captured_utc": datetime.now(timezone.utc).isoformat(),
                        "elapsed_s": now - begin, "width": frame.shape[1], "height": frame.shape[0],
                        "kind": args.kind, "metadata": metadata,
                    }, default=str) + "\n")
                    count += 1
                    print(f"Saved {count}: {name}", flush=True)
                    # No catch-up bursts after slow writes or a paused session.
                    next_save = time.monotonic() + args.interval
                    if args.max_images and count >= args.max_images:
                        break
    except KeyboardInterrupt:
        status = "interrupted"
    except Exception as error:
        status = "failed"
        manifest["error"] = str(error)
        raise
    finally:
        try:
            if camera is not None:
                try:
                    if started:
                        camera.stop()
                finally:
                    camera.close()
        finally:
            manifest.update(status=status, image_count=count,
                            finished_utc=datetime.now(timezone.utc).isoformat(),
                            elapsed_s=time.monotonic() - begin if begin is not None else 0)
            write_json(session / "session.json", manifest)
            if args.preview:
                cv2.destroyAllWindows()
            print(f"{status}: {count} images in {session}", flush=True)
    return 130 if status == "interrupted" else 0


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    args = parse_args(argv)
    for name in ("SIGTERM", "SIGHUP"):
        signal.signal(getattr(signal, name), interrupted)
    try:
        from picamera2 import Picamera2
        import cv2
        return run(args, Picamera2, cv2)
    except ImportError as error:
        print(f"Missing camera dependency: {error}. On Raspberry Pi OS install "
              "python3-picamera2 python3-opencv through apt. See README.md.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"Capture failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
