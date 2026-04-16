from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from live_site.space_assignment import LotSpaceAssociationConfig, LotSpaceAssociationService


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "jetson_plate_telemetry.json"

TEST_SPACES = {
    "A1": {
        "latitude": 43.000000,
        "longitude": -79.000120,
        "polygon": [
            {"latitude": 43.000060, "longitude": -79.000200},
            {"latitude": 43.000060, "longitude": -79.000040},
            {"latitude": 42.999940, "longitude": -79.000040},
            {"latitude": 42.999940, "longitude": -79.000200},
        ],
    },
    "A2": {
        "latitude": 43.000000,
        "longitude": -78.999880,
        "polygon": [
            {"latitude": 43.000060, "longitude": -78.999960},
            {"latitude": 43.000060, "longitude": -78.999800},
            {"latitude": 42.999940, "longitude": -78.999800},
            {"latitude": 42.999940, "longitude": -78.999960},
        ],
    },
}

TEST_CONFIG = LotSpaceAssociationConfig(
    outside_space_max_distance_m=20.0,
    ambiguous_score_margin=0.10,
    ambiguous_distance_margin_m=1.0,
    empty_after_seconds=10,
    history_window_seconds=90,
    min_confirmations_for_occupied=2,
    min_vote_share=0.55,
    min_stable_confidence=0.65,
    empty_confidence_floor=0.90,
)


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def decision_by_space(result, space_id: str):
    return next(decision for decision in result.space_decisions if decision.space_id == space_id)


class LotSpaceAssociationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = LotSpaceAssociationService(copy.deepcopy(TEST_SPACES), config=TEST_CONFIG)

    def test_repeated_consistent_detection_promotes_space_to_occupied(self) -> None:
        first_payload = load_fixture()
        second_payload = load_fixture()
        first_payload["timestamp"] = "2026-04-16T15:32:10Z"
        first_payload["plate_detections"][0]["time"] = "2026-04-16T15:32:10Z"
        second_payload["timestamp"] = "2026-04-16T15:32:14Z"
        second_payload["plate_detections"][0]["time"] = "2026-04-16T15:32:14Z"

        first_result = self.service.ingest("jetson-01", first_payload)
        second_result = self.service.ingest("jetson-01", second_payload)

        self.assertEqual(decision_by_space(first_result, "A1").status, "UNCERTAIN")
        self.assertEqual(decision_by_space(second_result, "A1").status, "OCCUPIED")
        self.assertEqual(decision_by_space(second_result, "A1").plate_read, "ABC1234")
        self.assertEqual(decision_by_space(second_result, "A2").status, "EMPTY")
        self.assertEqual(second_result.detection_results[0].assigned_space_id, "A1")

    def test_ambiguous_detection_marks_nearby_spaces_uncertain(self) -> None:
        payload = load_fixture()
        payload["timestamp"] = "2026-04-16T15:40:10Z"
        payload["plate_detections"][0]["time"] = "2026-04-16T15:40:10Z"
        payload["plate_detections"][0]["location"] = {
            "lat": 43.000000,
            "lon": -79.000000,
        }

        result = self.service.ingest("jetson-01", payload)

        self.assertEqual(result.detection_results[0].status, "UNCERTAIN")
        self.assertEqual(decision_by_space(result, "A1").status, "UNCERTAIN")
        self.assertEqual(decision_by_space(result, "A2").status, "UNCERTAIN")

    def test_far_detection_is_rejected_and_spaces_stay_empty(self) -> None:
        payload = load_fixture()
        payload["timestamp"] = "2026-04-16T15:45:10Z"
        payload["plate_detections"][0]["time"] = "2026-04-16T15:45:10Z"
        payload["plate_detections"][0]["location"] = {
            "lat": 43.002500,
            "lon": -79.002500,
        }

        result = self.service.ingest("jetson-01", payload)

        self.assertEqual(result.detection_results[0].status, "REJECTED")
        self.assertTrue(all(decision.status == "EMPTY" for decision in result.space_decisions))

    def test_empty_detection_feed_expires_previous_occupied_state(self) -> None:
        warm_one = load_fixture()
        warm_two = load_fixture()
        warm_one["timestamp"] = "2026-04-16T15:50:10Z"
        warm_one["plate_detections"][0]["time"] = "2026-04-16T15:50:10Z"
        warm_two["timestamp"] = "2026-04-16T15:50:13Z"
        warm_two["plate_detections"][0]["time"] = "2026-04-16T15:50:13Z"

        self.service.ingest("jetson-01", warm_one)
        occupied_result = self.service.ingest("jetson-01", warm_two)
        self.assertEqual(decision_by_space(occupied_result, "A1").status, "OCCUPIED")

        empty_payload = {
            "device_id": "jetson-01",
            "timestamp": "2026-04-16T15:50:29Z",
            "plate_detections": [],
        }
        empty_result = self.service.ingest("jetson-01", empty_payload)

        self.assertEqual(decision_by_space(empty_result, "A1").status, "EMPTY")
        self.assertEqual(decision_by_space(empty_result, "A1").reason, "no_valid_detection")


if __name__ == "__main__":
    unittest.main()
