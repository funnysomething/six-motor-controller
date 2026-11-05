# Live hoop tracking with an iPhone

Use an iPhone as a wireless macOS camera to exercise the existing ellipse detector
and temporal tracker. This preview does not send motor commands. It does not
validate underwater performance or resolve the hoop's signed yaw/pitch.

## Set up Continuity Camera

Apple requires an iPhone XR or newer running iOS 16+, a Mac running macOS 13+, and
the same Apple Account with two-factor authentication on both devices.

1. On iPhone, enable **Settings → General → AirPlay & Continuity** (or **AirPlay &
   Handoff**) **→ Continuity Camera**.
2. Enable Wi-Fi and Bluetooth on both devices. Keep them nearby. Disable Personal
   Hotspot, Mac Internet Sharing, AirPlay and Sidecar during wireless use.
3. Mount the phone securely in landscape, lock it, and point its unobstructed rear
   cameras toward the hoop.
4. Check that FaceTime can select the iPhone in its Video menu; then quit FaceTime
   before starting this program.

See [Apple's setup and troubleshooting guide](https://support.apple.com/en-us/102546).
If the phone disappears after Disconnect, Apple advises reconnecting by USB to
make it available again.

For this CV experiment, turn off Center Stage, Portrait, Reactions and lighting
effects in the Mac's video controls while the capture helper is active. Keep the
phone orientation, zoom and camera mode fixed. Cropping and image effects can
change the geometry the detector measures.

## Install and launch

From this repository's root, create the same lightweight environment used for
offline replay, if it does not already exist:

```sh
python3 -m venv .venv-hoop
.venv-hoop/bin/python -m pip install -r raspberry_pi/requirements-hoop.txt
```

The capture helper needs Apple's Swift compiler and macOS SDK. Check with
`xcrun --find swiftc`; if the command is unavailable, install Command Line Tools
using `xcode-select --install`, finish the macOS installer, and retry. No Swift
packages or GUI-enabled OpenCV installation are needed.

List cameras:

```sh
.venv-hoop/bin/python scripts/vision/hoop_live.py --list-cameras
```

The first run compiles and signs a small local app under the ignored directory
`camera_results/.continuity-camera/`. Allow camera access when macOS asks. If
denied, enable the requesting app in **System Settings → Privacy & Security →
Camera**, then restart the command. Depending on macOS attribution, the requesting
app may be Hoop Camera Bridge or the terminal/host app you launched it from.
The helper never requests microphone access.

Launch using a unique name from the list:

```sh
.venv-hoop/bin/python scripts/vision/hoop_live.py --camera-name "iPhone" --fps 10
```

If multiple cameras match, select the exact `id` printed by discovery:

```sh
.venv-hoop/bin/python scripts/vision/hoop_live.py --device-id 'PASTE_EXACT_ID_HERE' --fps 10
```

A local browser window shows the annotated stream. If it does not open, copy the
printed `http://127.0.0.1:...` URL. No browser camera permission is needed: the
native helper captures video and Python serves the local preview. Stop with
**Ctrl+C** in the terminal; closing the browser tab does not stop capture.

Useful options:

- `--seconds 60` stops automatically after one minute.
- `--no-browser` prints the URL without opening it.
- `--preview-timeout 1.0` sets how long to keep an unannotated image through
  delivery gaps. Detection geometry still expires after 0.2 seconds.
- `--capture-max-dimension 960` caps the delivered JPEG size while preserving
  aspect ratio. Set `0` to use the device's default capture mode without this
  downscaling. Verify the actual dimensions before supplying camera calibration.
- `--debug` draws unaccepted proposals and predictions as well as accepted
  measurements. Those extra drawings are diagnostic only.
- `--tracking-profile strict` selects the slower, conservative tracker profile.
- The ellipse profile uses the [local continuity lock](hoop-continuity-lock.md)
  by default. `--no-continuity-lock` disables its location preference and weaker
  maintenance path for comparison.
- `--camera-calibration file.json` supplies intrinsics for this exact camera,
  image resolution and mode. Do not reuse the original media camera's calibration
  or the future submarine camera's calibration for the iPhone.

## What the preview means

The default is the faster ellipse profile with an
[HSV white-material gate](hoop-hsv-color-gating.md). It combines current ellipse
measurements with temporal association; normal views draw green only for an
accepted current image measurement. Acquisition needs at least two qualifying
observations spanning 0.1 seconds. A visible hoop can still produce unknown when
image evidence fails the geometry or white-color checks. The color gate does not
remove the documented reflection failures. Use `--saturation-max`, `--value-min`
and `--min-white-support` to configure its range and required support.

The native helper explicitly opens the selected camera ID and queries capture
presets after attaching that input. It tries 640×480, then 1280×720 when supported,
and caps the delivered JPEG's longest dimension at 960 pixels by default. The
native-resolution option disables both the smaller-preset preference and JPEG
downscaling. `--fps 10` is a sampling
ceiling, not a guaranteed delivery or processing rate. AVFoundation drops late
frames; a Python reader drains packets continuously into one latest-frame slot,
so a slower detector skips frames instead of accumulating a processing backlog.
The tracker uses the frames' actual AVFoundation presentation timestamps rather
than an assumed frame interval. The helper disables output mirroring where the
camera supports that setting.

Camera preview and detection freshness have separate lifetimes. The capture
reader updates the raw preview independently of the detector. Accepted geometry
and the annotated image expire **200 ms** after Python receives the corresponding
frame, including processing time. The browser then shows an **unannotated** raw
image instead of blanking the video. During short delivery gaps it can retain
that image for up to **one second** from receipt, with a delayed-camera message.
It never renews an old image's lifetime merely because the browser polls again.
If the camera stalls beyond that timeout, the image clears; new frames restore
the preview automatically. A disconnect
or capture error stops the run; restarting requires explicit camera selection.
The program does not open another camera as a fallback.

This freshness limit measures **host receipt to consumption**, not sensor exposure
to consumption. Continuity Camera's wireless and upstream buffering latency is
unmeasured, and AVFoundation timestamps alone do not establish that latency. This
mode is suitable for a visual prototype test; production motion control needs a
verified capture clock, measured end-to-end latency and stale-command gating.

Camera frames are served only on loopback through an unguessable per-run URL,
without external web assets. No media or observation recordings are saved by
default. The compiled helper is the only persistent generated artifact.

## Suggested first test

1. Hold the camera steady with the hoop fully visible. Look for acquisition and
   stable ellipse placement, then approach several oblique angles slowly.
2. Cover the hoop or point away. Accepted output should disappear when evidence
   is lost. Returning to the hoop should require confirmation again.
3. Pause/disconnect the phone. A pause first removes annotations, then clears the
   raw image after the preview timeout. Frames resume automatically after an
   ordinary pause. A reported disconnect stops the command.
4. Watch capture fps, capture age and processing time in the preview. A moving
   raw picture with no detections indicates delayed/rejected vision results.
   A retained image with increasing capture age indicates a delivery gap.
   `--preview-timeout 1.5` can bridge longer brief gaps without extending detection
   validity. If processing time is high, lower `--capture-max-dimension` to 640.

The November 5, 2026 preview-stability fix was checked with the iPhone 17 over
Continuity Camera. A bounded live check received 121 frames at 960×540 over 12.05
seconds, with a reported capture rate of 9.9 fps. All 225 preview polls after the
first result had fresh annotated results. Capture was then stopped. This short
check does not measure sensor-to-host latency or establish reliability during
long wireless interruptions.

Automated tests also exercise independent raw-frame delivery, 300–500 ms capture
gaps, slow vision, expiry of annotations before raw pixels, eventual stall
clearing, disconnects, and protection against late results replacing newer raw
frames. Browser timer checks verify the transition from annotated to raw to
hidden during a stall.

A separate transport simulation before the HSV gate was added sent the supplied video's first three seconds
through JPEG packets and the live processing path at 10 samples per source
second, with the long image side reduced to 640 pixels. Of 30 frames, 11 were
accepted and 19 were unknown; median processing time was 23.1 ms and maximum
32.1 ms on the development Mac. This was an unpaced replay, not a wireless-camera
benchmark or a measurement of detection accuracy. The remaining unknown results
reflect the existing image/tracking gates; live capture does not improve those
gates by itself.

For the final submarine setup, continue with the
[underwater production reference and roadmap](underwater-vision-production-reference.md)
and [known detector limitations](hoop-ellipse-tracking-results.md).
