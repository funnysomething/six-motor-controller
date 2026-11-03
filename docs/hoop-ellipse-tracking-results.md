# Faster ellipse tracking and camera-relative orientation

This revision addresses the excessively sparse conservative tracker. It combines
the ellipse detector with temporal edge refitting and faster object confirmation.
The branch remains `codex/hoop-temporal-tracking`.

## Changes

- `--tracking` now selects the ellipse profile. The former implementation remains
  accessible with `--tracking-profile strict`; bare library `TrackerConfig()`
  remains strict for compatibility.
- Acquisition requires two measurements spanning 0.1 seconds, rather than five
  spanning 0.5 seconds. Reacquisition requires two spanning 0.05 seconds. Capture
  timestamps still control elapsed time, so this is not a fixed frame-rate rule.
- Remembered ellipses predict where to search for edges in the current frame.
  Refit candidates require current image gradients. Blank frames cannot produce
  guided detections just because a previous track exists.
- The detector retains additional rim hypotheses against neutral backgrounds.
  Brightness alone no longer vetoes a hoop against a gray wall. Distributed
  curve support and secondary boundary evidence remain required to acquire a
  neutral-background hypothesis. Synthetic filled disks are rejected.
- Opening, rim and outline fits can belong to the same object. Overlapping and
  nested hypotheses are grouped for object identity; changing measurement kind
  resets the full geometry covariance instead of averaging incompatible shapes.
- A compatible current edge fit for an established target takes priority over a
  distant new candidate. Multiple openings can block initial acquisition, but a
  far reflection need not reset an already supported identity. Missing current
  evidence, invalid time and stale consumption still withhold output.

## Detection availability on the supplied video

At a sampled 10 fps, the revision produced **77 accepted current-image hypotheses
out of 148 video frames**, compared with 8 previously. First acquisition is at
0.1 seconds. The initial sequence stays accepted from 0.1 through 0.9 seconds.
Thirteen independent photos remain unknown in temporal mode because they have
no preceding video history.

The other sampling runs produced:

- 29 accepted hypotheses out of 74 frames at 5 fps.
- 89 out of 222 frames at 15 fps.
- 419 out of 887 decoded frames at the source frame rate.

The previous conservative version abstained throughout those three runs.
Configurations and full timing scopes are retained in the
[10 fps summary](hoop-vision/ellipse-tracking/10fps-summary.json),
[5 fps summary](hoop-vision/ellipse-tracking/5fps-summary.json),
[15 fps summary](hoop-vision/ellipse-tracking/15fps-summary.json), and
[source-rate summary](hoop-vision/ellipse-tracking/0fps-summary.json).

These counts measure output availability, not accuracy. The same footage was
used during development. More output does not mean every fit identifies the
correct boundary, physical hoop, or orientation.

## Orientation output

Accepted targets include the actual fitted ellipse, drawn in green in replay.
`image_orientation` reports the axis ratio and an approximate unsigned
obliqueness cue. This cue assumes a circular rim under weak perspective. A wide
band outline or a visible opening assembled from both rims can bias it. Image
ellipse rotation is never reported as yaw.

With a measured camera calibration supplied through `--camera-calibration`, the
solver corrects sampled ellipse points and computes the possible circular-plane
normals in camera coordinates. Each hypothesis includes yaw, elevation and tilt
relative to the optical axis. Resolution mismatches are explicit errors in the
orientation result. No focal length is guessed for the supplied phone footage.

The calibrated model is tested against synthetic projected circular rims with
known intrinsics and yaw/pitch combinations of 0/0, 40/20, -55/15 and 65/-20
degrees. One returned hypothesis matches the true normal within 0.1 degrees in
these ideal synthetic tests. This validates the mathematics, not real-world
angle accuracy or the detector's rim identity.

A single calibrated circle generally has two different plane-orientation
solutions. Both are retained. The normal's sign alone is a separate ambiguity.
See [the circle-pose research](https://arxiv.org/abs/1804.04922).
`signed_direction_resolved` and `orientation_valid` remain false. Reliable
steering still requires measured camera intrinsics, correct physical-rim
identification and evidence that resolves the alternative orientations. The
nominal hoop diameter does not provide the camera calibration or the band depth.

## Remaining failures

Reflection confusion remains. Faster confirmation and weaker evidence improve
availability, but reduce the protection supplied by the strict profile. A
reflection by itself can form a stable, geometrically plausible track. The
known target-free crop evaluation and glass-corner interval must be inspected
alongside output counts; these are included in
[the development evaluation](hoop-vision/ellipse-tracking/evaluation.json).
Specifically, one of six target-free crops formed an accepted track, and four
of nine reviewed glass-corner positions that forbid acceptance were accepted.
This is a known regression against the strict profile, not a validated
misdetection improvement. The faster profile remains experimental.

Low-contrast, clipped and edge-on views still interrupt some tracks. A fitted
ellipse can include a band's silhouette or plant edges, and an approximate
tilt can therefore be inaccurate even when the correct object is localized.
This is an ellipse-based prototype, not a trained general hoop detector.
Navigation-valid flags remain false and no motor commands are connected.

## Tests and artifacts

The last recorded suite passed 63 tests. It covers the strict profile regressions, fast two-frame acquisition,
continuity in the presence of distractors, ambiguous initial acquisition,
cross-kind covariance validity, blank-frame guidance rejection, filled disks,
calibrated synthetic normals and invalid calibration inputs. Numerical runtime
warnings are treated as errors.

See [the current usage instructions](../raspberry_pi/hoop_vision/README.md).
The current local run is `camera_results/hoop-ellipse-tracking-release/`, including
per-frame JSON and `ellipse-tracking.mp4`. Green ellipses are current image fits.
The video labels unsigned shape cues as approximate and does not display a
fabricated signed yaw for uncalibrated footage.

The [tracked annotated preview](hoop-vision/ellipse-tracking/ellipse-tracking.mp4)
is available with the source checkout; full generated logs remain local.
