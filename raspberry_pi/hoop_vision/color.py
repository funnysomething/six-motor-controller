"""Shared HSV white-material tests, evaluated on current image pixels."""

import cv2
import numpy as np

from .geometry import sample


WHITE_SATURATION_MAX = 60
WHITE_VALUE_MIN = 160
WHITE_MIN_SUPPORT = .65
WHITE_MIN_SECTORS = 6
WHITE_MIN_BOUNDED_SUPPORT = .15


def validate_hsv(saturation_max, value_min):
    if any(type(v) is not int or not 0 <= v <= 255 for v in (saturation_max, value_min)):
        raise ValueError("HSV saturation/value thresholds must be integers in 0..255")


def white_mask(hsv, saturation_max=WHITE_SATURATION_MAX, value_min=WHITE_VALUE_MIN):
    # OpenCV uint8 HSV: H=0..179, S/V=0..255. Near white, hue is undefined.
    return cv2.inRange(hsv, (0, 0, value_min), (179, saturation_max, 255)) / 255.


def white_band_evidence(mask, points, normals=None):
    """Require a coherent white strip on one side of the fitted perimeter.

    A single highlight is insufficient: at least two of three nearby samples
    must be white at each supported angle. Also measure where the strip ends
    farther along the same normal, to distinguish material from a white wall.
    Out-of-image samples never support a fit. Choose a consistent side, not
    whichever side happens to be white at each angle.
    """
    if normals is None:
        tangent = np.roll(points, -1, axis=0) - np.roll(points, 1, axis=0)
        normals = np.column_stack((-tangent[:, 1], tangent[:, 0]))
        normals /= np.maximum(1e-6, np.linalg.norm(normals, axis=1))[:, None]
    sides = []
    far = max(16., .35*float(np.ptp(points, axis=0).min()))
    for direction in (1, -1):
        votes = np.zeros(len(points), dtype=int)
        for offset in (2, 4, 6):
            values, valid = sample(mask, points + direction*offset*normals)
            votes += (values > .5) & valid
        near_white = votes >= 2
        bounded = np.ones(len(points), dtype=bool)
        for offset in (far, far*1.25):
            values, valid = sample(mask, points + direction*offset*normals)
            bounded &= (values < .5) & valid
        sectors = sum(float(part.mean()) >= .5 for part in np.array_split(near_white, 8))
        sides.append(dict(support=float(near_white.mean()), bounded_support=float((near_white & bounded).mean()),
                          sectors=sectors, side=direction))
    return max(sides, key=lambda side:(side["bounded_support"]>=WHITE_MIN_BOUNDED_SUPPORT,
                                     side["support"], side["sectors"]))
