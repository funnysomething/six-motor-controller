"""Offline hoop proposals. No camera, GPIO, motor, or pose-control dependencies."""

from .detector import DetectorConfig, detect

__all__ = ["DetectorConfig", "detect"]
