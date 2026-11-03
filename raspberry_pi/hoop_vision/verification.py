"""Independent boundary checks. Passing is image evidence, not physical identity."""

from dataclasses import dataclass
import math

import cv2
import numpy as np

from .geometry import ellipse_points, sample


@dataclass(frozen=True)
class VerificationConfig:
    profile: str = "strict"
    max_dimension: int = 640
    min_coverage: float = .72
    min_sectors: int = 6
    min_material: float = .65
    min_polarity: float = .65
    strong_coverage: float = .82

    def __post_init__(self):
        if self.profile not in ("strict","ellipse"):
            raise ValueError("Unknown verification profile")
        if type(self.max_dimension) is not int or self.max_dimension < 128:
            raise ValueError("Verification dimension must be an integer >= 128")
        if type(self.min_sectors) is not int or not 1 <= self.min_sectors <= 8:
            raise ValueError("min_sectors must be an integer in 1..8")
        if not all(math.isfinite(v) and 0 < v <= 1 for v in
                   (self.min_coverage, self.min_material, self.min_polarity, self.strong_coverage)):
            raise ValueError("Verification fractions must be finite in (0,1]")
        if self.strong_coverage < self.min_coverage:
            raise ValueError("Acquisition coverage cannot be weaker than maintenance")


def verify(image, observation, config=None):
    """Attach diagnostics without removing raw proposals or changing pose flags."""
    config = config or VerificationConfig()
    h, w = image.shape[:2]
    if observation.frame_size != (w, h):
        raise ValueError("Observation and image coordinate systems differ")
    scale = min(1., config.max_dimension/max(h, w))
    frame = cv2.resize(image, (round(w*scale), round(h*scale)))
    sy, sx = frame.shape[0]/h, frame.shape[1]/w
    gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (3, 3), 0)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    whites = [((hsv[:, :, 1] <= 100) & (hsv[:, :, 2] >= value)).astype(float)
              for value in (95, 140)]
    for candidate in observation.candidates:
        ellipse = (candidate.center_px, candidate.axes_px, candidate.image_rotation_deg)
        numbers = (*candidate.center_px, *candidate.axes_px, candidate.image_rotation_deg,
                   *candidate.bbox_xyxy, candidate.score)
        if (not all(math.isfinite(v) for v in numbers) or min(candidate.axes_px) <= 0 or
                max(candidate.axes_px) > 4*max(w,h)):
            candidate.verification = dict(passed=False, strong=False, reasons=["invalid_geometry"])
            continue
        points = ellipse_points(ellipse)*[sx, sy]
        tangent = np.roll(points, -1, axis=0)-np.roll(points, 1, axis=0)
        normals = np.column_stack((-tangent[:, 1], tangent[:, 0]))
        normals /= np.maximum(1e-6, np.linalg.norm(normals, axis=1))[:, None]
        support = np.zeros(len(points), bool)
        for offset in (-3, -2, -1, 0, 1, 2, 3):
            probe = points+offset*normals
            x, valid = sample(gx, probe)
            y, _ = sample(gy, probe)
            magnitude = np.hypot(x, y)
            alignment = np.abs(x*normals[:, 0]+y*normals[:, 1])/np.maximum(magnitude, 1e-6)
            support |= valid & (magnitude >= 40) & (alignment >= .8)
        coverage = float(support.mean())
        secondary = np.zeros(len(points),bool)
        for radial in (.4,.55,.7,.85):
            probe=ellipse_points(ellipse,scale=radial)*[sx,sy]
            x,valid=sample(gx,probe);y,_=sample(gy,probe)
            secondary |= valid & (np.abs(x*normals[:,0]+y*normals[:,1])>=40)
        secondary_coverage=float(secondary.mean())
        sectors = sum(float(s.mean()) >= .5 for s in np.array_split(support, 8))
        material, polarity = [], []
        for white in whites:
            left, vl = sample(white, points+6*normals)
            right, vr = sample(white, points-6*normals)
            material.append(float(np.mean(np.maximum(left, right)*vl*vr)))
            # White must lie predominantly on one consistent side of the curve.
            polarity.append(float(max(np.mean((left-right) > .5), np.mean((right-left) > .5))))
        reasons = []
        interior = float(sample(whites[0], ellipse_points(ellipse, scale=.5)*[sx,sy])[0].mean())
        if interior > .8:
            reasons.append("no_visible_aperture_evidence")
        if (candidate.clipped or points[:,0].min() < 2 or points[:,1].min() < 2 or
                points[:,0].max() > frame.shape[1]-3 or points[:,1].max() > frame.shape[0]-3):
            reasons.append("clipped")
        if coverage < config.min_coverage:
            reasons.append("insufficient_oriented_edges")
        if sectors < config.min_sectors:
            reasons.append("insufficient_arc_distribution")
        if min(material) < config.min_material:
            reasons.append("unstable_material_support")
        if max(polarity) < config.min_polarity:
            reasons.append("inconsistent_band_side")
        if config.profile == "ellipse":
            # Neutral background behind a hoop is not a filled disk. Keep these
            # hypotheses for paired-boundary validation rather than using a
            # brightness-only aperture veto.
            reasons = []
            if coverage < .48 or sectors < 4:
                reasons.append("insufficient_rim_edges")
            if min(material) < .45:
                reasons.append("insufficient_band_material")
            if candidate.kind!='rim_arc' and max(polarity)<.35:
                reasons.append("inconsistent_band_side")
            if candidate.clipped:
                reasons.append("clipped")
            if candidate.kind == "rim_arc" and not (
                    coverage>=.45 and sectors>=4 and min(material)>=.6 and candidate.score>=.8):
                reasons.append("needs_paired_boundary")
            elif candidate.kind=="rim_arc":
                reasons=[r for r in reasons if r!="insufficient_rim_edges"]
                if secondary_coverage<.08 and candidate.source!='confirmed_temporal_refit':
                    reasons.append("no_secondary_boundary")
        candidate.verification = dict(
            passed=not reasons, strong=not reasons and coverage >= config.strong_coverage,
            reasons=reasons, oriented_coverage=coverage, sectors=sectors,
            material_support=min(material), polarity=max(polarity),
            interior_boundary_coverage=secondary_coverage,
            quality_score=.4*candidate.score+.4*coverage+.2*sectors/8)
        if config.profile == "ellipse" and not reasons:
            candidate.verification["strong"] = coverage >= .55 or (candidate.kind=="rim_arc" and candidate.score>=.8)
            if candidate.kind=='rim_arc' and secondary_coverage<.08:
                candidate.verification['strong']=False
    if config.profile == "ellipse":
        # Independent noncoincident curves are supporting evidence for a band.
        # They are not proof of a physical target or a known front rim.
        for c in observation.candidates:
            if c.kind != "rim_arc" or c.verification["reasons"] != ["needs_paired_boundary"]:
                continue
            for other in observation.candidates:
                if other is c:
                    continue
                a,b = c.axes_px, other.axes_px
                if min(b)<=0:
                    continue
                separation = np.linalg.norm(np.array(c.center_px)-other.center_px)
                similar = .55 < min(a)/min(b) < 1.8 and .55 < max(a)/max(b) < 1.8
                distinct = separation > .04*min(a) or abs(max(a)-max(b)) > .08*max(a)
                near = separation < .65*max(a)
                ov = other.verification or {}
                if similar and distinct and near and ov.get("oriented_coverage",0) >= .6:
                    c.verification.update(passed=True,strong=c.verification["oriented_coverage"]>=.6,
                                          reasons=[],paired_boundary=True)
                    break
    return observation
