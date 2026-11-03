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
- `--debug` draws unaccepted proposals and predictions as well as accepted
  measurements. Those extra drawings are diagnostic only.
- `--tracking-profile strict` selects the slower, conservative tracker profile.
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

The native helper explicitly opens the selected camera ID, requests 640×480 when
supported, and samples at no more than the requested rate. `--fps 10` is a sampling
ceiling, not a guaranteed delivery or processing rate. AVFoundation drops late
frames; a Python reader drains packets continuously into one latest-frame slot,
so a slower detector skips frames instead of accumulating a processing backlog.
The tracker uses the frames' actual AVFoundation presentation timestamps rather
than an assumed frame interval. The helper disables output mirroring where the
camera supports that setting.

The preview clears the image and accepted geometry when more than **200 ms** have
elapsed since Python received that frame. Processing time counts toward this
limit. The browser also expires the image locally between requests. A disconnect
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
3. Pause/disconnect the phone. The preview should clear and the command should
   stop on a reported disconnect. A pause without a disconnect may leave the
   command running with an unavailable preview until frames resume.
4. Watch processing time. If results repeatedly expire, try a slower sampling
   rate to reduce load; the 200 ms age gate remains unchanged. A persistent
   processing-time excess needs profiling rather than a weaker freshness gate.

The native helper build and command-line help were checked on the development
Mac. Automated tests exercise transport validation, explicit camera identity,
latest-frame replacement, disconnect/expiry behavior, the local HTTP preview,
and JPEG decoding through ellipse detection and temporal confirmation. Actual
iPhone discovery, camera permission behavior and wireless delivery still require
an end-to-end test with your phone.

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
