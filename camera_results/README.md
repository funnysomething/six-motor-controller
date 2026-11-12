# Camera results

This folder preserves the locally collected recordings, generated detector outputs, filter comparisons and experiment logs uploaded with the underwater testing branch.

## Start here

- `underwater_test1.mp4`: original underwater input. The container reports 2,366 frames, but both OpenCV and FFmpeg decoded 2,306 in our checks.
- `underwater_test1_testudo_yolo26n.mp4`: annotated output from the trained `testudo-3/weights/best.pt` model, confidence 0.25 and input size 640. All 2,306 readable frames are included. This is a visual review, not a labeled accuracy measurement.
- `underwater_test1_hoop_tracking.mp4`: current hoop-pipeline output. All 2,306 readable frames remained unknown; the matching observations and summary retain diagnostics.
- [Filter comparisons](underwater-filter-comparisons/README.md): three views per target, 24 transforms per view, overview sheets and native-resolution crops. The generation script and manifest preserve formulas, source frame indices and input hash.
- `underwater-filter-comparisons.zip`: downloadable bundle of the comparison images, script and metadata.
- `underwater-scene-review/`: sampled contact sheets and diagnostic images. This includes exploratory artifacts; for reproducible filter comparisons, prefer the comparison folder above.
- `ring tracking.mov`: additional local recording, preserved with its original filename. No new performance claims are attached to this upload.
- `hoop-*` and `live-review-*`: earlier hoop/Continuity Camera experiments. Retain their original summaries and configurations; they are not underwater accuracy benchmarks.

## Downloading media

MP4, MOV, AVI and ZIP files use Git LFS. Install Git LFS and run `git lfs install`, then `git lfs pull`. To fetch only the new underwater recording and outputs:

```sh
git lfs pull --include="camera_results/underwater_test1*.mp4"
```

A code-only clone/pull can use `GIT_LFS_SKIP_SMUDGE=1`; media then remains as pointer files until fetched. GitHub LFS storage and transfer quotas apply.

Compiled `.continuity-camera` helper caches, `.DS_Store` metadata and incomplete `.partial.mp4` conversions are excluded. New recordings need an explicit commit and push to be backed up here.
