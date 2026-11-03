"""Ellipse proposals and support tests, without interpreting ellipse rotation as yaw."""

import math

import cv2
import numpy as np


def ellipse_points(ellipse, count=96, scale=1.):
    (cx, cy), (a, b), angle = ellipse
    t = np.arange(count) * (2 * math.pi / count)
    xy = np.column_stack((np.cos(t)*a*.5*scale, np.sin(t)*b*.5*scale))
    r = math.radians(angle)
    rotation = np.array(((math.cos(r), -math.sin(r)), (math.sin(r), math.cos(r))))
    return xy @ rotation.T + (cx, cy)


def fit_hypotheses(contour):
    """Fit full contours and overlapping arcs to tolerate attachments and joined blobs.

    Arc fitting is only a proposal mechanism. Full-perimeter image support is
    checked separately; a fit to five points alone never accepts a candidate.
    """
    points = contour.reshape(-1, 2)
    if len(points) < 20:
        return
    points = points[::max(1, len(points)//240)].astype(np.float32)
    subsets = [points]
    if len(points) >= 50:
        for fraction in (.15, .25, .4, .6, .8):
            size = int(len(points)*fraction)
            for start in np.linspace(0, len(points), 8, endpoint=False, dtype=int):
                subsets.append(points[(np.arange(size)+start) % len(points)])
    for subset in subsets:
        try:
            ellipse = cv2.fitEllipseDirect(subset.reshape(-1, 1, 2))
            if all(math.isfinite(float(x)) for x in (*ellipse[0], *ellipse[1], ellipse[2])):
                yield ellipse
        except cv2.error:
            continue


def sample(array, points):
    xy = np.rint(points).astype(int)
    h, w = array.shape
    valid = (xy[:, 0] >= 0) & (xy[:, 0] < w) & (xy[:, 1] >= 0) & (xy[:, 1] < h)
    values = np.zeros(len(points), dtype=float)
    values[valid] = array[xy[valid, 1], xy[valid, 0]]
    return values, valid


def bbox(ellipse):
    points = ellipse_points(ellipse, count=180)
    return (*points.min(axis=0), *points.max(axis=0))


def overlap(a, b):
    x = max(0., min(a[2], b[2])-max(a[0], b[0]))
    y = max(0., min(a[3], b[3])-max(a[1], b[1]))
    intersection = x*y
    return intersection/max(1e-9, (a[2]-a[0])*(a[3]-a[1]) +
                            (b[2]-b[0])*(b[3]-b[1])-intersection)
