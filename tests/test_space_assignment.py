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


def detection_result_by_id(result, detection_id: str):
    return next(item for item in result.detection_results if item.detection_id == detection_id)


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
        self.assertEqual(decision_by_space(second_result, "A1").plate_read, "ABCD123")
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

    def test_robot_gps_can_match_nearest_space_inside_expanded_radius(self) -> None:
        config = LotSpaceAssociationConfig(
            outside_space_max_distance_m=18.0,
            ambiguous_score_margin=0.01,
            ambiguous_distance_margin_m=0.1,
            empty_after_seconds=TEST_CONFIG.empty_after_seconds,
            history_window_seconds=TEST_CONFIG.history_window_seconds,
            min_confirmations_for_occupied=TEST_CONFIG.min_confirmations_for_occupied,
            min_vote_share=TEST_CONFIG.min_vote_share,
            min_stable_confidence=TEST_CONFIG.min_stable_confidence,
            empty_confidence_floor=TEST_CONFIG.empty_confidence_floor,
        )
        service = LotSpaceAssociationService(copy.deepcopy(TEST_SPACES), config=config)
        payload = load_fixture()
        payload["timestamp"] = "2026-04-16T15:47:10Z"
        payload["plate_detections"][0]["time"] = "2026-04-16T15:47:10Z"
        payload["plate_detections"][0]["location"] = {
            "lat": 43.000150,
            "lon": -79.000120,
        }

        result = service.ingest("jetson-01", payload)

        self.assertEqual(result.detection_results[0].status, "ASSIGNED")
        self.assertEqual(result.detection_results[0].assigned_space_id, "A1")

    def test_confirmed_space_decision_carries_detection_image_id(self) -> None:
        first_payload = load_fixture()
        second_payload = load_fixture()
        first_payload["timestamp"] = "2026-04-16T15:48:10Z"
        first_payload["plate_detections"][0]["time"] = "2026-04-16T15:48:10Z"
        first_payload["plate_detections"][0]["image_id"] = "plate-upload-1"
        second_payload["timestamp"] = "2026-04-16T15:48:14Z"
        second_payload["plate_detections"][0]["time"] = "2026-04-16T15:48:14Z"
        second_payload["plate_detections"][0]["image_id"] = "plate-upload-2"

        self.service.ingest("jetson-01", first_payload)
        result = self.service.ingest("jetson-01", second_payload)
        decision = decision_by_space(result, "A1")

        self.assertEqual(decision.status, "OCCUPIED")
        self.assertEqual(decision.image_id, "plate-upload-2")
        self.assertEqual(decision.to_dict()["image_id"], "plate-upload-2")

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

        self.assertEqual(decision_by_space(empty_result, "A1").status, "OCCUPIED")
        self.assertEqual(decision_by_space(empty_result, "A1").plate_read, "ABCD123")

    def test_new_plate_replaces_existing_occupied_plate_without_manual_clear(self) -> None:
        warm_one = load_fixture()
        warm_two = load_fixture()
        warm_one["timestamp"] = "2026-04-16T15:52:10Z"
        warm_one["plate_detections"][0]["time"] = "2026-04-16T15:52:10Z"
        warm_two["timestamp"] = "2026-04-16T15:52:14Z"
        warm_two["plate_detections"][0]["time"] = "2026-04-16T15:52:14Z"

        self.service.ingest("jetson-01", warm_one)
        occupied_result = self.service.ingest("jetson-01", warm_two)
        self.assertEqual(decision_by_space(occupied_result, "A1").status, "OCCUPIED")
        self.assertEqual(decision_by_space(occupied_result, "A1").plate_read, "ABCD123")

        replacement_payload = load_fixture()
        replacement_payload["timestamp"] = "2026-04-16T15:52:20Z"
        replacement_payload["plate_detections"][0]["time"] = "2026-04-16T15:52:20Z"
        replacement_payload["plate_detections"][0]["plate_read"] = "ZZZZ999"

        replacement_result = self.service.ingest("jetson-01", replacement_payload)

        self.assertEqual(decision_by_space(replacement_result, "A1").status, "OCCUPIED")
        self.assertEqual(decision_by_space(replacement_result, "A1").plate_read, "ZZZZ999")

    def test_bbox_prefilter_keeps_only_largest_detection_in_camera_window(self) -> None:
        payload = {
            "device_id": "jetson-01",
            "timestamp": "2026-04-16T16:00:10Z",
            "plate_detections": [
                {
                    "event_id": "det-large",
                    "plate_text": "BIGG100",
                    "detected_at": "2026-04-16T16:00:10.100Z",
                    "confidence": 0.90,
                    "bbox_xyxy": [10, 20, 90, 180],
                    "gps": {"lat": 43.000000, "lon": -79.000120},
                    "source_camera": "front-narrow",
                },
                {
                    "event_id": "det-small",
                    "plate_text": "SMLL009",
                    "detected_at": "2026-04-16T16:00:10.700Z",
                    "confidence": 0.97,
                    "bbox_xyxy": [12, 22, 52, 102],
                    "gps": {"lat": 43.000000, "lon": -79.000121},
                    "source_camera": "front-narrow",
                },
            ],
        }

        result = self.service.ingest("jetson-01", payload)
        large_result = detection_result_by_id(result, "det-large")
        small_result = detection_result_by_id(result, "det-small")

        self.assertEqual(large_result.status, "ASSIGNED")
        self.assertEqual(large_result.assigned_space_id, "A1")
        self.assertTrue(large_result.bbox_filter_kept)
        self.assertEqual(large_result.bbox_filter_reason, "largest_bbox_in_window")
        self.assertEqual(large_result.bbox_filter_rank, 1)
        self.assertEqual(large_result.bbox_height_px, 160.0)

        self.assertEqual(small_result.status, "FILTERED")
        self.assertFalse(small_result.bbox_filter_kept)
        self.assertEqual(small_result.bbox_filter_reason, "bbox_rank_exceeds_top_k")
        self.assertEqual(small_result.bbox_filter_rank, 2)
        self.assertIn("front-narrow:", small_result.bbox_window_key or "")

    def test_missing_bbox_preserves_gps_only_fallback(self) -> None:
        payload = {
            "device_id": "jetson-01",
            "timestamp": "2026-04-16T16:05:10Z",
            "plate_detections": [
                {
                    "event_id": "gps-only",
                    "plate_text": "GPSA123",
                    "detected_at": "2026-04-16T16:05:10Z",
                    "confidence": 0.88,
                    "gps": {"lat": 43.000000, "lon": -79.000120},
                    "source_camera": "front-wide",
                }
            ],
        }

        result = self.service.ingest("jetson-01", payload)
        detection_result = detection_result_by_id(result, "gps-only")

        self.assertEqual(detection_result.status, "ASSIGNED")
        self.assertEqual(detection_result.assigned_space_id, "A1")
        self.assertTrue(detection_result.bbox_filter_kept)
        self.assertEqual(detection_result.bbox_filter_reason, "bbox_missing_fallback")
        self.assertIsNone(detection_result.bbox_height_px)
        self.assertEqual(decision_by_space(result, "A1").status, "UNCERTAIN")

    def test_missing_gps_detection_does_not_block_smaller_valid_bbox_detection(self) -> None:
        payload = {
            "device_id": "jetson-01",
            "timestamp": "2026-04-16T16:10:10Z",
            "plate_detections": [
                {
                    "event_id": "no-gps-large",
                    "plate_text": "NOGP001",
                    "detected_at": "2026-04-16T16:10:10.100Z",
                    "confidence": 0.95,
                    "bbox_xyxy": [20, 30, 120, 230],
                    "source_camera": "front-narrow",
                },
                {
                    "event_id": "with-gps-small",
                    "plate_text": "OKGP002",
                    "detected_at": "2026-04-16T16:10:10.400Z",
                    "confidence": 0.91,
                    "bbox_xyxy": [16, 26, 66, 126],
                    "gps": {"lat": 43.000000, "lon": -79.000120},
                    "source_camera": "front-narrow",
                },
            ],
        }

        result = self.service.ingest("jetson-01", payload)
        missing_gps_result = detection_result_by_id(result, "no-gps-large")
        valid_result = detection_result_by_id(result, "with-gps-small")

        self.assertEqual(missing_gps_result.status, "REJECTED")
        self.assertFalse(missing_gps_result.bbox_filter_kept)
        self.assertEqual(missing_gps_result.reason, "missing_gps_location")

        self.assertEqual(valid_result.status, "ASSIGNED")
        self.assertEqual(valid_result.assigned_space_id, "A1")
        self.assertTrue(valid_result.bbox_filter_kept)
        self.assertEqual(valid_result.bbox_filter_reason, "single_bbox_candidate")

    def test_second_detection_can_survive_when_top_k_and_relative_height_allow_it(self) -> None:
        config = LotSpaceAssociationConfig(
            outside_space_max_distance_m=TEST_CONFIG.outside_space_max_distance_m,
            ambiguous_score_margin=TEST_CONFIG.ambiguous_score_margin,
            ambiguous_distance_margin_m=TEST_CONFIG.ambiguous_distance_margin_m,
            empty_after_seconds=TEST_CONFIG.empty_after_seconds,
            history_window_seconds=TEST_CONFIG.history_window_seconds,
            min_confirmations_for_occupied=TEST_CONFIG.min_confirmations_for_occupied,
            min_vote_share=TEST_CONFIG.min_vote_share,
            min_stable_confidence=TEST_CONFIG.min_stable_confidence,
            empty_confidence_floor=TEST_CONFIG.empty_confidence_floor,
            bbox_top_k_per_window=2,
            bbox_min_relative_height_ratio=0.80,
        )
        service = LotSpaceAssociationService(copy.deepcopy(TEST_SPACES), config=config)
        payload = {
            "device_id": "jetson-01",
            "timestamp": "2026-04-16T16:15:10Z",
            "plate_detections": [
                {
                    "event_id": "rank-one",
                    "plate_text": "TOPA111",
                    "detected_at": "2026-04-16T16:15:10.100Z",
                    "confidence": 0.92,
                    "bbox_xyxy": [10, 20, 90, 180],
                    "gps": {"lat": 43.000000, "lon": -79.000120},
                    "source_camera": "front-wide",
                },
                {
                    "event_id": "rank-two",
                    "plate_text": "TOPB222",
                    "detected_at": "2026-04-16T16:15:10.500Z",
                    "confidence": 0.89,
                    "bbox_xyxy": [15, 22, 83, 154],
                    "gps": {"lat": 43.000000, "lon": -78.999880},
                    "source_camera": "front-wide",
                },
            ],
        }

        result = service.ingest("jetson-01", payload)
        first_result = detection_result_by_id(result, "rank-one")
        second_result = detection_result_by_id(result, "rank-two")

        self.assertEqual(first_result.status, "ASSIGNED")
        self.assertEqual(second_result.status, "ASSIGNED")
        self.assertEqual(second_result.assigned_space_id, "A2")
        self.assertTrue(second_result.bbox_filter_kept)
        self.assertEqual(second_result.bbox_filter_reason, "bbox_within_relative_height_ratio")
        self.assertEqual(second_result.bbox_filter_rank, 2)


if __name__ == "__main__":
    unittest.main()
