"""Local, read-only live hoop preview using an explicitly selected macOS camera."""

import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import secrets
import subprocess
import threading
import time
import webbrowser

import cv2
import numpy as np

from .detector import DetectorConfig, detect
from .ellipse_guidance import guided_candidates
from .native_capture import BridgeCapture, build_bridge, list_cameras, select_camera
from .orientation import estimate_orientation, validate_calibration
from .replay import overlay
from .tracker import HoopTracker, TrackerConfig
from .verification import VerificationConfig, verify
from .color import WHITE_SATURATION_MAX, WHITE_VALUE_MIN, WHITE_MIN_SUPPORT


class LiveState:
    """Expiry is evaluated when consumed, even if capture or processing stops."""
    def __init__(self, device, max_age=.2, clock=time.monotonic):
        self.device = device
        self.max_age = max_age
        self.clock = clock
        self.lock = threading.Lock()
        self.result = None
        self.unavailable = "Waiting for the selected camera"
        self.closed = False

    def publish(self, packet, observation, jpeg, processing_ms):
        with self.lock:
            if self.closed:
                return
            self.unavailable = None
            self.result = (packet, observation.to_dict(), base64.b64encode(jpeg).decode("ascii"), processing_ms)

    def invalidate(self, reason, terminal=False):
        with self.lock:
            self.result = None
            self.unavailable = reason
            self.closed = self.closed or terminal

    def snapshot(self):
        with self.lock:
            result, unavailable = self.result, self.unavailable
        status = dict(camera=self.device["name"], device_id=self.device["id"], available=False,
                      decision="unknown", reason=unavailable or "Frame expired", image=None,
                      observation=None, remaining_ms=0, capture_to_host_latency="unmeasured",
                      motor_output=False)
        if unavailable or result is None:
            return status
        packet, observation, image, processing_ms = result
        age = self.clock() - packet.received_at
        if not 0 <= age <= self.max_age:
            return status
        status.update(available=True, decision=observation["decision"],
                      reason=observation["tracking_reason"], image=image, observation=observation,
                      source_time_s=packet.metadata["source_time_s"], sequence=packet.metadata["sequence"],
                      host_age_ms=round(age*1000, 1), processing_ms=round(processing_ms, 1),
                      remaining_ms=max(0, (self.max_age-age)*1000))
        return status


class VisionWorker:
    def __init__(self, capture, state, profile="ellipse", calibration=None, debug=False, color_options=None,
                 continuity_lock=True):
        self.capture, self.state = capture, state
        self.calibration, self.debug = calibration, debug
        color_options = color_options or {}
        self.detector = DetectorConfig(rim_hypotheses=profile=="ellipse", max_candidates=18, **color_options)
        self.verifier = VerificationConfig(profile=profile, **color_options)
        self.tracker = HoopTracker(TrackerConfig.ellipse(continuity_lock=continuity_lock) if profile=="ellipse" else TrackerConfig())
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def process(self, packet):
        started = time.monotonic()
        image = cv2.imdecode(np.frombuffer(packet.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or (image.shape[1], image.shape[0]) != (packet.metadata["width"], packet.metadata["height"]):
            raise ValueError("Camera JPEG is invalid or does not match its frame dimensions")
        timestamp = packet.metadata["source_time_s"]
        observation = detect(image, self.detector)
        if self.tracker.size is not None and observation.frame_size != self.tracker.size:
            self.tracker.reset()
        if self.verifier.profile == "ellipse":
            observation.candidates = guided_candidates(image, self.tracker, timestamp) + observation.candidates
        verify(image, observation, self.verifier)
        # Host age is measurable. Wireless sensor-to-host latency is not.
        self.tracker.update(observation, timestamp, packet.metadata["timestamp_provenance"],
                            age_seconds=time.monotonic()-packet.received_at)
        if (self.calibration and observation.accepted_target and
                observation.accepted_target.get('boundary_stable',True)):
            e = observation.accepted_target["ellipse"]
            observation.image_orientation = estimate_orientation(
                (e["center_px"], e["axes_px"], e["image_rotation_deg"]), observation.frame_size, self.calibration)
        view = overlay(image, observation, "LIVE | " + self.capture.device["name"], debug=self.debug)
        ok, jpeg = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            raise ValueError("Could not encode the local preview")
        self.state.publish(packet, observation, jpeg.tobytes(), (time.monotonic()-started)*1000)

    def run(self):
        version = 0
        try:
            while not self.stop.is_set():
                version, packet = self.capture.slot.take(version)
                if self.capture.slot.closed:
                    break
                if packet is None:
                    continue
                if time.monotonic()-packet.received_at > self.state.max_age:
                    self.state.invalidate("Frame expired before processing")
                    continue
                self.process(packet)
        except Exception as exc:
            self.state.invalidate("Vision stopped: " + str(exc), terminal=True)
            self.stop.set()
        finally:
            if self.capture.slot.closed:
                self.state.invalidate(self.capture.slot.error or "Camera unavailable", terminal=True)

    def close(self):
        self.stop.set()
        self.state.invalidate("Stopped", terminal=True)
        if self.thread.is_alive():
            self.thread.join(timeout=3)


PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live hoop tracking</title><style>
body{font:16px system-ui;background:#171b22;color:#eef2f8;max-width:950px;margin:30px auto;padding:0 20px}
img{width:100%;max-width:800px}#status{font-size:22px}#detail{white-space:pre-wrap;color:#c3ccdb}
</style><h1>Live hoop tracking</h1><p id="camera"></p>
<p id="status">Connecting…</p><img id="preview" alt="Live annotated camera preview" hidden>
<p id="detail"></p><p>Image measurements only. Signed orientation unresolved. No motor output.</p>
<p>Stop capture with Ctrl+C in the terminal. Closing this tab does not stop capture.</p>
<script>
const endpoint=ENDPOINT, image=document.getElementById('preview');let expiry;
function clearFrame(message){image.hidden=true;image.removeAttribute('src');
 document.getElementById('status').textContent=message;document.getElementById('detail').textContent='';}
async function refresh(){const requested=performance.now();
 try{const response=await fetch(endpoint,{cache:'no-store',signal:AbortSignal.timeout(1500)});
 if(!response.ok)throw Error('Preview unavailable');const s=await response.json();clearTimeout(expiry);
 document.getElementById('camera').textContent=s.camera;
 const remaining=s.remaining_ms-(performance.now()-requested);
 if(s.available&&remaining>0){image.src='data:image/jpeg;base64,'+s.image;image.hidden=false;
 document.getElementById('status').textContent=s.decision.toUpperCase()+' — '+s.reason;
 document.getElementById('detail').textContent='Processing: '+s.processing_ms+' ms | Host frame age: '+s.host_age_ms+
 ' ms\\nWireless capture-to-host latency: unmeasured';
 expiry=setTimeout(()=>clearFrame('UNKNOWN — frame expired'),remaining);
 }else clearFrame('UNKNOWN — '+s.reason);
 }catch(e){clearFrame('UNKNOWN — preview disconnected');}finally{setTimeout(refresh,80);}}
refresh();</script></html>"""


def start_preview(state):
    token = secrets.token_urlsafe(24)
    root = "/" + token

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == root + "/":
                content = PAGE.replace("ENDPOINT", json.dumps(root+"/status")).encode()
                kind = "text/html; charset=utf-8"
            elif self.path == root + "/status":
                content = json.dumps(state.snapshot(), allow_nan=False).encode()
                kind = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}{root}/"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-cameras", action="store_true", help="List macOS camera names and stable IDs, then exit")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--device-id", help="Exact ID from --list-cameras")
    selection.add_argument("--camera-name", help="Unique case-insensitive substring, e.g. iPhone")
    parser.add_argument("--fps", type=float, default=10, help="Maximum sampled source rate (default: 10); actual rate varies")
    parser.add_argument("--seconds", type=float, help="Stop after this many seconds; default runs until Ctrl+C")
    parser.add_argument("--tracking-profile", choices=("ellipse", "strict"), default="ellipse")
    parser.add_argument("--camera-calibration", type=Path, help="Calibration for this camera, resolution and camera mode only")
    parser.add_argument("--debug", action="store_true", help="Also draw unaccepted proposals and predicted tracks")
    parser.add_argument("--no-browser", action="store_true", help="Print the local preview URL without opening a browser")
    parser.add_argument("--saturation-max", type=int, default=WHITE_SATURATION_MAX, help="White HSV saturation ceiling, 0..255 (default: %(default)s)")
    parser.add_argument("--value-min", type=int, default=WHITE_VALUE_MIN, help="White HSV brightness floor, 0..255 (default: %(default)s)")
    parser.add_argument("--min-white-support", type=float, default=WHITE_MIN_SUPPORT, help="Minimum supported white perimeter fraction (default: %(default)s)")
    parser.add_argument("--no-continuity-lock", action="store_true", help="Disable the ellipse profile's slow-motion local preference and weaker maintenance")
    args = parser.parse_args(argv)
    if not math.isfinite(args.fps) or not 0 < args.fps <= 60:
        parser.error("--fps must be finite and in (0,60]")
    if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error("--seconds must be finite and positive")
    if not args.list_cameras and not (args.device_id or args.camera_name):
        parser.error("Select --camera-name or --device-id; use --list-cameras to discover cameras")
    capture = worker = server = None
    try:
        color_options = dict(saturation_max=args.saturation_max, value_min=args.value_min,
                             min_white_support=args.min_white_support)
        # Validate color settings before discovery can request camera access.
        VerificationConfig(**color_options)
        calibration = validate_calibration(json.loads(args.camera_calibration.read_text())) if args.camera_calibration else None
        print("Preparing the macOS camera helper. Allow camera access if macOS asks.", flush=True)
        binary = build_bridge()
        devices = list_cameras(binary)
        if args.list_cameras:
            print(json.dumps(devices, indent=2))
            return 0
        device = select_camera(devices, args.device_id, args.camera_name)
        capture = BridgeCapture(binary, device, args.fps)
        state = LiveState(device)
        worker = VisionWorker(capture, state, args.tracking_profile, calibration, args.debug, color_options,
                              continuity_lock=not args.no_continuity_lock)
        server, url = start_preview(state)
        worker.thread.start()
        print(f"Selected camera: {device['name']}\nLocal preview: {url}\nCtrl+C stops capture. No motor output.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        started = time.monotonic()
        while args.seconds is None or time.monotonic()-started < args.seconds:
            if capture.slot.closed or worker.stop.is_set():
                # Invalidate immediately; the browser also expires independently.
                reason = capture.slot.error if capture.slot.closed else state.snapshot()["reason"]
                state.invalidate(reason, terminal=True)
                print(reason, flush=True)
                for line in list(capture.logs):
                    print(line, flush=True)
                return 1
            time.sleep(.05)
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print("Live camera error: " + str(exc))
        return 1
    finally:
        if capture:
            capture.close()
        if worker:
            worker.close()
        if server:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
