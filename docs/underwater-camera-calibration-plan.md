# Underwater camera calibration plan

Prepare the Raspberry Pi AI Camera for clear, repeatable underwater capture and validate vision before connecting it to submarine motor control.

## Constraints and setup

- Use the actual IMX500 camera, Raspberry Pi, final housing window and camera-to-window position. A temporary rigid mount is sufficient before submarine integration.
- Keep lighting unchanged. Do not add markers, change the hoop or modify the operating environment.
- Adjust the camera's mechanical focus only while the housing is open and dry. Reseal before every underwater test.
- Judge clarity using full-resolution recordings on the Mac. The current browser preview is only 640 pixels wide.
- Treat image quality, geometric calibration and detector performance as separate checks. Sharper video does not guarantee correct detections or pose.

See [recording and replay instructions](pi-underwater-testing.md) for capture commands, file locations and GitHub setup.

## 1. Define the operating range

- [ ] Specify the nearest and farthest distances where Testudo detection and hoop tracking must work, including the final hoop approach.
- [ ] Choose several representative distances across that range. Starting examples are 0.3, 0.5, 1 and 2 metres; replace these with the distances the submarine actually needs.
- [ ] Choose repeatable front, oblique and side views, using the existing targets.

**Complete when:** the required distances and views are written down, so every focus/settings comparison uses the same conditions.

## 2. Establish focus and optical clarity

- [ ] Clean the housing window; check for condensation, bubbles, reflections and scratches. Secure the camera without cable strain.
- [ ] Record stationary underwater views at each test distance with consistent camera settings.
- [ ] Adjust focus in small increments between runs. Photograph or mark the lens position on the camera assembly so settings can be compared and restored.
- [ ] Select one focus setting that preserves useful hoop-edge and Testudo detail across the required range. Check the centre and edges of the image.

**Complete when:** stationary images are acceptably clear across the required range. If no focus setting covers that range, resolve the optical limitation before tuning detection thresholds.

## 3. Tune exposure, gain and color

- [ ] Use a fixed sensor mode, resolution and frame rate while comparing settings. Start with automatic exposure/white balance and save the applied metadata after settling.
- [ ] Compare a small set of exposure times around that baseline, first stationary and then with realistic slow camera motion. Shorter exposure reduces motion blur but requires more gain under fixed lighting.
- [ ] Choose the lowest gain that preserves useful target detail without excessive darkness, noise or clipping of the white hoop.
- [ ] Compare automatic white balance with gains locked after settling underwater. Test multiple viewing directions.
- [ ] Keep sharpening and denoising modest initially. Change one setting at a time and retain the original footage.

**Complete when:** the selected settings produce repeatable, useful target edges and color during stationary and moving tests. Record the tested limitations rather than assuming all distances and orientations work.

The recorder currently exposes resolution, frame rate and bitrate. Exposure, gain, white balance and image-processing controls still need a software update for convenient live tuning. These controls are supported by Picamera2; they are not implemented as recorder sliders or command-line options yet.

## 4. Verify recording performance

- [ ] Compare 720p and 1080p using the chosen optical settings. A 16 Mbps recording is a starting comparison, not a guaranteed optimum.
- [ ] Re-test maximum-rate recording after tuning at a fixed rate. Confirm the applied exposure and image quality still meet the requirements.
- [ ] Inspect `session.json`, encoded timestamp gaps and actual video playback. Keep `clean.mp4` and the derived `clean-10fps.mp4`.

**Complete when:** sustained recording and live processing work at measured rates without unacceptable gaps or quality loss. A derived 10 FPS video does not change the original exposure or motion blur. Maximum-rate capture and 10 FPS live processing are separate rates.

## 5. Calibrate underwater geometry

- [ ] Freeze focus, sensor mode, resolution, crop and camera-to-window position before geometric calibration.
- [ ] If permitted in a separate calibration setup, use a rigid, measured checkerboard in representative water with the same housing. Do not introduce a target into the operating environment without permission.
- [ ] Capture approximately 20–30 sharp, varied views spanning image positions, distances and tilts. Reserve additional views for validation.
- [ ] Estimate the camera matrix and distortion, then evaluate held-out reprojection errors and systematic errors across distance and image position.
- [ ] Verify calibration for the actual vision stream. Do not apply main-stream parameters unchanged to a resized or differently cropped stream.

**Complete when:** held-out geometric checks meet the alignment requirements. Define numerical tolerances from the required alignment accuracy before accepting calibration. A low fitting error alone does not establish reliable underwater pose.

Standard OpenCV calibration is a practical baseline. Flat-port refraction can produce errors that require an explicit refractive model. If no calibration target/setup is permitted, document that geometric calibration remains incomplete and withhold validated metric-pose claims.

## 6. Validate vision, then verify installation

- [ ] Freeze the camera profile and capture separate underwater evaluation sessions: approaches, side views, occlusions, exits/re-entry, reflections and negative scenes.
- [ ] Retune hoop HSV/edge checks using underwater footage. Train/evaluate Testudo using session-separated datasets.
- [ ] Manually label a fixed review set. Measure false accepts, missed detections, acquisition/reacquisition time, track switches and processing latency. Acceptance counts alone are not accuracy.
- [ ] Set application-specific acceptance criteria before evaluating the held-out sessions.
- [ ] After submarine installation, repeat clarity and recording checks, verify geometric calibration, and measure camera alignment relative to the submarine. Recalibrate if the optics, focus, mode or housing geometry changes.

**Complete when:** held-out performance is acceptable and repeatable. Connect vision to motor control only after separately validating alignment outputs and failure handling.

For context, the current tracker confirmed zero tracks across the 2,306 decodable frames of `underwater_test1.mp4`. That is a development baseline, not a labeled accuracy measurement or proof that camera clarity alone caused the failures.

## Keep with each selected camera profile

Save the Git commit, camera/housing identification, mount measurements, focus reference photo, sensor mode, resolution/crop, requested and achieved FPS, bitrate, applied exposure/gain/white-balance metadata, processing settings, test distances/views, representative recordings, calibration files and held-out results. Note water conditions and existing lighting.

Archive generated footage separately; `camera_results/` is ignored by Git. Keep the selected settings and calibration metadata versioned with the project.

## References

- [Raspberry Pi AI Camera specifications](https://www.raspberrypi.com/products/ai-camera/)
- [Picamera2 controls and metadata](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf)
- [OpenCV camera calibration](https://docs.opencv.org/4.x/dc/dbb/tutorial_py_calibration.html)
- [Refractive underwater calibration](https://arxiv.org/abs/2405.18018)
