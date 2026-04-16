from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import polygon_centroid
from .models import (
    AssociationFrameInput,
    FrameAssociationResult,
    InternalDecision,
    ParkingSpace,
    SpaceStatus,
)
from .payload import WebsitePayloadFormatter, WebsitePayloadFormatterConfig
from .plate_vehicle_matcher import PlateVehicleMatcher, PlateVehicleMatcherConfig
from .temporal import TemporalStabilizer, TemporalStabilizerConfig
from .vehicle_space_matcher import VehicleSpaceMatcher, VehicleSpaceMatcherConfig


@dataclass(frozen=True)
class DecisionEngineConfig:
    vehicle_matcher: VehicleSpaceMatcherConfig = field(default_factory=VehicleSpaceMatcherConfig)
    plate_matcher: PlateVehicleMatcherConfig = field(default_factory=PlateVehicleMatcherConfig)
    temporal: TemporalStabilizerConfig = field(default_factory=TemporalStabilizerConfig)
    payload_formatter: WebsitePayloadFormatterConfig = field(default_factory=WebsitePayloadFormatterConfig)
    empty_space_uncertain_score: float = 0.30


class ParkingAssociationDecisionEngine:
    def __init__(self, config: DecisionEngineConfig | None = None) -> None:
        self.config = config or DecisionEngineConfig()
        self.vehicle_matcher = VehicleSpaceMatcher(self.config.vehicle_matcher)
        self.plate_matcher = PlateVehicleMatcher(self.config.plate_matcher)
        self.temporal_stabilizer = TemporalStabilizer(self.config.temporal)
        self.payload_formatter = WebsitePayloadFormatter(self.config.payload_formatter)

    def _space_debug_base(self, space: ParkingSpace) -> dict[str, object]:
        return {
            "space_polygon_pixels": [point.to_list() for point in space.polygon],
            "space_center_pixel": polygon_centroid(space.polygon).to_list(),
        }

    def _empty_or_uncertain_without_assignment(self, space: ParkingSpace, frame: AssociationFrameInput) -> InternalDecision:
        best_rejected = None
        for vehicle in frame.vehicles:
            evidence = self.vehicle_matcher.score_vehicle_for_space(vehicle, space)
            if best_rejected is None or evidence.score > best_rejected.score:
                best_rejected = evidence

        debug = self._space_debug_base(space)
        debug["nearby_vehicle_count"] = len(frame.vehicles)
        debug["best_rejected_vehicle"] = best_rejected.to_dict() if best_rejected else None

        if best_rejected and best_rejected.score >= self.config.empty_space_uncertain_score:
            return InternalDecision(
                parking_space_id=space.space_id,
                status=SpaceStatus.UNCERTAIN,
                association_confidence=best_rejected.score,
                frame_id=frame.context.frame_id,
                timestamp=frame.context.timestamp,
                gps_location=frame.context.gps_location,
                vehicle=best_rejected,
                plate=None,
                rejection_reason="vehicle_candidate_near_space_but_not_trusted",
                debug=debug,
            )

        return InternalDecision(
            parking_space_id=space.space_id,
            status=SpaceStatus.EMPTY,
            association_confidence=0.9 if not frame.vehicles else 0.72,
            frame_id=frame.context.frame_id,
            timestamp=frame.context.timestamp,
            gps_location=frame.context.gps_location,
            vehicle=None,
            plate=None,
            rejection_reason="no_vehicle_occupies_target_space",
            debug=debug,
        )

    def process_frame(self, frame: AssociationFrameInput) -> FrameAssociationResult:
        spaces = list(frame.spaces)
        vehicles = list(frame.vehicles)
        plates = list(frame.plates)

        assignments = self.vehicle_matcher.match(vehicles, spaces)
        decisions: list[InternalDecision] = []

        for space in spaces:
            assigned_pair = assignments.get(space.space_id)
            if assigned_pair is None:
                decisions.append(self._empty_or_uncertain_without_assignment(space, frame))
                continue

            matched_vehicle, vehicle_evidence = assigned_pair
            plate_evidence = self.plate_matcher.match_for_vehicle(
                vehicle=matched_vehicle,
                plates=plates,
                all_vehicles=vehicles,
            )

            debug = self._space_debug_base(space)
            debug.update(
                {
                    "bottom_center_inside_space": vehicle_evidence.bottom_center_inside_space,
                    "lower_overlap_ratio": vehicle_evidence.lower_overlap_ratio,
                    "vehicle_space_score": vehicle_evidence.score,
                    "neighboring_vehicle_count": max(0, len(vehicles) - 1),
                    "plate_association_valid": bool(plate_evidence),
                }
            )

            rejection_reason = None
            association_confidence = vehicle_evidence.score
            if plate_evidence:
                association_confidence = min(
                    1.0,
                    (vehicle_evidence.score * 0.56) + (plate_evidence.score * 0.44),
                )
                debug["plate_vehicle_score"] = plate_evidence.score
                debug["plate_inside_vehicle"] = plate_evidence.plate_inside_vehicle
                debug["plate_neighbor_margin"] = plate_evidence.neighbor_margin
            else:
                rejection_reason = "vehicle_confirmed_but_plate_not_confidently_matched"
                debug["plate_vehicle_score"] = 0.0

            decisions.append(
                InternalDecision(
                    parking_space_id=space.space_id,
                    status=SpaceStatus.OCCUPIED,
                    association_confidence=association_confidence,
                    frame_id=frame.context.frame_id,
                    timestamp=frame.context.timestamp,
                    gps_location=frame.context.gps_location,
                    vehicle=vehicle_evidence,
                    plate=plate_evidence,
                    rejection_reason=rejection_reason,
                    debug=debug,
                )
            )

        stabilized_decisions = [self.temporal_stabilizer.update(decision) for decision in decisions]
        website_payloads = []
        for stabilized in stabilized_decisions:
            payload = self.payload_formatter.format(stabilized)
            if payload is not None:
                website_payloads.append(payload)

        return FrameAssociationResult(
            decisions=decisions,
            stabilized_decisions=stabilized_decisions,
            website_payloads=website_payloads,
        )
