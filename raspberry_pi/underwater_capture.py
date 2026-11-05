#!/usr/bin/env python3
"""Record clean Pi camera video, with a browser preview and optional hoop tracking."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def capture_rate(value):
    return 'max' if value == 'max' else positive(value)


def make_10fps_copy(directory):
    """Resample presentation times after capture; never slow the master recording."""
    destination = directory / 'clean-10fps.mp4'
    temporary = directory / 'clean-10fps.partial.mp4'
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
               '-i', str(directory / 'clean.mp4'), '-map', '0:v:0', '-an',
               '-vf', 'fps=10', '-c:v', 'libx264', '-preset', 'veryfast',
               '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(temporary)]
    if destination.exists():
        raise RuntimeError(f'Refusing to overwrite {destination}')
    print('Capture stopped. Creating clean-10fps.mp4; this can take time on the Pi.', flush=True)
    log_path = directory / '10fps-conversion.log'
    try:
        with log_path.open('w') as log:
            subprocess.run(command, stdout=log, stderr=log, check=True)
        temporary.rename(destination)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f'10 FPS conversion failed. Master clean.mp4 is retained. See {log_path}') from error
    return dict(status='completed', path=destination.name, fps=10,
                timing='constant frame rate, normal speed; may repeat frames if capture falls below 10 FPS')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new session directory')
    parser.add_argument('--hoop', action='store_true', help='run the existing ellipse tracker on the Pi CPU')
    parser.add_argument('--camera', type=int, default=0)
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--fps', type=capture_rate, default='max',
                        help='max for the configured sensor mode, or a positive numeric capture rate')
    parser.add_argument('--process-fps', type=positive, default=10)
    parser.add_argument('--bitrate', type=int, default=8_000_000)
    parser.add_argument('--seconds', type=positive, default=120)
    parser.add_argument('--host', default='127.0.0.1', help='use SSH forwarding for remote viewing')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--notes', default='', help='water, lighting, scene and housing notes')
    args = parser.parse_args(argv)
    if args.width < 640 or args.width % 2 or args.height < 480 or args.height % 2:
        parser.error('recording size must be even and at least 640x480')
    if args.camera < 0 or args.bitrate <= 0 or not 1 <= args.port <= 65535:
        parser.error('invalid camera, bitrate or port')
    return args


class Preview:
    """Keep one JPEG; a disconnected or slow browser never queues camera frames."""

    def __init__(self):
        self.condition = threading.Condition()
        self.jpeg = None
        self.updated = 0
        self.sequence = 0
        self.status = {'state': 'starting'}

    def publish(self, jpeg, status):
        with self.condition:
            self.jpeg, self.status = jpeg, status
            self.updated = time.monotonic()
            self.sequence += 1
            self.condition.notify_all()

    def handler(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/':
                    body = (b'<!doctype html><title>Underwater camera</title>'
                            b'<h1>Underwater camera</h1><p>Clean video is saved on the Pi. '
                            b'Preview may skip frames. <a href="/status">Status</a></p>'
                            b'<p id="state">Waiting for frames</p>'
                            b'<img id="video" src="/stream.mjpg" style="max-width:100%">'
                            b'<script>let last=0; const v=document.getElementById("video");'
                            b'async function poll(){try{const r=await fetch("/status",{cache:"no-store"});'
                            b'const s=await r.json();last=Date.now();'
                            b'document.getElementById("state").textContent=JSON.stringify(s);'
                            b'v.style.visibility=s.preview_stale?"hidden":"visible";'
                            b'}catch(e){v.style.visibility="hidden";}}'
                            b'setInterval(poll,100);setInterval(()=>{if(Date.now()-last>1000)'
                            b'v.style.visibility="hidden";},100);poll();</script>')
                    content_type = 'text/html'
                elif self.path == '/status':
                    with state.condition:
                        age = time.monotonic() - state.updated
                        status = dict(state.status)
                    sensor_age = status.get('sensor_age_s', 0) + age
                    stale = age > 1 or (status.get('decision') == 'accepted' and sensor_age > .2)
                    status.update(preview_stale=stale, preview_age_s=age, current_sensor_age_s=sensor_age)
                    if stale:
                        status['decision'] = 'unknown'
                    body = json.dumps(status, allow_nan=False).encode()
                    content_type = 'application/json'
                elif self.path == '/stream.mjpg':
                    self.send_response(200)
                    self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    sequence = -1
                    try:
                        while True:
                            with state.condition:
                                state.condition.wait_for(lambda: state.sequence != sequence, timeout=2)
                                if time.monotonic() - state.updated > 1:
                                    return  # Close stalled streams instead of repeating old detections.
                                sequence, jpeg = state.sequence, state.jpeg
                            if jpeg is None:
                                continue
                            self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                                             + str(len(jpeg)).encode() + b'\r\n\r\n' + jpeg + b'\r\n')
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        return Handler


class HoopProcessor:
    def __init__(self):
        from raspberry_pi.hoop_vision.detector import DetectorConfig
        from raspberry_pi.hoop_vision.verification import VerificationConfig
        from raspberry_pi.hoop_vision.tracker import HoopTracker, TrackerConfig
        self.config = DetectorConfig(rim_hypotheses=True, max_candidates=18)
        self.verifier = VerificationConfig(profile='ellipse')
        self.tracker = HoopTracker(TrackerConfig.ellipse())

    def process(self, image, timestamp, age):
        from raspberry_pi.hoop_vision.detector import detect
        from raspberry_pi.hoop_vision.verification import verify
        from raspberry_pi.hoop_vision.ellipse_guidance import guided_candidates
        from raspberry_pi.hoop_vision.ring_alignment import annotate_alignment
        observation = detect(image, self.config)
        observation.candidates = guided_candidates(image, self.tracker, timestamp, self.verifier) + observation.candidates
        verify(image, observation, self.verifier)
        self.tracker.update(observation, timestamp, 'picamera2_sensor_timestamp', age_seconds=age())
        annotate_alignment(image, observation, self.tracker, timestamp, self.verifier, age_seconds=age())
        return observation


def run(args):
    import cv2
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import PyavOutput

    if shutil.which('ffmpeg') is None:
        raise RuntimeError('Install ffmpeg with sudo apt install ffmpeg before recording')
    args.output.mkdir(parents=True, exist_ok=False)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda *_: stop.set())
    preview = Preview()
    server = ThreadingHTTPServer((args.host, args.port), preview.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    camera = None
    recording = False
    started = time.monotonic()
    count, accepted = 0, 0
    first_sensor = last_sensor = None
    status = 'failed'
    errors = []
    output = None
    summary = dict(start_utc=datetime.now(timezone.utc).isoformat(),
                   options={key: str(value) if isinstance(value, Path) else value
                            for key, value in vars(args).items()},
                   note='Processed frames are sampled; they are not an inventory of encoded frames.')
    try:
        try:
            summary['git_commit'] = subprocess.check_output(
                ['git', '-C', str(Path(__file__).resolve().parent), 'rev-parse', 'HEAD'],
                text=True, stderr=subprocess.DEVNULL).strip()
            summary['git_dirty'] = bool(subprocess.check_output(
                ['git', '-C', str(Path(__file__).resolve().parent), 'status', '--porcelain'],
                text=True, stderr=subprocess.DEVNULL).strip())
        except (OSError, subprocess.CalledProcessError):
            summary['git_commit'] = None
        processor = HoopProcessor() if args.hoop else None
        camera = Picamera2(args.camera)
        # lores must be <= main in both dimensions; preserve the recording aspect ratio.
        lores_height = int(args.height * 640 / args.width) // 2 * 2
        camera.configure(camera.create_video_configuration(
            main={'size': (args.width, args.height), 'format': 'YUV420'},
            lores={'size': (640, lores_height), 'format': 'YUV420'},
            controls={} if args.fps == 'max' else {'FrameRate': args.fps}, buffer_count=6, queue=False))
        if args.fps == 'max':
            minimum_duration = int(camera.camera_controls['FrameDurationLimits'][0])
            if minimum_duration <= 0:
                raise RuntimeError('Camera did not report a valid minimum frame duration')
            camera.set_controls({'FrameDurationLimits': (minimum_duration, minimum_duration)})
            summary['requested_capture_fps'] = 1_000_000 / minimum_duration
            print(f"Requesting configured sensor mode maximum: {summary['requested_capture_fps']:.2f} FPS", flush=True)
        else:
            summary['requested_capture_fps'] = args.fps
        summary['camera_configuration'] = camera.camera_configuration()
        summary['camera_properties'] = camera.camera_properties
        output = PyavOutput(str(args.output / 'clean.mp4'), pts=str(args.output / 'video-pts.txt'))
        output.error_callback = lambda error: (errors.append(str(error)), stop.set())
        print('Starting Pi camera. IMX500 firmware loading may take several minutes.', flush=True)
        camera.start_recording(H264Encoder(bitrate=args.bitrate), output)
        recording = True
        started = time.monotonic()
        print(f'Preview http://{args.host}:{args.port} | Ctrl-C stops and finalizes clean.mp4', flush=True)
        next_frame = started
        with (args.output / 'frames.jsonl').open('w') as frames_log, \
                (args.output / 'hoop-observations.jsonl').open('w') as hoop_log:
            while not stop.is_set() and time.monotonic() - started < args.seconds:
                if stop.wait(max(0, next_frame - time.monotonic())):
                    break
                request = camera.capture_request()
                try:
                    metadata = request.get_metadata()
                    yuv = request.make_array('lores').copy()
                finally:
                    request.release()  # Never hold a camera buffer during CV, JPEG or disk work.
                processing_start = time.monotonic()
                sensor = metadata['SensorTimestamp']
                if last_sensor is not None and sensor <= last_sensor:
                    raise RuntimeError('Non-increasing camera SensorTimestamp')
                first_sensor = sensor if first_sensor is None else first_sensor
                last_sensor = sensor
                timestamp = (sensor - first_sensor) / 1e9
                # SensorTimestamp uses CLOCK_BOOTTIME on libcamera. Fall back to host receipt
                # only on platforms without that clock; record the choice in the summary.
                clock_id = getattr(time, 'CLOCK_BOOTTIME', None)
                summary['age_clock'] = 'CLOCK_BOOTTIME' if clock_id is not None else 'host_receipt'
                def age():
                    return max(0, time.clock_gettime(clock_id) - sensor / 1e9) if clock_id is not None else time.monotonic() - processing_start
                image = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
                observation = processor.process(image, timestamp, age) if processor else None
                decision = observation.decision if observation else 'disabled'
                view = image
                if observation:
                    from raspberry_pi.hoop_vision.replay import overlay
                    view = overlay(image, observation, 'UNDERWATER LIVE', show_caption=False)
                    row = observation.to_dict()
                    row.update(source_time_s=timestamp, sensor_timestamp_ns=sensor,
                               timestamp_provenance='picamera2_sensor_timestamp', processing_ms=(time.monotonic()-processing_start)*1000,
                               age_seconds=age())
                    hoop_log.write(json.dumps(row, allow_nan=False) + '\n')
                    hoop_log.flush()
                    accepted += observation.accepted_target is not None
                # Never display an old accepted overlay as current evidence.
                if age() > .2:
                    view, decision = image, 'unknown'
                cv2.putText(view, f'{decision} | age {age()*1000:.0f}ms', (10, 22), 0, .5, (0, 220, 255), 1)
                ok, jpeg = cv2.imencode('.jpg', view, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not ok:
                    raise RuntimeError('Preview JPEG encoding failed')
                count += 1
                preview.publish(jpeg.tobytes(), dict(state='running', decision=decision,
                                processed_frames=count, source_time_s=timestamp, sensor_age_s=age()))
                frames_log.write(json.dumps(dict(frame=count, source_time_s=timestamp,
                    host_elapsed_s=time.monotonic()-started, metadata=metadata), default=str) + '\n')
                frames_log.flush()
                # Drop processing opportunities rather than building a FIFO backlog.
                next_frame = max(next_frame + 1 / args.process_fps, time.monotonic())
        if errors:
            raise RuntimeError('Video encoder/output failed: ' + '; '.join(errors))
        status = 'stopped' if stop.is_set() else 'completed'
    except BaseException as error:
        summary['error'] = str(error)
        raise
    finally:
        try:
            if camera is not None:
                try:
                    if recording:
                        camera.stop_recording()
                finally:
                    camera.close()
        finally:
            if output is not None and output.ptsoutput is not None:
                output.ptsoutput.close()
            server.shutdown()
            server.server_close()
            elapsed = time.monotonic() - started
            summary.update(status=status, elapsed_s=elapsed, processed_frames=count,
                           processed_fps=count / elapsed if elapsed else 0,
                           accepted_observations=accepted, encoder_errors=errors,
                           first_processed_sensor_timestamp_ns=first_sensor,
                           last_processed_sensor_timestamp_ns=last_sensor)
            pts_file = args.output / 'video-pts.txt'
            if pts_file.exists():
                try:
                    pts = [float(line) for line in pts_file.read_text().splitlines() if line.strip()]
                    summary['encoded_frames'] = len(pts)
                    if len(pts) > 1:
                        span = (pts[-1] - pts[0]) / 1000
                        summary['encoded_span_s'] = span
                        summary['encoded_mean_fps'] = (len(pts) - 1) / span if span > 0 else None
                        summary['max_encoded_gap_ms'] = max(b-a for a, b in zip(pts, pts[1:]))
                except (OSError, ValueError) as error:
                    summary['pts_read_error'] = str(error)
            session_path = args.output / 'session.json'
            summary['derived_10fps'] = {'status': 'pending' if status != 'failed' else 'not_attempted'}
            session_path.write_text(json.dumps(summary, indent=2, default=str) + '\n')
            print(f'Session {status}: {count} processed frames. Files: {args.output.resolve()}', flush=True)
            if status != 'failed':
                # Recording and the camera are closed before this CPU-intensive step.
                try:
                    summary['derived_10fps'] = make_10fps_copy(args.output)
                except BaseException as error:
                    summary['derived_10fps'] = {'status': 'failed', 'error': str(error)}
                    raise
                finally:
                    session_path.write_text(json.dumps(summary, indent=2, default=str) + '\n')


def main(argv=None):
    args = parse_args(argv)
    try:
        run(args)
    except Exception as error:
        print(f'Capture failed: {error}. See docs/pi-underwater-testing.md.', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
