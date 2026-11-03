# Live iPhone hoop review, November 3, 2026

The live review used the current HSV/ellipse pipeline with Chris' iPhone 17
Continuity Camera. Capture was stopped after identifying useful failure cases.
The capture helper and review process exited. No motors were connected.

Local diagnostic samples and logs are under
`camera_results/live-review-20261103-144326/`. They are ignored by Git. The session
contains 445 processed observations spanning 46.64 seconds of source timestamps,
with 84 sampled raw/annotated JPEG pairs. It is not a complete video recording.
Incoming images were 1920×1080; processing used the 640-pixel maximum dimension.

## Observations

- 218 observations were accepted and 227 were unknown. This measures output
  availability, not accuracy. There is no measured camera/hoop pose ground truth.
- There were 29 boundary-kind changes between consecutive accepted observations.
  Selection alternated between `material_outline` and `apparent_opening`.
- There were 22 displayed unsigned-tilt changes of at least 15 degrees between
  adjacent accepted observations no more than 0.25 seconds apart. Nineteen also
  changed track ID; three retained the same ID. These are instability indicators,
  not measured angular errors, since the camera was moving manually.
- Samples 19, 24 and 29 show fits selecting different extents of the same hoop.
  The tracker can preserve object history while still switching the selected
  boundary or track. Bounding-box association does not constrain the entire
  ellipse geometry.
- At observations 160 and 200, candidate white support was approximately
  0.67–0.89, but oriented edges and band-side checks failed. Color is therefore
  not the only cause of dropouts. Samples 161 and 199 include an orange foreground
  object, oblique viewing and limited visible rim evidence.
- Clipping contributes additional losses. A visible fragment is insufficient to
  claim a reliable full ellipse or orientation. Sample 311 had a passing image
  candidate but was still accumulating temporal confirmation.
- Median processing time was 32.3 ms, p95 46.5 ms. Median source interval was
  100.01 ms. Host receipt-to-publication p95 was 49.3 ms. Wireless capture-to-host
  latency remains unmeasured. These results do not show a CPU bottleneck.

## Proposed changes

1. Prefer the established hoop's local image neighborhood, with a slow-motion
   prior. Once acquired, preserve the selected identity when a compatible current
   candidate exists instead of choosing whichever confirmed track has the
   strongest edge coverage that frame. Make changes to a distant target explicit
   reacquisitions.
2. Separate acquisition requirements from maintenance requirements. Allow modest
   geometry deterioration near a confirmed hoop while retaining current white
   pixels, distributed edges, freshness and clipping/occlusion rejection. Do not
   turn a remembered prediction into a measured detection.
3. Keep boundary-specific geometry within an object track. Prevent outline/opening
   alternation from appearing as submarine-relative rotation. Track center, axes
   and ellipse orientation with uncertainty before using a circle-pose solver.
4. Improve contour extraction and fit residual checks on actual white-band
   boundaries. Fit supported arcs with outlier rejection, and test band sampling
   widths that reflect local band thickness. Preserve the HSV color policy.
5. Retain an internal track through short misses, partial obstruction and clipping,
   but withhold public geometry until enough current evidence returns. Record
   exposure/blur and host inter-arrival times to separate image failures from
   transport stalls. Optical-flow-assisted local search is a later option.

Priorities 1 and 2 directly address the requested continuity behavior. A
boundary-specific geometric model remains separate work. OpenCV provides
[contour and ellipse fitting](https://docs.opencv.org/4.x/d3/dc0/group__imgproc__shape.html)
and [pyramidal optical flow](https://docs.opencv.org/4.x/dc/d6b/group__video__track.html)
for those later experiments.

The complete candidate log can compare tracker selection changes at the original
cadence. The sparse JPEG sample can test per-image fits, but cannot reproduce a
complete 10 fps temporal experiment. Changes to image extraction require another
timestamped full-frame capture for temporal validation.
