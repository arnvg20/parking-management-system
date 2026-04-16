from __future__ import annotations

from .decision_engine import ParkingAssociationDecisionEngine
from .models import (
    AssociationFrameInput,
    BBox,
    FrameContext,
    Location,
    ParkingSpace,
    PlateDetection,
    Point2D,
    VehicleDetection,
)


def build_example_frame_input() -> AssociationFrameInput:
    spaces = [
        ParkingSpace(
            space_id="A12",
            polygon=(
                Point2D(110, 280),
                Point2D(260, 260),
                Point2D(285, 470),
                Point2D(95, 495),
            ),
            location=Location(lat=43.123456, lon=-79.123456),
            metadata={
                "expected_bottom_y": 470,
                "expected_bottom_y_tolerance": 120,
                "expected_vehicle_height_px": (90, 260),
            },
        ),
    ]

    vehicles = [
        VehicleDetection(
            detection_id="vehicle-17",
            bbox=BBox(125, 210, 268, 460),
            detection_confidence=0.97,
            track_id="track-17",
        ),
    ]

    plates = [
        PlateDetection(
            detection_id="plate-17",
            text="ABC1234",
            bbox=BBox(160, 340, 232, 372),
            ocr_confidence=0.91,
            detection_confidence=0.95,
            track_id="track-17",
            candidate_reads=(
                {"text": "ABC1234", "confidence": 0.91},
                {"text": "ABC1284", "confidence": 0.61},
            ),
        ),
    ]

    return AssociationFrameInput(
        spaces=spaces,
        vehicles=vehicles,
        plates=plates,
        context=FrameContext(
            frame_id="frame-000123",
            timestamp="2026-04-16T15:32:10Z",
            gps_location=Location(lat=43.123456, lon=-79.123456),
        ),
    )


def build_example_result() -> dict[str, object]:
    engine = ParkingAssociationDecisionEngine()
    result = engine.process_frame(build_example_frame_input())
    return result.to_dict()
