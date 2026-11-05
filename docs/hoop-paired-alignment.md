# Paired boundary and view cues

The experimental boundary-tracking branch now pairs current nested ellipse
proposals using containment, compatible directions, opposing white-material
sides and white pixels between the curves. Centers may differ under perspective.
The pair describes image inner/outer boundaries; it does not prove which physical
front/back rim is visible or distinguish a reflection from the actual hoop.

The overlay draws the inner boundary green and outer boundary blue/orange, with
ellipse axes, a green target-center cross and a white image-center cross.
Center offsets are image pixels: positive dx means right, positive dy means down.
These cues use image center, not a measured camera principal point or submarine
centerline. No motor commands or passage-clearance estimates are produced.

`Observation.alignment` exposes pair geometry, white support, axis ratio,
centering offsets, axes and view classification:

- `near_front_on`: both paired ellipses have minor/major ratio at least 0.98,
  a stable tracked boundary, and at least 0.1 seconds of consecutive supporting
  observations. Exit hysteresis uses 0.96. Display axes become horizontal and
  vertical image axes here, because a near-circle's major direction is unstable.
- `oblique`: a current pair is available without the near-front-on conditions.
- `mostly_side_on`: a currently accepted ellipse has ratio below 0.30, with 0.40
  exit hysteresis. The opening is strongly foreshortened.
- `unpaired`: an accepted current ellipse is available but pairing is unresolved.
- `side_view_candidate`: full ellipse tracking failed, but a narrow current white
  component overlaps a recently confirmed hoop. This is an advisory orange box,
  not an accepted detection or orientation. It expires with the track's bounded
  search horizon and cannot initialize a hoop identity from a white pole.
- `unknown`: no supported geometry is available. Blank, clipped or stale views
  do not produce an alignment indication.

Exactly edge-on views can hide the aperture entirely. The system indicates the
side-view condition when supported rather than fabricating two ellipses. It
cannot infer which direction to turn from an unresolved side view alone.

`near_front_on` means nearly circular image geometry. It is not proof that the
submarine is perpendicular to the hoop plane or positioned to pass through it.
`alignment_valid` and `passage_clearance_valid` remain false. Reliable steering
needs the Raspberry Pi AI Camera's in-water housing calibration, camera-to-body
alignment, measured hoop/vehicle clearance and resolved physical-rim identity.

Restart the usual live launcher to load the changes. It also annotates tracked
saved-media replay. A lightweight synthetic frontal-ring smoke check exercised
pairing, temporal near-front-on indication, overlay rendering and JSON output.
Manual evaluation on real oblique, side, obscured and frontal views is still
required; no per-feature accuracy benchmark was run.
