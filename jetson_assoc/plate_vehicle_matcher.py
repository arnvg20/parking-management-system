from __future__ import annotations

from dataclasses import dataclass

from .geometry import clamp, euclidean_distance, normalized_axis_distance
from .models import PlateDetection, PlateVehicleEvidence, VehicleDetection


@dataclass(frozen=True)
class PlateVehicleMatcherConfig:
    min_detection_confidence: float = 0.20
    min_ocr_confidence: float = 0.15
    inside_margin_px: float = 18.0
    min_match_score: float = 0.38
    min_neighbor_margin: float = 0.08
    geometry_weight: float = 0.42
    ocr_weight: float = 0.34
    detection_weight: float = 0.24


class PlateVehicleMatcher:
    def __init__(self, config: PlateVehicleMatcherConfig | None = None) -> None:
        self.config = config or PlateVehicleMatcherConfig()

    def _geometry_score(self, plate: PlateDetection, vehicle: VehicleDetection) -> tuple[float, bool, float, float]:
        expanded_bbox = vehicle.bbox.expand(self.config.inside_margin_px)
        plate_center = plate.bbox.center
        inside_vehicle = expanded_bbox.contains_point(plate_center)

        offset_x, offset_y = normalized_axis_distance(plate_center, vehicle.bbox)
        # Plates are usually near the horizontal center and in the lower half of the vehicle.
        center_alignment = clamp(1.0 - (offset_x * 0.55))
        vertical_alignment = clamp(1.0 - max(0.0, offset_y - 0.25))
        base_geometry = (center_alignment * 0.55) + (vertical_alignment * 0.45)
        if inside_vehicle:
            base_geometry = clamp(base_geometry + 0.18)

        center_distance = euclidean_distance(plate_center, vehicle.bbox.center)
        bottom_distance = euclidean_distance(plate_center, vehicle.bbox.bottom_center)
        return (base_geometry, inside_vehicle, center_distance, bottom_distance)

    def _score_plate_for_vehicle(self, plate: PlateDetection, vehicle: VehicleDetection) -> tuple[float, bool, float, float, float]:
        if plate.detection_confidence < self.config.min_detection_confidence or plate.ocr_confidence < self.config.min_ocr_confidence:
            return (0.0, False, 0.0, 0.0, 0.0)

        geometry_score, inside_vehicle, center_distance, bottom_distance = self._geometry_score(plate, vehicle)
        score = (
            (geometry_score * self.config.geometry_weight)
            + (clamp(plate.ocr_confidence) * self.config.ocr_weight)
            + (clamp(plate.detection_confidence) * self.config.detection_weight)
        )
        return (score, inside_vehicle, center_distance, bottom_distance, geometry_score)

    def match_for_vehicle(
        self,
        vehicle: VehicleDetection,
        plates: list[PlateDetection],
        all_vehicles: list[VehicleDetection],
    ) -> PlateVehicleEvidence | None:
        best_evidence: PlateVehicleEvidence | None = None

        for plate in plates:
            selected_score, inside_vehicle, center_distance, bottom_distance, geometry_score = self._score_plate_for_vehicle(
                plate,
                vehicle,
            )
            if selected_score <= 0:
                continue

            competitor_score = 0.0
            for neighbor in all_vehicles:
                if neighbor.detection_id == vehicle.detection_id:
                    continue
                neighbor_score, _, _, _, _ = self._score_plate_for_vehicle(plate, neighbor)
                competitor_score = max(competitor_score, neighbor_score)

            neighbor_margin = selected_score - competitor_score
            rejection_reason = None
            if selected_score < self.config.min_match_score:
                rejection_reason = "plate_vehicle_score_below_threshold"
            elif neighbor_margin < self.config.min_neighbor_margin:
                rejection_reason = "plate_better_explained_by_neighboring_vehicle"
            elif not inside_vehicle:
                rejection_reason = "plate_center_not_within_or_near_vehicle_bbox"

            evidence = PlateVehicleEvidence(
                vehicle_id=vehicle.detection_id,
                plate_id=plate.detection_id,
                plate_text=plate.text,
                plate_bbox_pixels=plate.bbox.to_list(),
                plate_center_pixel=plate.bbox.center.to_list(),
                plate_inside_vehicle=inside_vehicle,
                plate_to_vehicle_distance_px=center_distance,
                plate_to_vehicle_bottom_distance_px=bottom_distance,
                neighbor_margin=neighbor_margin,
                ocr_confidence=plate.ocr_confidence,
                detection_confidence=plate.detection_confidence,
                geometric_score=geometry_score,
                score=selected_score,
                rejection_reason=rejection_reason,
                candidate_reads=plate.candidate_reads,
            )

            if evidence.rejection_reason:
                continue
            if best_evidence is None or evidence.score > best_evidence.score:
                best_evidence = evidence

        return best_evidence
