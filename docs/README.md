# Hoop vision documentation

- [Live iPhone review and proposed improvements](live-hoop-review-2026-11-03.md)
  records observed boundary switching, dropouts and next implementation steps.
- [HSV white-material gating and measured tradeoffs](hoop-hsv-color-gating.md)
  records the current color checks and the latest media rerun.
- [Live iPhone / Continuity Camera setup](iphone-continuity-camera.md) explains
  wireless camera selection, the local preview and freshness limits.
- [Production development reference and improvement roadmap](underwater-vision-production-reference.md)
  is the starting point for the final camera, housing and submarine inside water.
- [Latest tracker results and limitations](hoop-ellipse-tracking-results.md)
  records the ellipse-first prototype's measured behavior and known errors.
- [Setup, replay, evaluation and calibration usage](../raspberry_pi/hoop_vision/README.md)
  explains how to run the current implementation.
- [Retained evaluation](hoop-vision/ellipse-tracking/evaluation.json),
  [10 fps summary](hoop-vision/ellipse-tracking/10fps-summary.json) and
  [provenance manifest](hoop-vision/production-reference-manifest.json)
  preserve the latest evidence and file identities.

Original inputs remain in `ENEE408V Hoop/`. The latest annotated video is
[ellipse-tracking.mp4](hoop-vision/ellipse-tracking/ellipse-tracking.mp4).
Generated media and full logs are ignored by git; archive them explicitly when
transferring to another checkout. Only the final 10 fps, 5 fps, 15 fps and
source-rate runs are retained locally. Superseded experiments and reports have
been removed.
