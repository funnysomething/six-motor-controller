"""Classical white-band proposal detector for offline development footage.

    Proposals may include reflections. No target identity, true rim, range,
    bearing angle, or signed 3D orientation is inferred by this baseline.
"""

from dataclasses import dataclass
import math

import cv2
import numpy as np

from .geometry import bbox, ellipse_points, fit_hypotheses, overlap, sample
from .types import Candidate, Observation


@dataclass(frozen=True)
class DetectorConfig:
    max_dimension: int = 640
    saturation_max: int = 100
    value_min: int = 95
    min_area_fraction: float = .035
    min_edge_coverage: float = .62
    max_candidates: int = 6
    rim_hypotheses: bool = False

    def __post_init__(self):
        if self.max_dimension < 128 or self.max_candidates < 1:
            raise ValueError("max_dimension >= 128 and max_candidates >= 1 required")
        if not 0 <= self.saturation_max <= 255 or not 0 <= self.value_min <= 255:
            raise ValueError("HSV saturation/value thresholds must be in 0..255")
        if not 0 < self.min_area_fraction < 1 or not 0 < self.min_edge_coverage <= 1:
            raise ValueError("area and coverage fractions must be in (0,1]")


def detect(image, config=None):
    config = config or DetectorConfig()
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3 or not image.size:
        raise ValueError("Expected a nonempty uint8 BGR image")
    height, width = image.shape[:2]
    scale = min(1., config.max_dimension/max(height, width))
    frame = cv2.resize(image, (max(1, round(width*scale)), max(1, round(height*scale))))
    h, w = frame.shape[:2]
    # Exact per-axis mapping matters when rounding the resized dimensions.
    sx, sy = width/w, height/h
    hsv = cv2.cvtColor(cv2.GaussianBlur(frame, (3, 3), 0), cv2.COLOR_BGR2HSV)
    masks = []
    contours = []
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    for value in sorted(set((config.value_min, max(config.value_min, 140), max(config.value_min, 180)))):
        mask = cv2.inRange(hsv, (0, 0, value), (179, config.saturation_max, 255))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        masks.append(mask)
        found, hierarchy = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_NONE)
        for i, contour in enumerate(found):
            if len(contour) >= 30 and cv2.contourArea(cv2.convexHull(contour)) >= w*h*config.min_area_fraction:
                contours.append((contour, hierarchy[0, i, 3] >= 0))
    edges = np.zeros((h, w), np.uint8)
    for mask in masks:
        edges |= cv2.morphologyEx(mask, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    gray_edges = cv2.Canny(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), 30, 80)
    edge_contours, _ = cv2.findContours(gray_edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    for contour in edge_contours:
        if len(contour) >= 30 and cv2.contourArea(cv2.convexHull(contour)) >= w*h*config.min_area_fraction:
            contours.append((contour, False))
    edges |= gray_edges
    distances = cv2.distanceTransform(255-edges, cv2.DIST_L2, 3)
    white = masks[0]/255.
    candidates = []
    # Bound the work even on textured images. No unbounded random sampling.
    contours.sort(key=lambda item: cv2.contourArea(cv2.convexHull(item[0])), reverse=True)
    for contour, is_hole in contours[:64 if config.rim_hypotheses else 24]:
        for ellipse in fit_hypotheses(contour):
            (cx, cy), (a, b), angle = ellipse
            if min(a, b) < 20 or min(a, b)/max(a, b) < .18:
                continue
            area = math.pi*a*b/4
            if not w*h*config.min_area_fraction <= area <= w*h*.8:
                continue
            box = bbox(ellipse)
            if not 0 <= cx < w or not 0 <= cy < h or a > 1.6*max(w,h) or b > 1.6*max(w,h):
                continue
            points = ellipse_points(ellipse)
            values, valid = sample(distances, points)
            # Invalid samples count against full-perimeter coverage.
            coverage = float(np.mean((values <= 3.) & valid))
            if coverage < config.min_edge_coverage:
                continue
            inner, _ = sample(white, ellipse_points(ellipse, scale=.9))
            outer, _ = sample(white, ellipse_points(ellipse, scale=1.1))
            material = float(np.mean(np.maximum(inner, outer)))
            interior = np.concatenate([sample(white, ellipse_points(ellipse, scale=s))[0] for s in (.25,.5,.7)])
            interior_white = float(interior.mean())
            neutral = interior_white > (.8 if is_hole else .98)
            if material < .45 or (neutral and not config.rim_hypotheses):
                continue
            clipped = bool(box[0] < 3 or box[1] < 3 or box[2] > w-4 or box[3] > h-4)
            score = .4*coverage + .2*material + .4*min(1., area/(w*h*.20)) - .1*clipped
            # Nonuniform rounding of the resize needs a refit, not a naive
            # multiplication of rotated ellipse axis lengths.
            mapped = (ellipse_points(ellipse)*[sx, sy]).astype(np.float32)
            restored = cv2.fitEllipse(mapped.reshape(-1, 1, 2))
            candidates.append(Candidate(
                tuple(float(x) for x in restored[0]), tuple(float(x) for x in restored[1]), float(restored[2]),
                tuple(float(v) for v in bbox(restored)),
                float(score), coverage, material, interior_white, clipped,
                "rim_arc" if neutral else "apparent_opening" if is_hole else "material_outline"))
    candidates.sort(key=lambda c: c.score, reverse=True)
    kept = []
    for candidate in candidates:
        if all(overlap(candidate.bbox_xyxy, other.bbox_xyxy) < .65 for other in kept):
            kept.append(candidate)
        if len(kept) >= config.max_candidates:
            break
    observation = Observation((width, height), kept)
    if kept:
        observation.status = "uncalibrated_candidates"
        observation.reason = "Image proposals only; rim identity, real/reflected target and 3D pose unverified"
    return observation
