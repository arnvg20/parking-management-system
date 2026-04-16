from __future__ import annotations

import math
import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .schemas import JetsonTelemetryEnvelope


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


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
    latitude: float
    longitude: float
    confidence_level: float
    raw_payload: dict[str, Any] = field(default_factory=dict)


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
        }


@dataclass(frozen=True)
class DetectionAssociationResult:
    detection_id: str
    status: str
    assigned_space_id: str | None
    score: float
    reason: str | None
    candidates: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "detection_id": self.detection_id,
            "status": self.status,
            "assigned_space_id": self.assigned_space_id,
            "score": self.score,
            "reason": self.reason,
            "candidates": list(self.candidates),
        }


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


@dataclass(frozen=True)
class LotSpaceAssociationConfig:
    outside_space_max_distance_m: float = 3.0
    ambiguous_score_margin: float = 0.08
    ambiguous_distance_margin_m: float = 0.75
    empty_after_seconds: int = 25
    history_window_seconds: int = 45
    min_confirmations_for_occupied: int = 2
    min_vote_share: float = 0.60
    min_stable_confidence: float = 0.72
    empty_confidence_floor: float = 0.90


class LotSpaceAssociationService:
    def __init__(self, parking_spaces: dict[str, dict[str, Any]], config: LotSpaceAssociationConfig | None = None) -> None:
        self.config = config or LotSpaceAssociationConfig()
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

    def _prune_history(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.config.history_window_seconds)
        for history in self._history.values():
            while history and history[0].timestamp < cutoff:
                history.popleft()

    def _normalize_detection(self, detection_payload: dict[str, Any], fallback_timestamp: str) -> IncomingPlateDetection | None:
        location = detection_payload.get("location") if isinstance(detection_payload.get("location"), dict) else {}
        latitude = location.get("lat")
        longitude = location.get("lon")
        if latitude is None or longitude is None:
            return None

        timestamp = detection_payload.get("time") or detection_payload.get("timestamp") or fallback_timestamp or utcnow_iso()
        return IncomingPlateDetection(
            detection_id=str(detection_payload.get("detection_id") or uuid.uuid4().hex[:10]),
            plate_read=detection_payload.get("plate_read")
            or detection_payload.get("detected_plate")
            or detection_payload.get("plate"),
            timestamp=timestamp,
            latitude=float(latitude),
            longitude=float(longitude),
            confidence_level=float(detection_payload.get("confidence_level") or detection_payload.get("confidence") or 0.0),
            raw_payload=dict(detection_payload),
        )

    def _extract_detections(self, payload: dict[str, Any], device_id: str) -> list[IncomingPlateDetection]:
        envelope = JetsonTelemetryEnvelope.model_validate(payload)
        fallback_timestamp = envelope.timestamp or utcnow_iso()
        detections: list[IncomingPlateDetection] = []
        for raw_detection in payload.get("plate_detections", []) or []:
            if not isinstance(raw_detection, dict):
                continue
            normalized = self._normalize_detection(raw_detection, fallback_timestamp)
            if normalized is not None:
                detections.append(normalized)
        return detections

    def _candidate_spaces(self, detection: IncomingPlateDetection) -> list[DetectionCandidate]:
        candidates: list[DetectionCandidate] = []
        for space_id, space in self._spaces.items():
            inside_polygon = point_in_polygon(detection.latitude, detection.longitude, space["polygon"])
            distance_to_space_m = haversine_distance_meters(
                detection.latitude,
                detection.longitude,
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

    def _association_for_detection(self, detection: IncomingPlateDetection) -> tuple[DetectionAssociationResult, DetectionCandidate | None]:
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
                DetectionAssociationResult(
                    detection_id=detection.detection_id,
                    status="REJECTED",
                    assigned_space_id=None,
                    score=0.0,
                    reason="no_valid_space_within_distance_threshold",
                    candidates=serialized_candidates,
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
                    DetectionAssociationResult(
                        detection_id=detection.detection_id,
                        status="UNCERTAIN",
                        assigned_space_id=None,
                        score=top_candidate.score,
                        reason="ambiguous_location",
                        candidates=serialized_candidates,
                    ),
                    None,
                )

        return (
            DetectionAssociationResult(
                detection_id=detection.detection_id,
                status="ASSIGNED",
                assigned_space_id=top_candidate.space_id,
                score=top_candidate.score,
                reason=None,
                candidates=serialized_candidates,
            ),
            top_candidate,
        )

    def _resolve_assignments(
        self,
        detections: list[IncomingPlateDetection],
    ) -> tuple[dict[str, tuple[IncomingPlateDetection, DetectionCandidate]], list[DetectionAssociationResult]]:
        candidate_pairs: list[tuple[float, IncomingPlateDetection, DetectionCandidate]] = []
        detection_results: list[DetectionAssociationResult] = []

        for detection in detections:
            association, candidate = self._association_for_detection(detection)
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
                    DetectionAssociationResult(
                        detection_id=result.detection_id,
                        status="UNCERTAIN",
                        assigned_space_id=None,
                        score=result.score,
                        reason="competing_detection_for_nearby_spaces",
                        candidates=result.candidates,
                    )
                )

        return assignments, updated_results

    def _record_event(self, space_id: str, event: _SpaceEvent) -> None:
        self._history[space_id].append(event)

    def _derive_space_decision(self, space_id: str, now: datetime) -> SpaceDecision:
        history = self._history[space_id]
        recent_events = list(history)
        if not recent_events:
            return self._latest_decisions[space_id]

        freshness_cutoff = now - timedelta(seconds=self.config.empty_after_seconds)
        occupied_events = [
            event
            for event in recent_events
            if event.status == "OCCUPIED"
            and event.plate_read
            and event.timestamp >= freshness_cutoff
        ]
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
        )
        self._latest_decisions[space_id] = decision
        return decision

    def ingest(self, device_id: str, payload: dict[str, Any]) -> LotResolutionResult:
        detections = self._extract_detections(payload, device_id)
        current_time = parse_timestamp(payload.get("timestamp")) or utcnow()

        with self._lock:
            self._prune_history(current_time)
            assignments, detection_results = self._resolve_assignments(detections)

            affected_uncertain_spaces: set[str] = set()
            for result in detection_results:
                if result.status != "UNCERTAIN" or not result.candidates:
                    continue
                for candidate in result.candidates[:2]:
                    affected_uncertain_spaces.add(candidate["space_id"])

            for space_id in self._spaces:
                assignment = assignments.get(space_id)
                if assignment is not None:
                    detection, candidate = assignment
                    self._record_event(
                        space_id,
                        _SpaceEvent(
                            status="OCCUPIED",
                            plate_read=detection.plate_read,
                            confidence=min(1.0, max(0.0, (detection.confidence_level * 0.7) + (candidate.score * 0.3))),
                            timestamp=parse_timestamp(detection.timestamp) or current_time,
                            source_detection_time=detection.timestamp,
                            distance_to_space_m=candidate.distance_to_space_m,
                            reason=None,
                            location={"lat": detection.latitude, "lon": detection.longitude},
                            detection_id=detection.detection_id,
                        ),
                    )
                    continue

                if space_id in affected_uncertain_spaces:
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
                        ),
                    )
                else:
                    self._record_event(
                        space_id,
                        _SpaceEvent(
                            status="EMPTY",
                            plate_read=None,
                            confidence=self.config.empty_confidence_floor,
                            timestamp=current_time,
                            source_detection_time=payload.get("timestamp"),
                            distance_to_space_m=None,
                            reason="no_valid_detection",
                            location=None,
                            detection_id=None,
                        ),
                    )

            space_decisions = [self._derive_space_decision(space_id, current_time) for space_id in self._spaces]
            return LotResolutionResult(
                space_decisions=space_decisions,
                detection_results=detection_results,
                generated_at=current_time.isoformat(),
            )
