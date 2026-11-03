# Offline hoop vision

The default ellipse tracker now uses a
[slow-motion local continuity lock](../../docs/hoop-continuity-lock.md).
It prefers the confirmed hoop's nearby current measurements and allows bounded
weaker maintenance. Use `--no-continuity-lock` to disable that path for comparison.

The current pipeline applies an [HSV white-material gate](../../docs/hoop-hsv-color-gating.md)
to new ellipse proposals and current tracked refits. That document records its
defaults, tuning options and the latest results. The older ellipse results below
describe the revision before this color gate.

For a live wireless iPhone stream on macOS, see the
[Continuity Camera setup and live tracker guide](../../docs/iphone-continuity-camera.md).
Run `scripts/vision/hoop_live.py --list-cameras` with the environment below, then
select the iPhone explicitly using `--camera-name` or `--device-id`.

Start with the [underwater production reference and roadmap](../../docs/underwater-vision-production-reference.md)
for the final camera, housing and vehicle inside the water. See the
[latest measured results](../../docs/hoop-ellipse-tracking-results.md) for the
prototype's output availability and remaining reflection failures.

This is an experimental ellipse-based detector and tracker, not a trained hoop
recognition model. The supplied footage was captured outside the water using a
different camera. No GPIO, I2C, camera SDK, neural-network weights or submarine
hardware is required for offline replay. No motor output is connected.

## Setup and run

From the repository root:

```sh
python3 -m venv .venv-hoop
.venv-hoop/bin/python -m pip install -r raspberry_pi/requirements-hoop.txt
.venv-hoop/bin/python scripts/vision/hoop_replay.py "ENEE408V Hoop" \
  --tracking --sample-fps 10 --save-video \
  --output camera_results/my-hoop-run
```

Choose a new output directory for each run; existing results are never
overwritten. Input can be one image, one video or a directory of media. Calling
the script by absolute path from another directory works; input/output paths
remain relative to your current directory. Python 3.10 or newer is recommended
for compatibility with the rest of the repository. This offline module also
works on Python 3.9.

`--tracking` defaults to the faster ellipse profile. Use
`--tracking-profile strict` for the conservative fallback. Omitting `--tracking`
runs raw stateless proposals. `--verification-only` applies the selected
profile's image gates without temporal confirmation. In Python, bare
`TrackerConfig()` is strict; `TrackerConfig.ellipse()` selects faster settings.

Useful options:

- `--sample-fps 5`, `10` or `15` selects video frames by source timestamp.
  `--sample-fps 0` processes every decoded frame. Replay does not impose
  real-time pacing or emulate camera latency.
- `--tracking-debug` also draws rejected proposals and internal predictions.
  Normal tracking views draw green only for an accepted current measurement.
- `--max-dimension 640` controls processing size while preserving aspect ratio.
  Photo EXIF orientation and video display rotation are applied first.
- `--saturation-max` and `--value-min` expose material-mask thresholds. Tuning on
  phone footage does not establish that they work underwater. Defaults are
  S <= 60 and V >= 160 on OpenCV's 0..255 S/V scale. `--min-white-support`
  defaults to 0.65; detector and verification use the same white settings.
- `--tracker-config file.json` and `--verification-config file.json` override
  validated configuration fields. Their profiles must match `--tracking-profile`.

## Outputs and latest retained run

Each media run writes `observations.jsonl`, `summary.json`, annotated contact
sheets and sampled review images. `--save-video` writes an MJPEG AVI. The retained
MP4 is a convenience transcode; video transcoding is not a detector dependency.

The latest committed-preview location is
[ellipse-tracking.mp4](../../docs/hoop-vision/ellipse-tracking/ellipse-tracking.mp4).
Its full logs are in `camera_results/hoop-ellipse-tracking-release/`. Final 5 fps,
15 fps and source-rate runs are also retained. Intermediate experiments,
duplicate AVI files and individual review JPEGs have been removed from the
retained runs. `camera_results/` is ignored by git. Compact evidence remains in
[`docs/hoop-vision/ellipse-tracking/`](../../docs/hoop-vision/ellipse-tracking/10fps-summary.json).

## Detection, memory and validity

Material masks and grayscale edges propose ellipses. Boundary checks test curve
support, edge direction and nearby band evidence. Neutral-background rim
hypotheses can use secondary boundary evidence. Some gates are profile-specific
and hardcoded; retain source hashes as well as saved configuration.

The ellipse profile acquires with two measurements spanning 0.1 seconds and
reacquires with two spanning 0.05 seconds. Remembered ellipses guide refitting
against current edges, not predictions presented as detections. Overlapping or
nested rim/opening/outline hypotheses can share object history. A measurement-kind
change resets the geometry covariance instead of averaging incompatible shapes.
Current support for an established identity can outweigh a far new candidate.

The strict profile uses five observations over 0.5 seconds for acquisition,
three over 0.2 seconds for reacquisition, and more restrictive geometry checks.
Both profiles withhold acceptance on missing current evidence, use actual
capture-time intervals and reject invalid or untrusted fallback timestamps.
Internal coasting lasts at most 0.3 seconds; lost tracks expire after 1 second.
Standalone photos cannot establish temporal confirmation.

Schema v2 separates raw `candidates`, `tracks`, `decision`, nullable
`accepted_target`, `tracking_reason` and `image_orientation`. `accepted` means
an image hypothesis, not verified physical identity or movement authorization.
Scores are heuristics, not probabilities. Navigation-valid flags remain false
and metric pose fields remain unavailable. Reflections can still form stable
false tracks, and fitted curves can include plants or mixed-rim boundaries.

For a live consumer, call `tracker.target_at(observation, now)` in the capture
clock domain. It rejects stale, superseded or reset results even when the camera
stops producing frames. Default expiry is 0.2 seconds. Offline replay assumes
zero delivery age and does not validate the complete live clock/transport path.

## Orientation and calibration

Accepted targets include their fitted `ellipse`. Without calibration,
`image_orientation` provides axis ratio and an approximate unsigned tilt cue
under a weak-perspective circular-rim assumption. A wide-band silhouette or
mixed opening can bias it. Ellipse image rotation is not yaw.

Add `--camera-calibration camera.json` with `--tracking` for circular-plane normal
hypotheses. The JSON requires `image_size` as oriented source width/height,
`camera_matrix` as a measured 3 by 3 intrinsic matrix in source pixel units,
and optional OpenCV `dist_coeffs`. Resolution mismatches are explicitly flagged.
No focal length is guessed for the phone footage.

The solver returns candidate normals, yaw, elevation and optical-axis tilt in
camera axes x right, y down, z forward. A calibrated circle generally has two
possible plane orientations, so alternatives remain unresolved. Physical-rim
identity and orientation validity are not established by calibration alone.
See the [production reference](../../docs/underwater-vision-production-reference.md)
for underwater refraction, measured two-rim geometry and navigation requirements.

## Evaluate and test

```sh
.venv-hoop/bin/python scripts/vision/evaluate_hoop.py \
  camera_results/my-hoop-run/observations.jsonl \
  --output camera_results/my-hoop-run/evaluation.json --media-root "ENEE408V Hoop"
.venv-hoop/bin/python -W error::RuntimeWarning -m unittest \
  test.test_hoop_vision test.test_hoop_tracking test.test_hoop_verification \
  test.test_hoop_ellipse_tracking test.test_hoop_orientation test.test_vision_launchers
```

The single current fixture, `test/fixtures/hoop-review-labels.json`, contains the
expanded 52-position development review, measurement-kind-specific boxes where
reviewed, ambiguous glass-corner labels and six target-free crops. It is not
held-out accuracy or pose ground truth. Box IoU 0.3/0.5 checks do not certify a
correct physical rim. The optional `--media-root` evaluates negative crops with
the run's saved configuration; repeated stills are not independent negative video.

## Frozen-candidate replay

```sh
.venv-hoop/bin/python scripts/vision/hoop_replay.py \
  camera_results/my-hoop-run/observations.jsonl --candidate-json \
  --output camera_results/my-hoop-frozen-run
```

This changes association on saved proposals without decoding media. It cannot
regenerate guided candidates or pixel verification after a tracker change; use
a full media run for end-to-end comparisons. Unverified v1 logs cannot acquire
a target. Optional `delivery_age_seconds` tests delayed consumption and
`scene_cut: true` resets tracking. Do not rewrite capture timestamps to simulate
delivery delay. Wall-clock timing may vary between otherwise repeatable replays.
