from __future__ import annotations

from dataclasses import dataclass

from .geometry import (
    clamp,
    euclidean_distance,
    normalized_distance_score,
    point_in_polygon,
    polygon_centroid,
    polygon_diagonal_length,
    sampled_overlap_ratio,
)
from .models import ParkingSpace, VehicleDetection, VehicleSpaceEvidence


@dataclass(frozen=True)
class VehicleSpaceMatcherConfig:
    lower_region_fraction: float = 0.40
    min_detection_confidence: float = 0.25
    occupancy_ratio_threshold: float = 0.22
    min_match_score: float = 0.42
    overlap_weight: float = 0.38
    bottom_center_weight: float = 0.30
    distance_weight: float = 0.20
    detection_weight: float = 0.12


class VehicleSpaceMatcher:
    def __init__(self, config: VehicleSpaceMatcherConfig | None = None) -> None:
        self.config = config or VehicleSpaceMatcherConfig()

    def _depth_score(self, vehicle: VehicleDetection, space: ParkingSpace) -> float:
        expected_bottom_y = space.metadata.get("expected_bottom_y")
        expected_height_range = space.metadata.get("expected_vehicle_height_px")

        score = 1.0
        if expected_bottom_y is not None:
            delta = abs(vehicle.bbox.bottom_center.y - float(expected_bottom_y))
            tolerance = max(20.0, float(space.metadata.get("expected_bottom_y_tolerance", 120.0)))
            score *= clamp(1.0 - (delta / tolerance))

        if isinstance(expected_height_range, (tuple, list)) and len(expected_height_range) == 2:
            min_height, max_height = float(expected_height_range[0]), float(expected_height_range[1])
            height = vehicle.bbox.height
            if height < min_height:
                score *= clamp(height / max(1.0, min_height))
            elif height > max_height:
                score *= clamp(max_height / max(1.0, height))

        return score

    def score_vehicle_for_space(self, vehicle: VehicleDetection, space: ParkingSpace) -> VehicleSpaceEvidence:
        if vehicle.detection_confidence < self.config.min_detection_confidence:
            return VehicleSpaceEvidence(
                parking_space_id=space.space_id,
                vehicle_id=vehicle.detection_id,
                vehicle_bbox_pixels=vehicle.bbox.to_list(),
                vehicle_center_pixel=vehicle.bbox.center.to_list(),
                vehicle_bottom_center_pixel=vehicle.bbox.bottom_center.to_list(),
                occupancy_ratio=0.0,
                lower_overlap_ratio=0.0,
                bottom_center_inside_space=False,
                distance_to_space_center_px=0.0,
                detection_confidence=vehicle.detection_confidence,
                depth_score=0.0,
                score=0.0,
                rejection_reason="vehicle_detection_confidence_below_threshold",
            )

        lower_region = vehicle.bbox.lower_region(self.config.lower_region_fraction)
        lower_overlap_ratio = sampled_overlap_ratio(lower_region, space.polygon)
        bottom_center_inside = point_in_polygon(vehicle.bbox.bottom_center, space.polygon)
        occupancy_ratio = max(lower_overlap_ratio, 1.0 if bottom_center_inside else 0.0)

        space_center = polygon_centroid(space.polygon)
        distance_px = euclidean_distance(vehicle.bbox.bottom_center, space_center)
        distance_score = normalized_distance_score(distance_px, polygon_diagonal_length(space.polygon))
        detection_score = clamp(vehicle.detection_confidence)
        depth_score = self._depth_score(vehicle, space)

        bottom_center_score = 1.0 if bottom_center_inside else clamp(lower_overlap_ratio / max(1e-6, self.config.occupancy_ratio_threshold))
        score = (
            (occupancy_ratio * self.config.overlap_weight)
            + (bottom_center_score * self.config.bottom_center_weight)
            + (distance_score * self.config.distance_weight)
            + (detection_score * self.config.detection_weight)
        ) * depth_score

        rejection_reason = None
        if occupancy_ratio < self.config.occupancy_ratio_threshold and not bottom_center_inside:
            rejection_reason = "vehicle_does_not_occupy_space_ground_region"
        elif score < self.config.min_match_score:
            rejection_reason = "vehicle_space_score_below_threshold"

        return VehicleSpaceEvidence(
            parking_space_id=space.space_id,
            vehicle_id=vehicle.detection_id,
            vehicle_bbox_pixels=vehicle.bbox.to_list(),
            vehicle_center_pixel=vehicle.bbox.center.to_list(),
            vehicle_bottom_center_pixel=vehicle.bbox.bottom_center.to_list(),
            occupancy_ratio=occupancy_ratio,
            lower_overlap_ratio=lower_overlap_ratio,
            bottom_center_inside_space=bottom_center_inside,
            distance_to_space_center_px=distance_px,
            detection_confidence=vehicle.detection_confidence,
            depth_score=depth_score,
            score=score,
            rejection_reason=rejection_reason,
        )

    def match(
        self,
        vehicles: list[VehicleDetection],
        spaces: list[ParkingSpace],
    ) -> dict[str, tuple[VehicleDetection, VehicleSpaceEvidence] | None]:
        pairings: list[tuple[float, str, str, VehicleSpaceEvidence]] = []
        vehicle_index = {vehicle.detection_id: vehicle for vehicle in vehicles}

        for space in spaces:
            for vehicle in vehicles:
                evidence = self.score_vehicle_for_space(vehicle, space)
                if evidence.rejection_reason:
                    continue
                pairings.append((evidence.score, space.space_id, vehicle.detection_id, evidence))

        pairings.sort(key=lambda item: item[0], reverse=True)

        assignments: dict[str, tuple[VehicleDetection, VehicleSpaceEvidence] | None] = {
            space.space_id: None for space in spaces
        }
        used_vehicle_ids: set[str] = set()

        for _, space_id, vehicle_id, evidence in pairings:
            if assignments[space_id] is not None or vehicle_id in used_vehicle_ids:
                continue
            assignments[space_id] = (vehicle_index[vehicle_id], evidence)
            used_vehicle_ids.add(vehicle_id)

        return assignments
