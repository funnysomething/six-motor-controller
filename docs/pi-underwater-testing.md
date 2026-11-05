# Underwater recording and hoop testing on Raspberry Pi

Branch: `codex/pi-underwater-vision-testing`, starting from `b36da21` with the latest paired-boundary tracker. This checkout is separate from the Mac/iPhone work. It does not connect to motor controls.

## Use both recording and live testing

Start with a recording-only session to check focus, exposure, color and sustained capture. Then record with `--hoop` to test the actual Pi CPU processing speed and live tracking. Keep the clean recording from both sessions. Offline replay lets us compare detector versions on identical underwater views, including failures, without needing another tank session. Offline throughput does not establish live latency or reliable real-time tracking.

Only one process should own the CSI camera. Do not run `camera_test.py`, the existing Testudo inference server or `hoop_live.py` alongside this recorder. This program opens Picamera2 once, records the main stream as H.264 MP4, and samples a lower-resolution stream for preview and optional hoop tracking. Recording runs through the Picamera2 encoder independently of the processing loop. Encoding, disk writes and tracking still share Pi resources, so verify the resulting video for gaps.

The hoop tracker uses OpenCV on the CPU. This recording tool does not run a Testudo model or load a custom IMX500 network. Its clean footage is input for later labeling and training.

## Set up the Pi

Run these commands on the Pi to clone the testing branch into its own directory. This keeps your existing controller/Testudo checkout separate:

```sh
sudo apt update
sudo apt install git
git clone --branch codex/pi-underwater-vision-testing --single-branch \
  https://github.com/funnysomething/six-motor-controller.git ~/underwater-vision
cd ~/underwater-vision
git branch --show-current
```

The last command should print `codex/pi-underwater-vision-testing`. If `~/underwater-vision` already contains this clone, update it instead:

```sh
cd ~/underwater-vision
git status --short
git fetch origin
git switch codex/pi-underwater-vision-testing
git pull --ff-only origin codex/pi-underwater-vision-testing
```

If Git reports local edits or refuses to switch/pull, preserve those edits before continuing. A fresh clone in another directory is also fine. Run the following setup and recording commands from the repository root, not from `raspberry_pi/`.

On the Pi, use a current Raspberry Pi OS camera stack with IMX500 support. These commands install OS-managed camera packages and an isolated Python environment that can access them:

```sh
sudo apt update
sudo apt install python3-picamera2 python3-opencv python3-numpy python3-pil python3-av python3-venv imx500-all ffmpeg
cd ~/underwater-vision
python3 -m venv --system-site-packages .venv-hoop
rpicam-hello --list-cameras
.venv-hoop/bin/python -c 'from picamera2.outputs import PyavOutput; import cv2, PIL; print("Imports OK")'
```

Reboot after first installing IMX500 firmware if needed. Do not install pip replacements for libcamera/Picamera2 or replace the OS NumPy/OpenCV packages for this test. If `PyavOutput` is unavailable, update the OS Picamera2 package. The recorder deliberately requires the output backend that preserves encoder timestamps.

The clone records its Git commit and whether files have local changes in each session's `session.json`. Recordings under `camera_results/` are ignored by Git and remain on the Pi when pulling updates. Copy them separately when archiving results. After future pulls, check this guide for dependency changes; the virtual environment is local to the Pi and is not downloaded from GitHub.

## Record and view

First record a two-minute camera-only session on the Pi:

```sh
.venv-hoop/bin/python raspberry_pi/underwater_capture.py \
  --output camera_results/underwater-camera-01 --seconds 120 \
  --notes "Final housing underwater; describe lighting, water and target placement"
```

In another Mac terminal, forward the preview port:

```sh
ssh -N -L 8080:127.0.0.1:8080 pi@raspberrypi.local
```

Open `http://localhost:8080`. Preview uses MJPEG and needs a working network or tether; recording continues locally when a browser disconnects. Wi-Fi through water may be unreliable, so keep the network endpoint above water or use the available wired connection. The default server binds only to loopback for the SSH tunnel. `--host 0.0.0.0` exposes it directly on the local network without authentication.

Stop with Ctrl-C or wait for the duration. Then repeat with tracking enabled:

```sh
.venv-hoop/bin/python raspberry_pi/underwater_capture.py \
  --output camera_results/underwater-hoop-01 --seconds 120 --hoop \
  --fps 10 --process-fps 10 --notes "Hoop test; final housing and underwater illumination"
```

Defaults record at 1280x720, requested 10 FPS and 8 Mbps. Preview/tracking use a 640-pixel-wide stream with the same aspect ratio. `--process-fps 5` reduces processing load without changing the requested recording FPS. `--width 1920 --height 1080` increases recording detail for training if the Pi sustains it. Requested rates are targets, not measured guarantees. Allow automatic exposure/white balance to settle before collecting the useful part of a session; startup frames remain in the recording.

Each new output directory contains:

- `clean.mp4`: unannotated main-stream video for labeling and offline replay.
- `video-pts.txt`: encoded-frame presentation timestamps in milliseconds, useful for spotting gaps and counting encoded frames.
- `frames.jsonl`: sampled processing-frame sensor timestamps and exposure/gain/color metadata. This is not a log of every recorded frame.
- `hoop-observations.jsonl`: accepted/unknown decisions, candidates and alignment diagnostics when `--hoop` is enabled; empty otherwise.
- `session.json`: options, camera configuration, hardware properties, session status, processed FPS, encoded frame count/rate and largest encoded timestamp gap. Git metadata includes a dirty flag when available. Acceptance counts are not detection accuracy.

Sensor timestamps use libcamera's boot-time clock; hoop time starts at the first processed sensor timestamp. MP4 timestamps start at the encoder's timeline. Do not assume their first frames coincide. The original sensor timestamp is retained in each processing log. Preview omits stale accepted overlays and reports unknown when processing is too old; it is a test display, not a navigation command.

The MP4 container is finalized on normal exit, Ctrl-C, SIGTERM and SIGHUP. Sudden power loss or SIGKILL can leave an unusable MP4. Prefer short sessions and confirm playback after each. A failing camera read can require stopping the process externally; no automatic CSI recovery is implemented.

## Replay and make a training dataset

Replay on the Pi or transfer the entire session folder to the Mac first:

```sh
.venv-hoop/bin/python scripts/vision/hoop_replay.py \
  camera_results/underwater-hoop-01/clean.mp4 \
  --output camera_results/underwater-hoop-01-replay \
  --tracking --tracking-profile ellipse --sample-fps 10 --save-video
```

Replay's annotated review video is separate from the clean training source. Live processing uses the low-resolution stream; replay reads the higher-resolution main stream, so results need not match frame-for-frame even with the same code. Preserve both logs and the original video.

For Testudo labeling, extract a modest number of frames rather than labeling nearly identical video frames. With FFmpeg installed:

```sh
mkdir -p camera_results/testudo-01-labels
ffmpeg -i camera_results/underwater-camera-01/clean.mp4 \
  -vf fps=2 -q:v 2 camera_results/testudo-01-labels/frame-%06d.jpg
```

Capture separate Testudo sessions with different distances, angles, light levels and backgrounds. Include empty scenes and confusing objects. Label bounding boxes on sharp, useful images; preserve useful partial occlusions. Split training, validation and test sets by whole recording session, not random neighboring frames, to avoid inflating accuracy with almost identical images in each set. Keep an untouched underwater session for the final model comparison.

For the hoop, record slow approaches, straight-on and side views, occlusions, exits/re-entry and negative scenes with reflections or white pipes. Manually label a fixed review set before reporting precision/recall. Compare false accepts, rejection rate, ID switches, time to acquire/reacquire, processing latency and timestamp gaps. The existing HSV thresholds were developed outside water and may need adjustment under the final lighting and housing. Axes and front-on cues remain advisory until underwater calibration and camera-to-submarine alignment are validated.

This tool has local simulated-camera checks. Actual Raspberry Pi encoding speed, CSI behavior, underwater image quality and thermal behavior must be measured on the hardware.

Picamera2 references: [official camera software documentation](https://www.raspberrypi.com/documentation/accessories/camera-software.html) and [PyAV timestamp-preserving output implementation](https://github.com/raspberrypi/picamera2/blob/main/picamera2/outputs/pyavoutput.py).
