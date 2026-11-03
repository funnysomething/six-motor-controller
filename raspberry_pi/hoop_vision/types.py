"""JSON-compatible observations; image geometry is never a metric pose."""

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Candidate:
    center_px: tuple
    axes_px: tuple
    image_rotation_deg: float
    bbox_xyxy: tuple
    score: float
    edge_coverage: float
    material_support: float
    interior_white_fraction: float
    clipped: bool
    kind: str
    verification: Optional[dict] = None
    source: str = "detector"


@dataclass
class Observation:
    frame_size: tuple
    candidates: list = field(default_factory=list)
    schema_version: int = 1
    status: str = "no_candidate"
    detection_valid: bool = False
    target_identity_verified: bool = False
    pose_valid: bool = False
    yaw_deg: Optional[float] = None
    pitch_deg: Optional[float] = None
    center_m: Optional[tuple] = None
    passage_axis: Optional[tuple] = None
    reason: str = "No supported white-band ellipse proposal"
    decision: str = "unknown"
    accepted_target: Optional[dict] = None
    tracks: list = field(default_factory=list)
    tracking_reason: str = "tracking_disabled"
    image_orientation: Optional[dict] = None

    def to_dict(self):
        return asdict(self)
