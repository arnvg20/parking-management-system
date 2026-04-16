from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence


class SpaceStatus(str, Enum):
    EMPTY = "EMPTY"
    OCCUPIED = "OCCUPIED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class Point2D:
    x: float
    y: float

    def to_list(self) -> list[float]:
        return [float(self.x), float(self.y)]


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float

    def to_dict(self) -> dict[str, float]:
        return {
            "lat": float(self.lat),
            "lon": float(self.lon),
        }


@dataclass(frozen=True)
class BBox:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return max(0.0, float(self.x2) - float(self.x1))

    @property
    def height(self) -> float:
        return max(0.0, float(self.y2) - float(self.y1))

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Point2D:
        return Point2D(
            x=float(self.x1 + self.x2) / 2.0,
            y=float(self.y1 + self.y2) / 2.0,
        )

    @property
    def bottom_center(self) -> Point2D:
        return Point2D(
            x=float(self.x1 + self.x2) / 2.0,
            y=float(self.y2),
        )

    def lower_region(self, lower_fraction: float = 0.40) -> "BBox":
        lower_fraction = min(max(lower_fraction, 0.0), 1.0)
        top_y = float(self.y2) - (self.height * lower_fraction)
        return BBox(
            x1=float(self.x1),
            y1=top_y,
            x2=float(self.x2),
            y2=float(self.y2),
        )

    def expand(self, margin_px: float) -> "BBox":
        return BBox(
            x1=float(self.x1) - margin_px,
            y1=float(self.y1) - margin_px,
            x2=float(self.x2) + margin_px,
            y2=float(self.y2) + margin_px,
        )

    def contains_point(self, point: Point2D) -> bool:
        return self.x1 <= point.x <= self.x2 and self.y1 <= point.y <= self.y2

    def to_list(self) -> list[float]:
        return [float(self.x1), float(self.y1), float(self.x2), float(self.y2)]


@dataclass(frozen=True)
class ParkingSpace:
    space_id: str
    polygon: tuple[Point2D, ...]
    location: Location | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VehicleDetection:
    detection_id: str
    bbox: BBox
    detection_confidence: float
    track_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlateDetection:
    detection_id: str
    text: str | None
    bbox: BBox
    ocr_confidence: float
    detection_confidence: float
    track_id: str | None = None
    candidate_reads: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FrameContext:
    frame_id: str
    timestamp: str
    gps_location: Location | None = None
    debug_mode: bool = False


@dataclass
class VehicleSpaceEvidence:
    parking_space_id: str
    vehicle_id: str
    vehicle_bbox_pixels: list[float]
    vehicle_center_pixel: list[float]
    vehicle_bottom_center_pixel: list[float]
    occupancy_ratio: float
    lower_overlap_ratio: float
    bottom_center_inside_space: bool
    distance_to_space_center_px: float
    detection_confidence: float
    depth_score: float
    score: float
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "parking_space_id": self.parking_space_id,
            "vehicle_id": self.vehicle_id,
            "bbox_pixels": self.vehicle_bbox_pixels,
            "center_pixel": self.vehicle_center_pixel,
            "bottom_center_pixel": self.vehicle_bottom_center_pixel,
            "occupancy_ratio": self.occupancy_ratio,
            "lower_overlap_ratio": self.lower_overlap_ratio,
            "bottom_center_inside_space": self.bottom_center_inside_space,
            "distance_to_space_center_px": self.distance_to_space_center_px,
            "detection_confidence": self.detection_confidence,
            "depth_score": self.depth_score,
            "score": self.score,
            "rejection_reason": self.rejection_reason,
        }


@dataclass
class PlateVehicleEvidence:
    vehicle_id: str
    plate_id: str
    plate_text: str | None
    plate_bbox_pixels: list[float]
    plate_center_pixel: list[float]
    plate_inside_vehicle: bool
    plate_to_vehicle_distance_px: float
    plate_to_vehicle_bottom_distance_px: float
    neighbor_margin: float
    ocr_confidence: float
    detection_confidence: float
    geometric_score: float
    score: float
    rejection_reason: str | None = None
    candidate_reads: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "plate_id": self.plate_id,
            "text": self.plate_text,
            "bbox_pixels": self.plate_bbox_pixels,
            "center_pixel": self.plate_center_pixel,
            "plate_inside_vehicle": self.plate_inside_vehicle,
            "plate_to_vehicle_distance_px": self.plate_to_vehicle_distance_px,
            "plate_to_vehicle_bottom_distance_px": self.plate_to_vehicle_bottom_distance_px,
            "neighbor_margin": self.neighbor_margin,
            "ocr_confidence": self.ocr_confidence,
            "detection_confidence": self.detection_confidence,
            "geometric_score": self.geometric_score,
            "score": self.score,
            "rejection_reason": self.rejection_reason,
            "candidate_reads": list(self.candidate_reads),
        }


@dataclass
class InternalDecision:
    parking_space_id: str
    status: SpaceStatus
    association_confidence: float
    frame_id: str
    timestamp: str
    gps_location: Location | None
    vehicle: VehicleSpaceEvidence | None = None
    plate: PlateVehicleEvidence | None = None
    rejection_reason: str | None = None
    debug: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parking_space_id": self.parking_space_id,
            "status": self.status.value,
            "association_confidence": self.association_confidence,
            "frame_id": self.frame_id,
            "timestamp": self.timestamp,
            "vehicle": self.vehicle.to_dict() if self.vehicle else None,
            "plate": self.plate.to_dict() if self.plate else None,
            "gps": self.gps_location.to_dict() if self.gps_location else None,
            "debug": dict(self.debug),
            "rejection_reason": self.rejection_reason,
        }


@dataclass
class TemporalDecision:
    parking_space_id: str
    status: SpaceStatus
    stable_plate_read: str | None
    confidence_level: float
    confirmed_frames: int
    should_send: bool
    timestamp: str
    location: Location | None
    base_decision: InternalDecision
    debug: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parking_space_id": self.parking_space_id,
            "status": self.status.value,
            "stable_plate_read": self.stable_plate_read,
            "confidence_level": self.confidence_level,
            "confirmed_frames": self.confirmed_frames,
            "should_send": self.should_send,
            "timestamp": self.timestamp,
            "location": self.location.to_dict() if self.location else None,
            "base_decision": self.base_decision.to_dict(),
            "debug": dict(self.debug),
        }


@dataclass(frozen=True)
class WebsitePayload:
    plate_read: str
    time: str
    location: dict[str, float] | Location
    confidence_level: float

    def to_dict(self) -> dict[str, Any]:
        location_payload = self.location.to_dict() if isinstance(self.location, Location) else dict(self.location)
        return {
            "plate_read": self.plate_read,
            "time": self.time,
            "location": location_payload,
            "confidence_level": self.confidence_level,
        }


@dataclass(frozen=True)
class AssociationFrameInput:
    spaces: Sequence[ParkingSpace]
    vehicles: Sequence[VehicleDetection]
    plates: Sequence[PlateDetection]
    context: FrameContext


@dataclass
class FrameAssociationResult:
    decisions: list[InternalDecision]
    stabilized_decisions: list[TemporalDecision]
    website_payloads: list[WebsitePayload]

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions": [decision.to_dict() for decision in self.decisions],
            "stabilized_decisions": [decision.to_dict() for decision in self.stabilized_decisions],
            "website_payloads": [payload.to_dict() for payload in self.website_payloads],
        }
