# Experimental boundary tracking improvements

Branch: `codex/hoop-boundary-tracking-improvements`.

This branch includes the Continuity Camera preview-stability fix from
`codex/continuity-camera-preview-stability` and an initial implementation of the
proposed image-tracking improvements. These changes are ready for manual testing,
not a measured improvement in accuracy.

## Implemented

- Local edge search uses the previous boundary's white-material side, signed
  gradient and contrast profile. A nearby strong grayscale edge cannot qualify
  without white support on the expected side. Current HSV verification follows
  every refit.
- Refitting iteratively rejects large geometric residuals and requires retained
  edge points to cover distributed arcs. This reduces the influence of a few
  plants, attachments or reflection edges.
- Each remembered hoop gets a padded local crop processed at up to 800 pixels on
  its longest side. This preserves more boundary detail than shrinking the whole
  image to 640 pixels. Crop coordinates map back to the original frame.
- Sparse pyramidal optical flow on features near the previous ellipse can guide
  local translation. Forward/backward consistency and motion agreement reject
  unreliable points. Insufficient features fall back to the existing motion
  estimate. Flow never establishes target identity or replaces image checks.
- Proposal suppression preserves distinct nested/overlapping ellipse geometry.
  Only near-identical geometry of the same proposal kind is suppressed. This
  preserves more alternative boundaries for the existing local selector; it
  does not identify which physical rim each curve belongs to.
- A time-based ellipse filter smooths center, log axes and doubled-angle direction.
  Weaker edge coverage produces slower updates. The filter resets on a boundary
  change or long gap. Near circles retain the previous direction rather than
  treating unstable major-axis estimates as rotation.

Accepted output keeps the raw current `ellipse` and adds `filtered_ellipse` for
display. Orientation/calibration still uses the raw ellipse. The filtered drawing
only appears when a current measurement is accepted; blank frames do not produce
predicted detections. Existing loss, freshness, HSV and pose-validity gates remain
in effect.

## Manual test

Restart the running preview after switching branches:

```sh
.venv-hoop/bin/python scripts/vision/hoop_live.py --camera-name "iPhone" --fps 10
```

Hold a clear view steady, then move slowly through oblique views. Check whether
the green ellipse stays on the same visible rim and whether its displayed shape
jitters less. Watch processing time because sharper local crops add work.

Cover the hoop fully, move it completely out of view, and return to it. The
accepted drawing should disappear when current evidence is missing and recover
only with current support. A raw camera image may remain visible briefly under
the separate preview timeout.

Compare with `codex/continuity-camera-preview-stability` using the same camera
positions and lighting, restarting the process between branches.
`--no-continuity-lock` disables the local selection policy, not these new image
refitting algorithms.

Only a lightweight integration smoke check was run for these vision changes:
synthetic white-ring acquisition, refits, finite JSON output, filtered geometry
and no guided fit on a blank frame. Individual improvement tests and accuracy/
performance benchmarks were intentionally deferred to manual testing.

## Later work

Persistent physical front/back rim identities, uncertainty-calibrated ellipse
estimation, learned reflection-aware segmentation, temporal edge accumulation
and final-camera underwater calibration remain future work. No markers or other
environment modifications are required by the implemented changes.
