# Underwater hoop vision: production development reference

Evidence snapshot: November 1, 2026. Branch `codex/hoop-temporal-tracking`.

For subsequent implementation changes, see the
[HSV white-material gate and its measured tradeoffs](hoop-hsv-color-gating.md)
and the [live iPhone setup guide](iphone-continuity-camera.md). Results below are
the earlier evidence snapshot and have not been replaced by the newer runs.

This is the starting reference for developing the final vision system with the
actual submarine camera, housing and production assembly **inside the water**.
It consolidates current results, failure cases and requirements that must be
revisited before using vision to align or navigate through the hoop.

The current implementation is an offline ellipse-based prototype. It has not
been validated underwater or connected to autonomous motor control. A green
ellipse or an `accepted` image hypothesis is not a production-readiness claim.

## 1. Evidence and version provenance

Development used `ENEE408V Hoop/IMG_7316.JPG` through `IMG_7328.JPG`, thirteen
photos, and `IMG_7329.MOV`, a roughly 14.79-second video. These were captured
outside the water with a different camera. Displayed photos are 4284 by 5712
pixels; displayed video frames are 1080 by 1920 pixels. Rotation metadata is
applied before processing. The video contains 887 decoded source frames.

The footage does not establish the final camera's intrinsics, underwater
appearance, housing refraction, crop, extrinsics or field of view. No actual
submarine-camera underwater footage or measured real-world pose ground truth
was used for the reported results. This is development data, not a held-out
underwater test set.

The committed base is `b2dc9be92355b6a434a966d489d9f87d2cd2bdcf`, but the vision
implementation and reports are uncommitted working-tree changes at this
snapshot. That commit alone does not identify the tested implementation. See
[the provenance manifest](hoop-vision/production-reference-manifest.json) for
SHA-256 hashes of current source, fixtures, media and saved result files.
Current hashes do not retroactively prove the exact executable bytes used in
every historical run. Only the latest run configurations and full logs remain;
earlier comparison counts survive as historical summaries, not archived runs.

## 2. Pipeline and code map

The implementation combines bright/low-saturation material masks, grayscale
edges, ellipse proposals, boundary checks and temporal tracking. It is not a
trained neural hoop detector. Existing Testudo weights do not detect hoops.

- [`detector.py`](../raspberry_pi/hoop_vision/detector.py) generates proposals.
- [`verification.py`](../raspberry_pi/hoop_vision/verification.py) checks image
  support around curves, using different ellipse/strict profile gates.
- [`ellipse_guidance.py`](../raspberry_pi/hoop_vision/ellipse_guidance.py) refits
  remembered ellipses to current edges.
- [`tracker.py`](../raspberry_pi/hoop_vision/tracker.py) maintains motion estimates,
  object history, uncertainty, confirmation, loss and reacquisition.
- [`orientation.py`](../raspberry_pi/hoop_vision/orientation.py) supplies unsigned
  shape cues and calibrated circular-plane normal hypotheses.
- [`replay.py`](../raspberry_pi/hoop_vision/replay.py) and its
  [launcher](../scripts/vision/hoop_replay.py) export JSON and diagnostic media.
- [`evaluate_hoop.py`](../scripts/vision/evaluate_hoop.py) measures development
  region overlap, accepted outputs and negative-crop behavior.

The CLI's `--tracking` defaults to the faster ellipse profile. Use
`--tracking-profile strict` for the conservative version. Bare library
`TrackerConfig()` remains strict; `TrackerConfig.ellipse()` selects fast settings.
Do not assume CLI and bare-library defaults are identical.

Several ellipse-profile thresholds and continuity weights are hardcoded. A
configuration JSON is not a complete model specification. Preserve source
hashes as well as configuration and calibration files.

## 3. Measured results and their limits

The conservative temporal profile accepted 8 of 148 sampled video frames at
10 fps and abstained throughout its 5 fps, 15 fps and full-rate runs. It rejected
the known negative crops but withheld too much useful evidence. This historical
comparison is retained here; superseded experiment runs and reports were removed
during repository cleanup.

The revised ellipse-first profile produced:

- **10 fps:** 77 accepted image hypotheses out of 148 video frames. First
  acquisition was at 0.1 seconds; the first sequence spans 0.1 through 0.9
  seconds. Thirteen independent photos remained unknown because they have no
  temporal history.
- **5 fps:** 29 accepted hypotheses out of 74 frames.
- **15 fps:** 89 out of 222 frames.
- **Full source rate:** 419 out of 887 frames.

These are availability counts, not true-positive counts, precision, recall or
passage success rates. They do not prove correct physical identity or correct
rim geometry. Whole-band localization, apparent-opening localization and
recovery of one physical circular rim are separate measurements.

Known false acceptance in the faster profile is material:

- One of six target-free crops, the floor reflection, formed an accepted track.
  The diagnostic repeats each still fifteen times at 10 fps. It is not natural
  negative video or a deployment false-positive-rate estimate.
- Four of nine reviewed glass-corner positions where acceptance is forbidden
  were accepted.
- Of 52 reviewed still/video positions, 14 had accepted output and only 5 of
  those passed the recorded measurement-kind-aware coarse IoU 0.3 check. This
  is a diagnostic count, not clean precision: labels are coarse, some are
  shared across intervals, measurement kinds differ, and eight opening boxes
  were added after inspecting earlier accepted outputs.
- The evaluation reports 19 accepted-track-ID changes. These are switches
  between software hypotheses, not 19 proven physical identity errors.

The footage was repeatedly used during tuning. Increased acceptance is not
evidence of reduced misdetection. Preserve the user's policy: prefer unknown
to a wrong target or steering angle.

Sources of record are [the revised results](hoop-ellipse-tracking-results.md),
[evaluation JSON](hoop-vision/ellipse-tracking/evaluation.json) and
[saved summaries](hoop-vision/ellipse-tracking/10fps-summary.json).

## 4. Limitations the underwater successor must address

### Geometry and orientation

The hoop is a broad band with two rims separated along its axis, not a thin
planar ring. An oblique visible opening can combine arcs from both rims. An
ellipse fitted to that mixed boundary or the whole band need not be the
projection of one physical circle. Clips, seams, deformation and occlusion
further violate an ideal circular model.

The uncalibrated `acos(minor_axis/major_axis)` cue is approximate unsigned
obliqueness under a weak-perspective circular-rim assumption. It combines yaw
and pitch effects and is sensitive near circularity. Ellipse image rotation is
not yaw. Pointing toward the image center does not establish alignment with
the passage axis or the correct vehicle position.

A single calibrated circle generally admits two different plane-orientation
solutions, in addition to the interchangeable signs of a plane axis. Smooth
tracking cannot guarantee the correct branch. See
[the circle-pose study](https://arxiv.org/abs/1804.04922).

Ideal synthetic projections at yaw/pitch pairs 0/0, 40/20, -55/15 and 65/-20
degrees recover one matching normal within 0.1 degrees. This validates solver
mathematics, not detected-rim correctness or real angle accuracy. Real yaw,
pitch, distance and metric center-position accuracy remain unmeasured.

### Reflections, appearance and tracking drift

A reflection can satisfy image checks and remain stable. Tracking can reinforce
a wrong initial identity. Guided refitting can drift to plants, glass edges or
a different boundary. Persistence is not evidence of physical target identity.

Gray/white backgrounds can merge with material masks. Clipped and edge-on views
can remove the information needed for ellipse fitting. Faster grouping retains
identity across boundary changes but can also group incorrect curves. It is a
heuristic, not a measured two-rim reconstruction.

In-water housing reflections, lighting, color balance, particles, bubbles,
glare, motion blur and visibility must be measured. Outside-glass artifacts may
change or disappear while other reflections remain. Moving the camera inside
the tank does not establish that current thresholds transfer or reflections
are solved.

### Output meaning

`decision: accepted` means an accepted current **image hypothesis**, not motion
authorization. `detection_valid`, `target_identity_verified`, `pose_valid`,
`orientation_valid` and `signed_direction_resolved` remain false where emitted.
Metric `yaw_deg`, `pitch_deg`, `center_m` and `passage_axis` are not validated
outputs. Per-hypothesis angles inside `image_orientation` do not override those
flags.

Keep raw candidates, predictions, image acceptance, physical identity, pose
validity and navigation eligibility separate in the final API. A missing frame
or unresolved branch must not silently reuse the last steering command.

## 5. Final optical assembly and underwater calibration

Freeze and record sensor, lens, focus, resolution, sensor mode, crop, rotation,
exposure/white-balance policy, housing-window shape/material/thickness,
lens-to-port spacing and rigid mount. Assign assembly/calibration IDs. Existing
Pi 4B/IMX500 support is not proof that those exact final optics and settings have
been selected, assembled or calibrated.

Measure the camera-to-vehicle rigid transform, including translation. Camera
axes are x right, y down, z forward; define body axes explicitly. Apply the full
transform to position and rotation to orientation. A centered off-axis camera
does not imply a centered vehicle. Measure camera-to-IMU rotation and timestamp
alignment before using IMU motion to explain image changes.

Calibrate the assembled optical system in a separate permitted underwater
setup. No modifications to the operating environment are allowed, including
hoop markers, paint or added lighting fixtures. If targets/fixtures are not
permitted in the operating tank, calibrate elsewhere and verify the result
through allowed measurements in the actual tank. Temporary calibration
equipment is not an assumed exception to the restriction.

Refraction depends on the water/glass/air interfaces and housing geometry.
Ordinary lens-distortion parameters may approximate a limited volume without
remaining accurate across ranges and image locations. Validate angle and
position error underwater throughout the intended volume. Use refractive
calibration/projection when residual errors exceed the navigation budget;
confidence thresholds cannot correct systematic optical error. Primary
references include [refractive camera/housing calibration](https://arxiv.org/abs/2405.18018)
and [the Pinax model](https://www.sciencedirect.com/science/article/pii/S0029801817300434).

The current orientation API accepts a pinhole intrinsic matrix and OpenCV
distortion coefficients, not an arbitrary refractive ray model. Extending
calibration without updating the pose projection model would be incomplete.
Revalidate after optical/mount changes and explicitly transform intrinsics for
resize, crop and rotation. Neither a matching resolution nor phone calibration
is sufficient to identify the production camera's calibration.

## 6. Geometry, clearance and operating limits

User-provided dimensions are a nominal 12-inch hoop diameter, 10-inch vehicle
width, 16.31-inch vehicle length and 3.29-inch main-tube inner radius.

If the actual clear opening is 304.8 mm and the full outside vehicle width is
254 mm, nominal horizontal clearance is 25.4 mm per side. This excludes vertical
extent, protrusions, deformation, alignment error and motion. Tube inner radius
does not establish outside hull or thruster height. Fit has not been certified.

Measure minimum clear opening, both rim shapes, band depth, protrusions and the
complete vehicle/guard/cable envelope. Model the vehicle swept through both
rims and the finite tube, including roll, pitch, yaw, camera offset and travel
direction. Do not validate only a center point or image-box width.

Derive position, angle, freshness and speed limits from a budget covering
localization, pose-branch uncertainty, optical error, shape error, tracking,
dynamics and stopping distance. Account for coupled errors conservatively
rather than assuming independence. Earlier suggested 3-degree/10-mm targets
are neither demonstrated performance nor sufficient passage criteria.

At 0.1 m/s lateral drift, 100 ms consumes 10 mm and 300 ms consumes 30 mm. These
are illustrative calculations, not measured vehicle speeds. The latter exceeds
nominal side clearance before other errors are included.

## 7. Final-camera dataset and ground truth

Capture from inside the water with the camera in its final housing and mount
on the production assembly. Retain original frames and capture timestamps before
overlays. Record assembly/calibration IDs, camera settings, water/lighting
conditions, dropped frames and synchronized IMU/motor telemetry when available.

Use separate sessions covering:

- Both signs of yaw/pitch, level/rolled views and both hoop faces.
- Centered but oblique targets, and axis-aligned but laterally displaced targets.
  Vary heading and position independently rather than conflating their errors.
- Near/far ranges, vehicle heights, approach/departure, lateral motion, camera
  turns, reversals and disturbances.
- Gray/white backgrounds, reflections, plants, glass, partial/edge-on targets,
  bubbles, blur, glare, no-hoop scenes and target loss/reentry.
- Actual variable 10 fps capture, delivery delays, overload, frozen-camera
  behavior and dropped frames. A responsive preview is not an age measurement.
- Close approach where rims leave the field of view. Determine the estimation
  handoff before attempting passage; detection loss is not proof of exit.

Label physical identity, whole band, visible front/rear rim arcs, apparent
opening, reflection distractors, occlusion, ambiguity and pose eligibility.
Retain uncertain labels as uncertain. Add independently measured relative
position/attitude with reference uncertainty. An external camera looking through
glass requires its own calibrated geometry; it is not automatically ground
truth. Use permitted measurements without changing the tank.

Split training, tuning and final evaluation by complete capture session or
independent run, not neighboring frames. Reset tracks between evaluation
sessions. Synthetic images test geometry and failure handling but do not
replace in-water evidence. Lock the final test set before tuning thresholds.

## 8. Main-model architecture and uncertainty

Keep region detection, physical-rim extraction, pose-hypothesis generation,
temporal association and navigation eligibility independently observable.
Benchmark classical proposals against a compact learned region or rim-segmentation
model trained on actual underwater data. A learned model is a candidate
improvement, not a substitute for calibration or geometric ambiguity checks.

Use measured finite-depth hoop geometry for joint rim fitting and visibility.
Associate front/back evidence explicitly. Retain alternative poses when residuals
and uncertainty cannot distinguish them. Additional measured geometry, allowed
viewpoint changes with observed translation, or independent scene constraints
may resolve ambiguity. Pure rotation supplies no translation baseline; motor
percentages are not displacement measurements.

Estimate uncertainty from real residuals. Retune motion noise, innovation gates
and dwell time using final-camera motion. A 0.1-second acquisition window and
heuristic continuity bonus are not calibrated probabilities. Neighboring frames
are correlated. Require current evidence and consumption-time expiry.

## 9. Live runtime and motor-control integration

Desktop testing used Python 3.9.6, OpenCV 4.14.0, NumPy 2.0.2 and Pillow 11.3.0.
The last recorded suite passed 63 tests. These check software regressions and
ideal geometry, not production camera/control behavior. The separate existing
camera-control module requires Python 3.10 syntax and was outside that test
environment's camera-suite coverage.

At 10 fps, recorded desktop median/p95 image processing was about 20.4/38.4 ms,
verification 6.1/7.0 ms and tracking 0.28/0.82 ms. In the fast pipeline,
`processing_ms` includes detector plus guided refitting even though the saved
summary's historical timing-scope text says detector only. The measured p95
of per-frame stage sums was about 45.7 ms. Do not sum separate percentiles to
claim a total percentile. These numbers exclude capture, decoding, rendering
and transport; none establishes Pi throughput.

On the target computer, measure capture-to-consumption age and stage timing
with the complete production software running. Use one camera owner and a
bounded latest-frame path. Preserve capture-time provenance and map camera,
IMU and consumer clocks explicitly. Media PTS is not host monotonic time;
fabricated fallback timestamps must not silently establish measured motion.
The replay assumes zero delivery age, whereas the final adapter must measure
it and enforce expiry even when no new frame arrives.

The GUI camera worker is Testudo-specific, not an existing live hoop pipeline.
Test capture, deployment backend and postprocessing with the actual Pi/camera
environment. The desktop headless-OpenCV environment is not a full camera setup.

Use one motor-control owner to combine navigation with leveling, height and
heading control. On this branch's base, manual `set_speeds` cancels automatic
depth control; it is not a multi-controller arbitration API. A newer stabilization
runtime remains on a separate branch and its inspected allocator rejects
forward/lateral effort. Resolve that integration explicitly.

Vision loss should withdraw navigation effort under a tested policy while any
separately validated stabilization follows its own rules. Motor-link heartbeat
health does not validate vision. A range reading through the opening may measure
background, not the hoop. Track identity and pose need independent freshness.

## 10. Evidence required before production steering

Define release limits from measured clearance, vehicle dynamics and stopping
behavior. Require the following on locked independent in-water evaluation runs:

- Physical-target precision/recall, false-lock duration and abstention coverage
  by condition, with denominators and reference uncertainty.
- Correct signed yaw/elevation and metric center/range errors across the tested
  volume, including wrong-branch and calibration-mismatch handling.
- Track switching, fragmentation, acquisition/reacquisition delay and occlusion
  drift. A long stable false track is a failure, not continuity success.
- End-to-end age, overload recovery, dropped/frozen-frame handling and target
  computer throughput under the complete production workload.
- Controlled zero-forward-motion alignment, then positioning/standoff, followed
  by approach/pass trials only after swept-envelope and loss-policy validation.
- Recovery/exit evidence independent of simply losing sight of the hoop.

Zero accepted results is not useful success, and high acceptance counts are not
precision. Do not declare readiness from unit tests, this phone clip, an
attractive overlay or the absence of observed errors in a small sample.

## 11. Current artifacts and reproduction

Latest tracked video relative to the repository root:

`docs/hoop-vision/ellipse-tracking/ellipse-tracking.mp4`

[Open the latest video](hoop-vision/ellipse-tracking/ellipse-tracking.mp4).
The full `observations.jsonl`, `summary.json` and `evaluation.json` remain in
`camera_results/hoop-ellipse-tracking-release/` locally. Generated runs under `camera_results/` are ignored by git and
may be missing from another checkout. Compact evidence is retained under
[`docs/hoop-vision/ellipse-tracking/`](hoop-vision/ellipse-tracking/10fps-summary.json).
Archive raw media, logs, configurations, calibration, hashes and environment
versions explicitly when transferring work; a branch name is insufficient.

Use [the setup/replay instructions](../raspberry_pi/hoop_vision/README.md).
Use [the latest tracker results](hoop-ellipse-tracking-results.md) for measured
behavior and known failures. Superseded plans, reports and intermediate replay
runs have been removed. The original media, latest implementation, tests,
expanded review labels and four final frame-rate runs remain. This document
contains the consolidated roadmap for the underwater successor.

## 12. Prioritized improvement roadmap

The following are proposed changes, not capabilities already demonstrated.
Improve correct accepted measurements at a controlled false-acceptance rate.
Do not optimize the number of green frames by itself. Keep the tank and hoop
unchanged throughout this work.

### Priority 1: make failures measurable before further tuning

**Can begin now.** The current 77/148 availability result does not distinguish
correct rim fits from wrong boundaries, and the small label set mixes several
geometric meanings.

Create a versioned, contiguous frame review set covering complete approach,
reflection, loss and reacquisition sequences. Label the physical target,
band region, visible rim arcs and apparent opening separately. Preserve the
current labels/results as historical evidence rather than rewriting them to
make new predictions look correct. Keep uncertain identity explicitly uncertain.

Add a repeatable comparison tool for stateless ellipses, strict tracking and
ellipse-first tracking. Compare identical frames, calibration and measurement
definitions. Record false accepts, false-lock duration, correct accepted
coverage, identity switches, reacquisition delay and runtime by condition.
Evaluate image presence and physical-rim/pose correctness separately.

Move hardcoded profile thresholds and continuity weights into validated
configuration. Log the effective gates, source version and calibration ID, and
correct the image-processing timing label to include guided refitting.

**Evidence to proceed:** reproducible comparisons expose the known floor and
glass-corner failures and can distinguish a coverage gain from an accuracy gain.
Independent underwater sessions will later replace the phone footage as the
release evaluation set.

### Priority 2: strengthen geometry and keep tracking from validating itself

**Can begin now with synthetic and existing footage.** The current fit may
combine plants, multiple rims and band outlines. Guided fits can reinforce a
wrong track.

Use robust arc fitting with outlier rejection, distributed angular support,
edge-direction agreement and residuals evaluated on image support beyond the
samples that generated the proposal. Represent front/back rim hypotheses
separately from the object-level identity. Test measured two-rim models once
band depth and shape are available; do not assume concentric image ellipses.

Periodically compare the guided fit against a fresh full-frame detection.
Measure drift and trigger reacquisition when they disagree. As an optional
experiment, track local features near the band using pyramidal Lucas-Kanade
flow and reject inconsistent forward/backward matches. OpenCV provides the
[feature-tracking implementation](https://docs.opencv.org/4.x/d4/dee/tutorial_optical_flow.html).
This may help motion association, but smooth white surfaces, blur, changing
illumination and reflections can defeat it. Treat it as supporting evidence,
not proof of physical identity or a replacement for current-frame detection.

Keep fast image-presence confirmation separate from stricter physical identity
and pose confirmation. The system can show a plausible hoop quickly while
withholding a steering angle. Estimate localization uncertainty by perturbing
edge samples and fitting parameters, then compare those estimates with real
residuals instead of treating weighted scores as probabilities.

**Evidence to proceed:** fewer incorrect boundaries and shorter false locks
without losing useful correctly accepted intervals. Prediction-only and blank
frames still withhold accepted current measurements. Stable isolated reflections
must remain a documented limitation until independent evidence distinguishes them.

### Priority 3: establish final optics, geometry and in-water reference data

**Requires the actual camera, housing and vehicle assembly.** Freeze the optical
configuration, measure the hoop/vehicle geometry and camera-to-body transform,
and calibrate as described in sections 5 through 7. Verify calibration across
range, image position and viewing angle before trusting metric outputs.
Use a refractive model if measured errors require it, with a compatible pose
projection model. Refractive calibration depends on the housing and optical
interfaces, not just the camera sensor. See
[the underwater calibration research](https://arxiv.org/abs/2405.18018).

Capture independent final-camera sessions inside the water, including empty
scenes and difficult reflections. Reserve sessions for final evaluation before
training or tuning. Keep calibration, assembly and ground-truth uncertainty with
each session. Do not spend all collected data repeatedly tuning one video.

**Evidence to proceed:** reproducible pixel-to-ray geometry and independent
position/angle references over a declared operating volume. Matching the
appearance of the phone footage is not the success criterion.

### Priority 4: benchmark a learned region/rim model against the classical baseline

**Prepare labels now; train the main model on final-camera underwater data.**
Use a compact detector or segmentation model to propose the hoop band and, if
the data supports it, visible rim boundaries. Continue using geometry for pose
and tracking for identity; a bounding-box detector alone does not provide the
passage axis.

Mine incorrect accepts from logs and include plants, white/gray surfaces,
partial rings and reflections as hard negatives or explicitly annotated
distractors. Do not assign confident real/reflection labels where the reference
cannot distinguish them. Use realistic color, exposure, noise and blur
augmentation, and synthetic calibrated rim views for geometry coverage.
Transform orientation labels consistently with augmentation. These techniques
supplement real underwater sessions; they do not replace them.

Compare classical-only, learned-only region proposals and a hybrid under the
same held-out sessions and compute budget. Choose deployment format only after
checking the actual Pi/camera backend. There is no evidence yet that a larger
network, a particular model family or IMX500 inference will be the best choice.

**Evidence to proceed:** fewer false physical-target accepts and better correct
coverage across viewing angles on independent sessions, with acceptable target
computer latency. Reject a model that improves easy views but prolongs false
reflection locks.

### Priority 5: resolve pose ambiguity with physical evidence

**Needs calibration and measured rim geometry.** Fit both rims jointly with
visibility/self-occlusion and robust reprojection error. Track multiple pose
branches rather than selecting the branch closest to an arbitrary prior.
Use synchronized calibrated IMU rotation to explain vehicle rotation, not to
invent unmeasured translation.

Test whether allowed observed translation, known rim separation, or independently
measured scene constraints can distinguish branches. For example, a validated
floor plane and camera height might reject an estimated target below the floor.
Do not add that rule until the plane, transform, range association and their
uncertainties are measured. A downward range sensor by itself does not identify
which forward ellipse is the real hoop.

Validate position, normal direction and signed angle on both sides of zero,
including nearly frontal and edge-on cases. The pose gate must reject unresolved
branches, wrong calibration and mixed-rim fits even if image detection remains
accepted.

**Evidence to proceed:** independently measured signed pose errors and branch
failures fit the clearance budget. A low residual or smooth normal alone is
insufficient.

### Priority 6: validate real-time integration before approach control

**Needs the production computer and assembled submarine.** Implement the live
camera adapter, shared clock mapping, latest-frame delivery and measured
consumption age. Test frozen frames, dropped frames, overload and camera restarts.
Keep image processing independent of the single motor owner and expose explicit
unknown/expired outputs.

Progress through read-only guidance, zero-forward-motion heading alignment,
positioning/standoff and finally controlled approach trials. Test the
near-field handoff when rims leave view before attempting passage. Use the
swept-envelope and stopping-distance limits from section 6, not a fixed image
centering tolerance. Preserve a separately tested loss/recovery policy at every
stage.

**Evidence to proceed:** the complete system meets the section 10 release gates
on independent in-water runs, including perception failures and control latency.
Select and freeze the production profile only after these measurements.

### Recommended immediate next implementation

Start with Priority 1's failure-oriented evaluator and complete sequence labels,
then Priority 2's independent guided-fit checks and boundary-model tests. This
addresses the known reflection and drift regressions without simply increasing
dwell time or lowering thresholds again. In parallel, prepare the final optical
assembly and capture protocol so the main-model work can move onto genuine
underwater data as soon as the hardware is available.
