from __future__ import annotations

import logging
import math
import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from plate_utils import normalize_plate_read, parse_timestamp

from .schemas import JetsonTelemetryEnvelope

if TYPE_CHECKING:
    from .gps_calibration import SegmentMapper


logger = logging.getLogger(__name__)
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_meters = 6371000.0
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a_value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    c_value = 2 * math.asin(math.sqrt(a_value))
    return radius_meters * c_value


def point_in_polygon(latitude: float, longitude: float, polygon: list[tuple[float, float]]) -> bool:
    inside = False
    if len(polygon) < 3:
        return False

    previous_lat, previous_lon = polygon[-1]
    for current_lat, current_lon in polygon:
        intersects = ((current_lon > longitude) != (previous_lon > longitude)) and (
            latitude
            < (previous_lat - current_lat) * (longitude - current_lon) / ((previous_lon - current_lon) or 1e-9)
            + current_lat
        )
        if intersects:
            inside = not inside
        previous_lat, previous_lon = current_lat, current_lon
    return inside


@dataclass(frozen=True)
class IncomingPlateDetection:
    detection_id: str
    plate_read: str | None
    timestamp: str
    latitude: float | None
    longitude: float | None
    confidence_level: float
    heading_deg: float | None = None
    source_camera: str | None = None
    bbox_xyxy: tuple[float, float, float, float] | None = None
    bbox_width_px: float | None = None
    bbox_height_px: float | None = None
    bbox_area_px: float | None = None
    image_id: str | None = None
    image_url: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def has_valid_gps(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def has_bbox_metrics(self) -> bool:
        return self.bbox_height_px is not None and self.bbox_area_px is not None


@dataclass(frozen=True)
class DetectionCandidate:
    space_id: str
    score: float
    distance_to_space_m: float
    inside_polygon: bool
    distance_rank_score: float


@dataclass(frozen=True)
class SpaceDecision:
    space_id: str
    status: str
    plate_read: str | None
    confidence: float
    source_detection_time: str | None
    distance_to_space_m: float | None
    reason: str | None
    location: dict[str, float] | None = None
    detection_id: str | None = None
    image_id: str | None = None
    image_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "space_id": self.space_id,
            "status": self.status,
            "plate_read": self.plate_read,
            "confidence": self.confidence,
            "source_detection_time": self.source_detection_time,
            "distance_to_space_m": self.distance_to_space_m,
            "reason": self.reason,
            "location": self.location,
            "detection_id": self.detection_id,
            "image_id": self.image_id,
            "image_url": self.image_url,
        }


@dataclass(frozen=True)
class DetectionAssociationResult:
    detection_id: str
    status: str
    assigned_space_id: str | None
    score: float
    reason: str | None
    candidates: list[dict[str, Any]]
    source_camera: str | None = None
    bbox_width_px: float | None = None
    bbox_height_px: float | None = None
    bbox_area_px: float | None = None
    bbox_filter_kept: bool | None = None
    bbox_filter_reason: str | None = None
    bbox_filter_rank: int | None = None
    bbox_window_key: str | None = None
    image_id: str | None = None
    image_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "detection_id": self.detection_id,
            "status": self.status,
            "assigned_space_id": self.assigned_space_id,
            "score": self.score,
            "reason": self.reason,
            "candidates": list(self.candidates),
            "source_camera": self.source_camera,
            "bbox_width_px": self.bbox_width_px,
            "bbox_height_px": self.bbox_height_px,
            "bbox_area_px": self.bbox_area_px,
            "bbox_filter_kept": self.bbox_filter_kept,
            "bbox_filter_reason": self.bbox_filter_reason,
            "bbox_filter_rank": self.bbox_filter_rank,
            "bbox_window_key": self.bbox_window_key,
            "image_id": self.image_id,
            "image_url": self.image_url,
        }


@dataclass(frozen=True)
class BBoxFilterDecision:
    kept: bool
    reason: str
    rank: int | None
    window_key: str | None


@dataclass(frozen=True)
class LotResolutionResult:
    space_decisions: list[SpaceDecision]
    detection_results: list[DetectionAssociationResult]
    generated_at: str

    def changed_space_ids(self) -> list[str]:
        return [decision.space_id for decision in self.space_decisions]


@dataclass
class _SpaceEvent:
    status: str
    plate_read: str | None
    confidence: float
    timestamp: datetime
    source_detection_time: str | None
    distance_to_space_m: float | None
    reason: str | None
    location: dict[str, float] | None
    detection_id: str | None
    image_id: str | None
    image_url: str | None
    drive_by: bool = False


@dataclass(frozen=True)
class LotSpaceAssociationConfig:
    """GPS-based stall matcher with an optional bbox pre-filter.

    The bbox stage only decides which detections are eligible to reach the
    existing GPS matcher. Stall assignment still comes from GPS polygon and
    distance scoring, followed by the same temporal smoothing logic.
    """

    outside_space_max_distance_m: float = 3.0
    ambiguous_score_margin: float = 0.08
    ambiguous_distance_margin_m: float = 0.75
    empty_after_seconds: int = 25
    history_window_seconds: int = 45
    min_confirmations_for_occupied: int = 2
    min_vote_share: float = 0.60
    min_stable_confidence: float = 0.40
    empty_confidence_floor: float = 0.90
    drive_by_clear_radius_m: float = 15.0
    auto_clear_occupied_spaces: bool = False
    bbox_filter_enabled: bool = True
    bbox_window_sec: float = 2.0
    bbox_top_k_per_window: int = 1
    bbox_min_relative_height_ratio: float = 0.65
    bbox_min_absolute_height_px: float = 0.0
    bbox_use_area_tiebreak: bool = True
    heading_section_filter_enabled: bool = True
    heading_section_tolerance_deg: float = 45.0


_HEADING_SECTION_FILTER_TARGETS = {"A", "B"}


def _normalize_heading(heading_deg: float | None) -> float | None:
    if heading_deg is None:
        return None
    return heading_deg % 360.0


def _angular_distance_deg(lhs: float, rhs: float) -> float:
    raw_delta = abs(lhs - rhs) % 360.0
    return min(raw_delta, 360.0 - raw_delta)


class LotSpaceAssociationService:
    def __init__(
        self,
        parking_spaces: dict[str, dict[str, Any]],
        config: LotSpaceAssociationConfig | None = None,
        gps_mapper: "SegmentMapper | None" = None,
    ) -> None:
        self.config = config or LotSpaceAssociationConfig()
        self._gps_mapper = gps_mapper
        self._lock = threading.RLock()
        self._spaces = self._build_space_index(parking_spaces)
        self._history: dict[str, deque[_SpaceEvent]] = {
            space_id: deque(maxlen=40) for space_id in self._spaces
        }
        self._latest_decisions: dict[str, SpaceDecision] = {
            space_id: SpaceDecision(
                space_id=space_id,
                status="EMPTY",
                plate_read=None,
                confidence=self.config.empty_confidence_floor,
                source_detection_time=None,
                distance_to_space_m=None,
                reason="no_valid_detection",
                location=None,
                detection_id=None,
                image_id=None,
                image_url=None,
            )
            for space_id in self._spaces
        }

    @staticmethod
    def _build_space_index(parking_spaces: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        indexed: dict[str, dict[str, Any]] = {}
        for space_id, values in parking_spaces.items():
            polygon_points = []
            for point in values.get("polygon", []):
                if isinstance(point, dict):
                    polygon_points.append((float(point["latitude"]), float(point["longitude"])))
                else:
                    polygon_points.append((float(point[0]), float(point[1])))
            indexed[space_id] = {
                "space_id": space_id,
                "center_lat": float(values.get("latitude")),
                "center_lon": float(values.get("longitude")),
                "polygon": polygon_points,
            }
        return indexed

    def reset(self) -> None:
        with self._lock:
            self._history = {
                space_id: deque(maxlen=40) for space_id in self._spaces
            }
            self._latest_decisions = {
                space_id: SpaceDecision(
                    space_id=space_id,
                    status="EMPTY",
                    plate_read=None,
                    confidence=self.config.empty_confidence_floor,
                    source_detection_time=None,
                    distance_to_space_m=None,
                    reason="no_valid_detection",
                    location=None,
                    detection_id=None,
                    image_id=None,
                    image_url=None,
                )
                for space_id in self._spaces
            }

    def _prune_history(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.config.history_window_seconds)
        for history in self._history.values():
            while history and history[0].timestamp < cutoff:
                history.popleft()

    @staticmethod
    def _extract_location(detection_payload: dict[str, Any]) -> tuple[float | None, float | None]:
        location = detection_payload.get("location") if isinstance(detection_payload.get("location"), dict) else {}
        if not location:
            location = detection_payload.get("gps") if isinstance(detection_payload.get("gps"), dict) else {}

        latitude = coerce_float(location.get("lat"))
        if latitude is None:
            latitude = coerce_float(location.get("latitude"))
        if latitude is None:
            latitude = coerce_float(detection_payload.get("latitude"))

        longitude = coerce_float(location.get("lon"))
        if longitude is None:
            longitude = coerce_float(location.get("longitude"))
        if longitude is None:
            longitude = coerce_float(detection_payload.get("longitude"))

        return latitude, longitude

    @staticmethod
    def _extract_bbox_metrics(
        detection_payload: dict[str, Any],
    ) -> tuple[tuple[float, float, float, float] | None, float | None, float | None, float | None]:
        bbox_payload = detection_payload.get("bbox_xyxy")
        if bbox_payload is None:
            return None, None, None, None

        raw_points: list[Any]
        if isinstance(bbox_payload, dict):
            raw_points = [
                bbox_payload.get("x1"),
                bbox_payload.get("y1"),
                bbox_payload.get("x2"),
                bbox_payload.get("y2"),
            ]
        elif isinstance(bbox_payload, (list, tuple)) and len(bbox_payload) >= 4:
            raw_points = list(bbox_payload[:4])
        else:
            return None, None, None, None

        parsed_points: list[float] = []
        for value in raw_points:
            parsed = coerce_float(value)
            if parsed is None:
                return None, None, None, None
            parsed_points.append(parsed)

        x1, y1, x2, y2 = parsed_points
        bbox_width_px = max(0.0, x2 - x1)
        bbox_height_px = max(0.0, y2 - y1)
        if bbox_width_px <= 0.0 or bbox_height_px <= 0.0:
            return None, None, None, None

        bbox_xyxy = (x1, y1, x2, y2)
        bbox_area_px = bbox_width_px * bbox_height_px
        return bbox_xyxy, bbox_width_px, bbox_height_px, bbox_area_px

    def _normalize_plate_strict(self, raw: str | None) -> str | None:
        return normalize_plate_read(raw)

    def _normalize_detection(self, detection_payload: dict[str, Any], fallback_timestamp: str, heading_deg: float | None = None) -> IncomingPlateDetection | None:
        latitude, longitude = self._extract_location(detection_payload)
        if latitude is not None and longitude is not None and self._gps_mapper is not None:
            mapped = self._gps_mapper.map_point(latitude, longitude)
            if mapped is not None:
                latitude, longitude = mapped
        bbox_xyxy, bbox_width_px, bbox_height_px, bbox_area_px = self._extract_bbox_metrics(detection_payload)
        image_id = detection_payload.get("image_id") or detection_payload.get("upload_id")
        image_url = detection_payload.get("image_url")
        timestamp = (
            detection_payload.get("time")
            or detection_payload.get("timestamp")
            or detection_payload.get("detected_at")
            or fallback_timestamp
            or utcnow_iso()
        )
        raw_plate = (
            detection_payload.get("plate_read")
            or detection_payload.get("detected_plate")
            or detection_payload.get("plate")
            or detection_payload.get("plate_text")
            or detection_payload.get("license_plate")
        )
        source_camera_raw = (
            str(detection_payload.get("source_camera") or detection_payload.get("camera_id")).strip()
            if (detection_payload.get("source_camera") or detection_payload.get("camera_id")) is not None
            else None
        )
        return IncomingPlateDetection(
            detection_id=str(
                detection_payload.get("detection_id")
                or detection_payload.get("event_id")
                or uuid.uuid4().hex[:10]
            ),
            plate_read=self._normalize_plate_strict(raw_plate),
            timestamp=timestamp,
            latitude=latitude,
            longitude=longitude,
            confidence_level=float(detection_payload.get("confidence_level") or detection_payload.get("confidence") or 0.0),
            source_camera=source_camera_raw or None,
            bbox_xyxy=bbox_xyxy,
            bbox_width_px=bbox_width_px,
            bbox_height_px=bbox_height_px,
            bbox_area_px=bbox_area_px,
            image_id=str(image_id) if image_id else None,
            image_url=str(image_url) if image_url else None,
            heading_deg=float(detection_payload["heading_deg"]) if detection_payload.get("heading_deg") is not None else heading_deg,
            raw_payload=dict(detection_payload),
        )

    def _extract_detections(self, payload: dict[str, Any], device_id: str) -> list[IncomingPlateDetection]:
        envelope = JetsonTelemetryEnvelope.model_validate(payload)
        fallback_timestamp = envelope.timestamp or utcnow_iso()
        payload_heading = payload.get("heading_deg")
        payload_heading_float = float(payload_heading) if payload_heading is not None else None
        detections: list[IncomingPlateDetection] = []
        for raw_detection in payload.get("plate_detections", []) or []:
            if not isinstance(raw_detection, dict):
                continue
            normalized = self._normalize_detection(raw_detection, fallback_timestamp, heading_deg=payload_heading_float)
            if normalized is not None and normalized.plate_read:
                detections.append(normalized)
        return detections

    def _build_detection_result(
        self,
        detection: IncomingPlateDetection,
        status: str,
        score: float,
        reason: str | None,
        candidates: list[dict[str, Any]] | None,
        bbox_decision: BBoxFilterDecision,
        assigned_space_id: str | None = None,
    ) -> DetectionAssociationResult:
        return DetectionAssociationResult(
            detection_id=detection.detection_id,
            status=status,
            assigned_space_id=assigned_space_id,
            score=score,
            reason=reason,
            candidates=list(candidates or []),
            source_camera=detection.source_camera,
            bbox_width_px=detection.bbox_width_px,
            bbox_height_px=detection.bbox_height_px,
            bbox_area_px=detection.bbox_area_px,
            bbox_filter_kept=bbox_decision.kept,
            bbox_filter_reason=bbox_decision.reason,
            bbox_filter_rank=bbox_decision.rank,
            bbox_window_key=bbox_decision.window_key,
            image_id=detection.image_id,
            image_url=detection.image_url,
        )

    def _bbox_window_key(self, detection: IncomingPlateDetection) -> str | None:
        if not detection.has_bbox_metrics or self.config.bbox_window_sec <= 0:
            return None

        detection_time = parse_timestamp(detection.timestamp)
        if detection_time is None:
            return None

        bucket_start_epoch = math.floor(detection_time.timestamp() / self.config.bbox_window_sec) * self.config.bbox_window_sec
        bucket_start = datetime.fromtimestamp(bucket_start_epoch, tz=timezone.utc)
        source_camera = detection.source_camera or "unknown-camera"
        return f"{source_camera}:{bucket_start.isoformat()}"

    def _bbox_sort_key(self, detection: IncomingPlateDetection) -> tuple[float, float, float, str]:
        height = detection.bbox_height_px or 0.0
        area = detection.bbox_area_px or 0.0
        area_score = area if self.config.bbox_use_area_tiebreak else 0.0
        return (height, area_score, detection.confidence_level, detection.detection_id)

    def _apply_bbox_prefilter(
        self,
        detections: list[IncomingPlateDetection],
    ) -> tuple[list[IncomingPlateDetection], dict[str, BBoxFilterDecision], dict[str, DetectionAssociationResult]]:
        bbox_decisions: dict[str, BBoxFilterDecision] = {}
        non_match_results: dict[str, DetectionAssociationResult] = {}
        eligible_detections: list[IncomingPlateDetection] = []

        if not detections:
            return eligible_detections, bbox_decisions, non_match_results

        bbox_window_groups: dict[str, list[IncomingPlateDetection]] = defaultdict(list)

        for detection in detections:
            if not detection.has_valid_gps:
                bbox_decision = BBoxFilterDecision(
                    kept=False,
                    reason="missing_gps_location",
                    rank=None,
                    window_key=None,
                )
                bbox_decisions[detection.detection_id] = bbox_decision
                non_match_results[detection.detection_id] = self._build_detection_result(
                    detection,
                    status="REJECTED",
                    score=0.0,
                    reason="missing_gps_location",
                    candidates=[],
                    bbox_decision=bbox_decision,
                )
                continue

            if not self.config.bbox_filter_enabled:
                bbox_decisions[detection.detection_id] = BBoxFilterDecision(
                    kept=True,
                    reason="bbox_filter_disabled",
                    rank=None,
                    window_key=None,
                )
                eligible_detections.append(detection)
                continue

            if not detection.has_bbox_metrics:
                bbox_decisions[detection.detection_id] = BBoxFilterDecision(
                    kept=True,
                    reason="bbox_missing_fallback",
                    rank=None,
                    window_key=None,
                )
                eligible_detections.append(detection)
                continue

            window_key = self._bbox_window_key(detection)
            if window_key is None:
                bbox_decisions[detection.detection_id] = BBoxFilterDecision(
                    kept=True,
                    reason="bbox_window_unavailable_fallback",
                    rank=None,
                    window_key=None,
                )
                eligible_detections.append(detection)
                continue

            bbox_window_groups[window_key].append(detection)

        if not self.config.bbox_filter_enabled:
            return eligible_detections, bbox_decisions, non_match_results

        top_k = max(1, self.config.bbox_top_k_per_window)
        min_height = max(0.0, self.config.bbox_min_absolute_height_px)
        min_relative_ratio = max(0.0, self.config.bbox_min_relative_height_ratio)

        for window_key, grouped_detections in bbox_window_groups.items():
            ranked_detections = sorted(grouped_detections, key=self._bbox_sort_key, reverse=True)
            leader_height = ranked_detections[0].bbox_height_px or 0.0
            log_rows: list[dict[str, Any]] = []

            for index, detection in enumerate(ranked_detections, start=1):
                bbox_height = detection.bbox_height_px or 0.0
                relative_height_ratio = (bbox_height / leader_height) if leader_height > 0 else 0.0

                keep_detection = True
                reason = "single_bbox_candidate" if len(ranked_detections) == 1 else "largest_bbox_in_window"
                if bbox_height < min_height:
                    keep_detection = False
                    reason = "bbox_below_absolute_height_threshold"
                elif index > top_k:
                    keep_detection = False
                    reason = "bbox_rank_exceeds_top_k"
                elif index > 1 and relative_height_ratio < min_relative_ratio:
                    keep_detection = False
                    reason = "bbox_below_relative_height_ratio"
                elif index > 1:
                    reason = "bbox_within_relative_height_ratio"

                bbox_decision = BBoxFilterDecision(
                    kept=keep_detection,
                    reason=reason,
                    rank=index,
                    window_key=window_key,
                )
                bbox_decisions[detection.detection_id] = bbox_decision

                if keep_detection:
                    eligible_detections.append(detection)
                else:
                    non_match_results[detection.detection_id] = self._build_detection_result(
                        detection,
                        status="FILTERED",
                        score=0.0,
                        reason=reason,
                        candidates=[],
                        bbox_decision=bbox_decision,
                    )

                log_rows.append(
                    {
                        "detection_id": detection.detection_id,
                        "kept": keep_detection,
                        "reason": reason,
                        "rank": index,
                        "bbox_height_px": detection.bbox_height_px,
                        "bbox_area_px": detection.bbox_area_px,
                        "confidence": detection.confidence_level,
                    }
                )

            logger.debug("bbox filter window=%s detections=%s", window_key, log_rows)

        return eligible_detections, bbox_decisions, non_match_results

    def _candidate_spaces(self, detection: IncomingPlateDetection) -> list[DetectionCandidate]:
        if not detection.has_valid_gps:
            return []

        # Heading-based section filter: facing south → Section A only, facing north → Section B only.
        allowed_sections: set[str] | None = None
        if self.config.heading_section_filter_enabled and detection.heading_deg is not None:
            heading = _normalize_heading(detection.heading_deg)
            tol = self.config.heading_section_tolerance_deg
            if _angular_distance_deg(heading, 180.0) <= tol:
                allowed_sections = {"A"}
            elif _angular_distance_deg(heading, 0.0) <= tol:
                allowed_sections = {"B"}

        latitude = float(detection.latitude)
        longitude = float(detection.longitude)
        candidates: list[DetectionCandidate] = []
        for space_id, space in self._spaces.items():
            section_id = space_id[0]
            if (
                allowed_sections is not None
                and section_id in _HEADING_SECTION_FILTER_TARGETS
                and section_id not in allowed_sections
            ):
                continue
            inside_polygon = point_in_polygon(latitude, longitude, space["polygon"])
            distance_to_space_m = haversine_distance_meters(
                latitude,
                longitude,
                space["center_lat"],
                space["center_lon"],
            )

            if not inside_polygon and distance_to_space_m > self.config.outside_space_max_distance_m:
                continue

            if inside_polygon:
                distance_rank_score = max(0.45, 1.0 - (distance_to_space_m / max(self.config.outside_space_max_distance_m, 0.1)))
                score = (0.65 + (distance_rank_score * 0.15) + (detection.confidence_level * 0.20))
            else:
                distance_rank_score = max(
                    0.0,
                    1.0 - (distance_to_space_m / max(self.config.outside_space_max_distance_m, 0.1)),
                )
                score = (distance_rank_score * 0.70) + (detection.confidence_level * 0.30)

            candidates.append(
                DetectionCandidate(
                    space_id=space_id,
                    score=score,
                    distance_to_space_m=distance_to_space_m,
                    inside_polygon=inside_polygon,
                    distance_rank_score=distance_rank_score,
                )
            )

        candidates.sort(key=lambda item: (item.score, -item.distance_to_space_m), reverse=True)
        return candidates

    def _association_for_detection(
        self,
        detection: IncomingPlateDetection,
        bbox_decision: BBoxFilterDecision,
    ) -> tuple[DetectionAssociationResult, DetectionCandidate | None]:
        candidates = self._candidate_spaces(detection)
        serialized_candidates = [
            {
                "space_id": candidate.space_id,
                "score": candidate.score,
                "distance_to_space_m": candidate.distance_to_space_m,
                "inside_polygon": candidate.inside_polygon,
            }
            for candidate in candidates[:4]
        ]

        if not candidates:
            return (
                self._build_detection_result(
                    detection,
                    status="REJECTED",
                    score=0.0,
                    reason="no_valid_space_within_distance_threshold",
                    candidates=serialized_candidates,
                    bbox_decision=bbox_decision,
                ),
                None,
            )

        top_candidate = candidates[0]
        second_candidate = candidates[1] if len(candidates) > 1 else None

        if second_candidate is not None:
            score_margin = top_candidate.score - second_candidate.score
            distance_margin = abs(top_candidate.distance_to_space_m - second_candidate.distance_to_space_m)
            if (
                score_margin < self.config.ambiguous_score_margin
                and distance_margin < self.config.ambiguous_distance_margin_m
            ):
                return (
                    self._build_detection_result(
                        detection,
                        status="UNCERTAIN",
                        score=top_candidate.score,
                        reason="ambiguous_location",
                        candidates=serialized_candidates,
                        bbox_decision=bbox_decision,
                    ),
                    None,
                )

        return (
            self._build_detection_result(
                detection,
                status="ASSIGNED",
                score=top_candidate.score,
                reason=None,
                candidates=serialized_candidates,
                bbox_decision=bbox_decision,
                assigned_space_id=top_candidate.space_id,
            ),
            top_candidate,
        )

    def _resolve_assignments(
        self,
        detections: list[IncomingPlateDetection],
        bbox_decisions: dict[str, BBoxFilterDecision],
    ) -> tuple[dict[str, tuple[IncomingPlateDetection, DetectionCandidate]], list[DetectionAssociationResult]]:
        candidate_pairs: list[tuple[float, IncomingPlateDetection, DetectionCandidate]] = []
        detection_results: list[DetectionAssociationResult] = []

        for detection in detections:
            association, candidate = self._association_for_detection(
                detection,
                bbox_decisions.get(
                    detection.detection_id,
                    BBoxFilterDecision(kept=True, reason="bbox_filter_unset", rank=None, window_key=None),
                ),
            )
            detection_results.append(association)
            if association.status == "ASSIGNED" and candidate is not None:
                candidate_pairs.append((candidate.score, detection, candidate))

        candidate_pairs.sort(key=lambda item: item[0], reverse=True)
        assigned_spaces: set[str] = set()
        assigned_detections: set[str] = set()
        assignments: dict[str, tuple[IncomingPlateDetection, DetectionCandidate]] = {}

        for _, detection, candidate in candidate_pairs:
            if detection.detection_id in assigned_detections or candidate.space_id in assigned_spaces:
                continue
            assignments[candidate.space_id] = (detection, candidate)
            assigned_spaces.add(candidate.space_id)
            assigned_detections.add(detection.detection_id)

        updated_results: list[DetectionAssociationResult] = []
        for result in detection_results:
            if result.status != "ASSIGNED":
                updated_results.append(result)
                continue
            if result.assigned_space_id in assignments and assignments[result.assigned_space_id][0].detection_id == result.detection_id:
                updated_results.append(result)
            else:
                updated_results.append(
                    replace(
                        result,
                        status="UNCERTAIN",
                        assigned_space_id=None,
                        reason="competing_detection_for_nearby_spaces",
                    )
                )

        return assignments, updated_results

    def _record_event(self, space_id: str, event: _SpaceEvent) -> None:
        self._history[space_id].append(event)

    def _derive_space_decision(self, space_id: str, now: datetime) -> SpaceDecision:
        history = self._history[space_id]
        recent_events = list(history)
        last_decision = self._latest_decisions[space_id]
        if not recent_events:
            return last_decision

        freshness_cutoff = now - timedelta(seconds=self.config.empty_after_seconds)
        occupied_events = [
            event
            for event in recent_events
            if event.status == "OCCUPIED"
            and event.plate_read
            and event.timestamp >= freshness_cutoff
        ]

        if not self.config.auto_clear_occupied_spaces and last_decision.status == "OCCUPIED":
            if occupied_events:
                latest_occupied = max(occupied_events, key=lambda event: event.timestamp)
                decision = SpaceDecision(
                    space_id=space_id,
                    status="OCCUPIED",
                    plate_read=latest_occupied.plate_read or last_decision.plate_read,
                    confidence=max(last_decision.confidence, latest_occupied.confidence),
                    source_detection_time=latest_occupied.source_detection_time or last_decision.source_detection_time,
                    distance_to_space_m=latest_occupied.distance_to_space_m,
                    reason=None,
                    location=latest_occupied.location,
                    detection_id=latest_occupied.detection_id,
                    image_id=latest_occupied.image_id,
                    image_url=latest_occupied.image_url,
                )
                self._latest_decisions[space_id] = decision
                return decision
            return last_decision

        # If robot drove past this space (drive_by=True) AFTER the last OCCUPIED event,
        # it confirmed no car is there — clear immediately without waiting for the full window.
        if self.config.auto_clear_occupied_spaces and occupied_events:
            last_occupied_ts = max(e.timestamp for e in occupied_events)
            drive_by_clear = any(
                e.drive_by and e.status == "EMPTY" and e.timestamp > last_occupied_ts
                for e in recent_events
            )
            if drive_by_clear:
                decision = SpaceDecision(
                    space_id=space_id,
                    status="EMPTY",
                    plate_read=None,
                    confidence=self.config.empty_confidence_floor,
                    source_detection_time=None,
                    distance_to_space_m=None,
                    reason="drive_by_confirmed_empty",
                    location=None,
                    detection_id=None,
                    image_id=None,
                    image_url=None,
                )
                self._latest_decisions[space_id] = decision
                return decision

        uncertain_events = [
            event
            for event in recent_events
            if event.status == "UNCERTAIN"
            and event.timestamp >= freshness_cutoff
        ]
        empty_events = [event for event in recent_events if event.status == "EMPTY"]

        if occupied_events:
            plate_votes: dict[str, float] = defaultdict(float)
            plate_counts: dict[str, int] = defaultdict(int)
            latest_by_plate: dict[str, _SpaceEvent] = {}
            for event in occupied_events:
                plate_votes[event.plate_read or ""] += event.confidence
                plate_counts[event.plate_read or ""] += 1
                latest_by_plate[event.plate_read or ""] = event

            winner_plate, winner_vote = max(plate_votes.items(), key=lambda item: item[1])
            total_votes = sum(plate_votes.values()) or 1.0
            vote_share = winner_vote / total_votes
            confirmed_count = plate_counts[winner_plate]
            winner_event = latest_by_plate[winner_plate]
            stabilized_confidence = min(1.0, (winner_event.confidence * 0.7) + (vote_share * 0.3))

            if (
                confirmed_count >= self.config.min_confirmations_for_occupied
                and vote_share >= self.config.min_vote_share
                and stabilized_confidence >= self.config.min_stable_confidence
            ):
                decision = SpaceDecision(
                    space_id=space_id,
                    status="OCCUPIED",
                    plate_read=winner_plate,
                    confidence=stabilized_confidence,
                    source_detection_time=winner_event.source_detection_time,
                    distance_to_space_m=winner_event.distance_to_space_m,
                    reason=None,
                    location=winner_event.location,
                    detection_id=winner_event.detection_id,
                    image_id=winner_event.image_id,
                    image_url=winner_event.image_url,
                )
                self._latest_decisions[space_id] = decision
                return decision

            decision = SpaceDecision(
                space_id=space_id,
                status="UNCERTAIN",
                plate_read=None,
                confidence=min(0.69, stabilized_confidence),
                source_detection_time=winner_event.source_detection_time,
                distance_to_space_m=winner_event.distance_to_space_m,
                reason="insufficient_temporal_consensus",
                location=winner_event.location,
                detection_id=winner_event.detection_id,
                image_id=winner_event.image_id,
                image_url=winner_event.image_url,
            )
            self._latest_decisions[space_id] = decision
            return decision

        if uncertain_events:
            latest_uncertain = uncertain_events[-1]
            decision = SpaceDecision(
                space_id=space_id,
                status="UNCERTAIN",
                plate_read=None,
                confidence=min(0.65, latest_uncertain.confidence),
                source_detection_time=latest_uncertain.source_detection_time,
                distance_to_space_m=latest_uncertain.distance_to_space_m,
                reason=latest_uncertain.reason or "ambiguous_location",
                location=latest_uncertain.location,
                detection_id=latest_uncertain.detection_id,
                image_id=latest_uncertain.image_id,
                image_url=latest_uncertain.image_url,
            )
            self._latest_decisions[space_id] = decision
            return decision

        if empty_events:
            latest_empty = empty_events[-1]
            decision = SpaceDecision(
                space_id=space_id,
                status="EMPTY",
                plate_read=None,
                confidence=max(self.config.empty_confidence_floor, latest_empty.confidence),
                source_detection_time=latest_empty.source_detection_time,
                distance_to_space_m=None,
                reason=latest_empty.reason or "no_valid_detection",
                location=None,
                detection_id=None,
                image_id=None,
                image_url=None,
            )
            self._latest_decisions[space_id] = decision
            return decision

        decision = SpaceDecision(
            space_id=space_id,
            status="EMPTY",
            plate_read=None,
            confidence=self.config.empty_confidence_floor,
            source_detection_time=None,
            distance_to_space_m=None,
            reason="no_recent_valid_detection",
            location=None,
            detection_id=None,
            image_id=None,
            image_url=None,
        )
        self._latest_decisions[space_id] = decision
        return decision

    def _robot_nearby_spaces(self, payload: dict[str, Any]) -> set[str]:
        """Return space IDs within drive_by_clear_radius_m of the robot's current GPS."""
        loc = payload.get("location") if isinstance(payload.get("location"), dict) else {}
        robot_lat = coerce_float(loc.get("lat") or loc.get("latitude")) if loc else None
        robot_lon = coerce_float(loc.get("lon") or loc.get("longitude")) if loc else None
        if robot_lat is None:
            robot_lat = coerce_float(payload.get("latitude"))
        if robot_lon is None:
            robot_lon = coerce_float(payload.get("longitude"))
        if robot_lat is None or robot_lon is None:
            return set()

        radius = self.config.drive_by_clear_radius_m
        return {
            space_id
            for space_id, space in self._spaces.items()
            if haversine_distance_meters(robot_lat, robot_lon, space["center_lat"], space["center_lon"]) <= radius
        }

    def ingest(self, device_id: str, payload: dict[str, Any]) -> LotResolutionResult:
        detections = self._extract_detections(payload, device_id)
        current_time = parse_timestamp(payload.get("timestamp")) or utcnow()

        with self._lock:
            self._prune_history(current_time)
            nearby_spaces = self._robot_nearby_spaces(payload)
            eligible_detections, bbox_decisions, prefilter_results = self._apply_bbox_prefilter(detections)
            assignments, matched_results = self._resolve_assignments(eligible_detections, bbox_decisions)
            matched_results_by_id = {result.detection_id: result for result in matched_results}
            detection_results = [
                prefilter_results.get(detection.detection_id) or matched_results_by_id[detection.detection_id]
                for detection in detections
                if detection.detection_id in prefilter_results or detection.detection_id in matched_results_by_id
            ]

            affected_uncertain_spaces: set[str] = set()
            for result in matched_results:
                if result.status != "UNCERTAIN" or not result.candidates:
                    continue
                if result.reason == "competing_detection_for_nearby_spaces":
                    continue
                for candidate in result.candidates[:2]:
                    affected_uncertain_spaces.add(candidate["space_id"])

            for space_id in self._spaces:
                assignment = assignments.get(space_id)
                if assignment is not None:
                    detection, candidate = assignment
                    effective_confidence = detection.confidence_level if detection.confidence_level > 0 else candidate.score
                    self._record_event(
                        space_id,
                        _SpaceEvent(
                            status="OCCUPIED",
                            plate_read=detection.plate_read,
                            confidence=min(1.0, max(0.0, (effective_confidence * 0.7) + (candidate.score * 0.3))),
                            timestamp=current_time,
                            source_detection_time=detection.timestamp,
                            distance_to_space_m=candidate.distance_to_space_m,
                            reason=None,
                            location={"lat": detection.latitude, "lon": detection.longitude},
                            detection_id=detection.detection_id,
                            image_id=detection.image_id,
                            image_url=detection.image_url,
                        ),
                    )
                    continue

                if space_id in affected_uncertain_spaces:
                    if not (
                        not self.config.auto_clear_occupied_spaces
                        and self._latest_decisions[space_id].status == "OCCUPIED"
                    ):
                        self._record_event(
                            space_id,
                            _SpaceEvent(
                                status="UNCERTAIN",
                                plate_read=None,
                                confidence=0.45,
                                timestamp=current_time,
                                source_detection_time=payload.get("timestamp"),
                                distance_to_space_m=None,
                                reason="ambiguous_location",
                                location=None,
                                detection_id=None,
                                image_id=None,
                                image_url=None,
                            ),
                        )
                else:
                    if self.config.auto_clear_occupied_spaces:
                        is_drive_by = space_id in nearby_spaces
                        self._record_event(
                            space_id,
                            _SpaceEvent(
                                status="EMPTY",
                                plate_read=None,
                                confidence=self.config.empty_confidence_floor,
                                timestamp=current_time,
                                source_detection_time=payload.get("timestamp"),
                                distance_to_space_m=None,
                                reason="drive_by_no_detection" if is_drive_by else "no_valid_detection",
                                location=None,
                                detection_id=None,
                                image_id=None,
                                image_url=None,
                                drive_by=is_drive_by,
                            ),
                        )

            space_decisions = [self._derive_space_decision(space_id, current_time) for space_id in self._spaces]
            return LotResolutionResult(
                space_decisions=space_decisions,
                detection_results=detection_results,
                generated_at=current_time.isoformat(),
            )
