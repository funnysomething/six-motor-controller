# HSV white-material gating

The live and saved-media pipelines now require current white-material evidence
before an ellipse can become an accepted measurement. This is active by default
on `codex/iphone-live-hoop-tracking` for both ellipse and strict profiles.

The hoop is assumed to be white and reasonably well lit. The initial defaults
use OpenCV's 8-bit HSV scale:

- H: 0 through 179, unrestricted. Hue is unstable near white.
- S: 0 through 60, to reject saturated colors.
- V: 160 through 255, to reject dark surfaces.

These are prototype thresholds, not a measured color calibration. Slightly warm
or cool whites can pass the saturation limit; strongly tinted white surfaces
can fail. The implementation uses BGR-to-HSV conversion and `inRange`, following
[OpenCV's HSV thresholding documentation](https://docs.opencv.org/4.x/da/d97/tutorial_threshold_inRange.html).

## How it affects ellipse fitting

The proposal detector constructs its masks from the configured HSV range.
Grayscale contours still help find boundaries, but a grayscale fit must pass
the same white-material check before it can occupy a proposal slot. Color
support also contributes to candidate ranking.

Verification checks the original resized image mask, without filling gaps by
morphology. At each of 96 angles around a fit, two of three samples at offsets
2, 4 and 6 pixels must be white. One consistent side of the fitted curve must
support at least 65% of its perimeter and cover at least six of eight sectors.
Those are processing-image pixels, with a default maximum image dimension of
640. Pixels outside the HSV range and samples outside the image supply no
support. A few white highlights on a colored object cannot satisfy this rule.

A uniformly white background must not be mistaken for the hoop's material.
At least 15% of the perimeter must also show the white strip ending farther
along that same normal. The farther checks are at 35% and 43.75% of the ellipse's
smaller image extent, with a minimum first offset of 16 pixels. This is a
heuristic guard against unbounded white fields. It can reject real hoops when
the surrounding background also looks white.

The final HSV gate applies after the profile-specific checks. Temporal history,
high geometry scores and paired boundaries cannot override a color failure.
Tracked refits must pass again on current pixels. The tracker withholds output
as soon as no current candidate passes; a prediction alone does not qualify.

Verification diagnostics include `white_support`, `white_bounded_support`,
`white_sectors`, `white_side`, `white_hsv`, and `white_color_passed`. Color-specific
reasons include `outside_white_hsv_range`, `insufficient_white_arc_distribution`
and `unbounded_white_background`.

## Running and tuning

Existing live commands automatically use this gate:

```sh
.venv-hoop/bin/python scripts/vision/hoop_live.py --camera-name "iPhone" --fps 10
```

Both live capture and media replay accept `--saturation-max`, `--value-min` and
`--min-white-support`. For example, a stricter color range is:

```sh
.venv-hoop/bin/python scripts/vision/hoop_live.py --camera-name "iPhone" \
  --saturation-max 45 --value-min 180 --min-white-support 0.75
```

Replay's `--verification-config` JSON can also set these fields and
`min_white_sectors`. JSON settings override the CLI white defaults; the resolved
settings apply to both proposal generation and verification. Library callers
of `run()` must also use matching detector/verification white settings. Frozen
`--candidate-json` replay uses the verifications recorded in that log and cannot
apply a new pixel-based color gate; rerun the original media to test this policy.

Keep camera exposure, white balance, zoom and video effects stable while testing.
Tune from positive hoop pixels and target-free backgrounds recorded with the
actual camera. Save separate validation clips before changing thresholds.
Lowering the brightness floor or raising the saturation ceiling admits more
distractors. The pipeline does not loosen its range automatically to preserve a
track.

## Development results

The full supplied set was rerun: 13 independent photos and 148 sampled video
frames at 10 fps. Each photo remains unknown in temporal mode because it lacks
preceding observations. The latest generated files are local and ignored by Git:

- `camera_results/hoop-hsv-white-final/hsv-white-tracking.mp4`
- `camera_results/hoop-hsv-white-final/observations.jsonl`
- `camera_results/hoop-hsv-white-final/summary.json`
- `camera_results/hoop-hsv-white-final/evaluation.json`

The new gate accepts **58 of 148 video frames**, compared with 77 in the retained
pre-color revision. First acceptance moves from 0.1 seconds to about 4.10 seconds.
The first few seconds therefore still need better fitting/color support, even
though the physical hoop is visible. This is a cost of the stricter policy, not
an increase in detection availability.

On the existing development annotations, 15 of 20 accepted labeled views overlap
the appropriate coarse hoop region by at least 0.3 IoU. The retained evaluation
had 5 of 14 such matches. Forbidden interval accepts decrease from four to one.
These are development diagnostics on reused, coarse annotations, not precision
or a held-out success rate. The remaining forbidden accept and one accepted
target-free floor-reflection crop show that color is insufficient for identity.

Median detector time was 17.3 ms and median verification time was 1.4 ms on the
development Mac. Detector p95 was 38.1 ms and verification p95 was 2.7 ms. Those
timings exclude decoding, transport and rendering; they do not establish live
camera latency or a guaranteed frame rate.

All 88 relevant tests pass, including new checks for exact HSV bounds, white
balance variation, saturated colors, dark gray, white-background distractors,
sparse highlights, paired-boundary/refit bypass attempts and immediate loss of
accepted output when the current ring becomes colored.

## Remaining limits

White reflections and other white ring-shaped objects can still pass. White
walls reduce useful color contrast. Shadows, exposure changes and underwater
color casts can make a physically white hoop fall outside this fixed range.
Partial visibility and highly oblique views can fail the perimeter checks.
Passing HSV and ellipse checks does not resolve the physical rim or signed pose.

Use the [underwater production reference](underwater-vision-production-reference.md)
for camera calibration, reflection rejection and final in-water validation.
