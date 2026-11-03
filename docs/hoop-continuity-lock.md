# Slow-motion local hoop continuity

Implemented on `codex/hoop-continuity-lock`, based on the live-camera/HSV snapshot
`bdb8ed9` on `codex/iphone-live-hoop-tracking`. The default ellipse profile now
prefers the previously confirmed hoop's local image neighborhood. The strict
profile retains its previous behavior.

## Behavior

Initial acquisition still requires strong, current white-hoop evidence on two
observations spanning at least 0.1 seconds. A weak candidate cannot start a track.

Once a hoop is confirmed, the tracker selects the closest compatible current
ellipse before assigning tracks. It prefers the held ID over a different
confirmed track with a higher edge score. Center proximity, similar axes,
boundary kind and ellipse direction contribute to preference. Near circles have
less directional preference because their major-axis direction is unstable.

The default local center gate is 0.10 in image-width/image-height normalized
coordinates. The normal axes gate is a maximum log-axis change of 0.35, about
42% on either axis. A matching nearby boundary wins over a larger or distant
higher-scoring alternative. A lagging bounding-box filter cannot veto a current
fit that passes the explicit local gate.

If only a strongly verified alternative boundary is available on the same nearby
object, the tracker can keep the object ID through a larger axes change, up to
0.7 in log axes, about twice the size. This fallback requires full verification,
strong evidence and compatible overlapping/nested boxes. It is lower priority
than a stable-boundary measurement. A changed boundary temporarily withholds the
unsigned tilt cue, including when camera calibration is supplied. Two subsequent
stable boundary updates are required to show that cue again.

## Weaker maintenance, bounded authority

For an already confirmed local hoop, the verifier supplies a separate
`maintenance_passed` result. It allows at least 35% oriented-edge coverage across
four sectors, material support of 0.4 and band-side polarity of 0.2 for ordinary
outlines/openings. The HSV white support, white-sector distribution, bounded-band
check and clipping rejection remain required. These relaxed checks cannot be
used for acquisition or by a distant candidate.

Weak-only maintenance expires 0.6 seconds after the last fresh, strongly verified
observation. Weak updates cannot refresh this budget or walk outside the center
and axes limits relative to that strong observation. Stale strong frames cannot
renew the budget. This prevents indefinite drift from a chain of marginal fits.

A missing or rejected current measurement immediately returns `unknown`, without
an accepted ellipse. The internal identity remains available for local search
for up to 0.8 seconds after its last observation. Two qualifying local current
observations can reconfirm it after a short miss; sufficiently recent weaker
evidence can participate in that reconfirmation. Once this bounded hold expires,
normal acquisition can select another target. Unused tracks are removed after
two seconds. No prediction is published as a current detection.

This preserves continuity when current evidence supports the same area, while
still allowing loss on obstruction, clipping, disappearance and excessive gaps.
It does not establish physical target identity against white reflections.

## Run and configure

The feature is enabled automatically in the default live command:

```sh
.venv-hoop/bin/python scripts/vision/hoop_live.py --camera-name "iPhone" --fps 10
```

It also applies to `hoop_replay.py --tracking`. Use `--no-continuity-lock` on
either launcher to disable the local preference and weaker-maintenance path.
This flag does not restore every historical configuration value. In Python,
use `TrackerConfig.ellipse(continuity_lock=False)`.

Replay's `--tracker-config` JSON can override `lock_center_gate`,
`lock_logsize_gate`, `lock_hold_seconds`, `maintenance_seconds` and
`retain_seconds`. Retention must exceed the lock hold. A smaller center gate
assumes slower image motion; a larger gate admits more nearby distractors. The
default is intended for slow submarine movement and can reject abrupt manual
camera pans.

Accepted observations identify `evidence` as `verified` or `local_maintenance`.
`boundary_stable` controls whether the shape-based tilt cue is available.
`maintained_local_image_track`, `tracked_boundary_unstable` and
`locked_target_not_observed` explain continuity-specific states. Current green
ellipses remain image measurements; pose and motor-control validity remain false.

## Validation and measured tradeoffs

All 109 relevant tests pass. New cases cover slow motion at 5/10/15 fps, stronger
distractors, nearby boundary alternatives, weak maintenance and its expiration,
gradual drift, short-miss reconfirmation, clipping, color failures, empty frames,
stale delivery and boundary-change orientation suppression.

The 445 observations from the [live review](live-hoop-review-2026-11-03.md) were
replayed with their frozen candidates and recorded per-candidate pixel metrics.
The baseline reproduces the original 218 accepted frames exactly. With the local
lock there are 203 accepted frames. Consecutive accepted ID switches decrease
from 26 to zero, and boundary-kind changes from 29 to 13. The number of
uninterrupted accepted runs of a single ID decreases from 40 to 19; their mean
length increases from 5.45 to 10.68 frames. This is less fragmentation, with a
cost in total output availability.

Tilt cues are available on 172 frames instead of 218 because changed boundaries
now suppress them temporarily. Among adjacent accepted pairs that both provide
a cue and are at most 0.25 seconds apart, there is one change of at least 15
degrees out of 147 pairs, compared with 22 out of 204 pairs. This diagnostic does
not measure true angular accuracy.

The frozen comparison does not rerun image detection or guided refits under the
new history. It is not a second live-camera test. Complete new image processing
was also run on the original saved photos/video at 10 fps. The video produced
47 accepted frames out of 148, versus 58 for the HSV baseline; accepted track-ID
changes in that evaluation decreased from 12 to three. One forbidden-interval
accept and an accepted floor-reflection negative crop remain. These results do
not show a general improvement in detection availability or accuracy.

Latest local artifacts are ignored by Git:

- `camera_results/hoop-continuity-live-replay/comparison.json`
- `camera_results/hoop-continuity-live-replay/continuity-observations.jsonl`
- `camera_results/hoop-continuity-lock-release/summary.json`
- `camera_results/hoop-continuity-lock-release/evaluation.json`
- `camera_results/hoop-continuity-lock-release/continuity-tracking.mp4`

The live stream used for the initial review is stopped. A new live session with
this branch is still needed to assess the complete updated path. Keep the
original capture separate from later validation data, and measure false locks
and lost-target behavior alongside continuity before deploying on the submarine.
