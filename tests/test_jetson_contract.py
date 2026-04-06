import copy
import json
import tempfile
import unittest
from pathlib import Path

from backend_state import BackendState
from gps_mapping import SegmentMapper
from jetson_contract import normalize_command_name, normalize_telemetry_payload


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "jetson_telemetry_new.json"


def build_test_spaces():
    spaces = {
        "A1": {
            "section_id": "A",
            "polygon": [],
            "latitude": 43.000000,
            "longitude": -79.000000,
            "occupied": False,
            "vehicle_data": None,
        },
        "A2": {
            "section_id": "A",
            "polygon": [],
            "latitude": 43.000080,
            "longitude": -79.000080,
            "occupied": False,
            "vehicle_data": None,
        },
    }

    def matcher(latitude, longitude, offset_meters=1):
        for space_id, values in spaces.items():
            if abs(latitude - values["latitude"]) < 1e-8 and abs(longitude - values["longitude"]) < 1e-8:
                return space_id
        return None

    return spaces, matcher


def build_distance_matcher(spaces):
    def matcher(latitude, longitude, offset_meters=1):
        for space_id, values in spaces.items():
            lat_distance = (latitude - values["latitude"]) * 111320
            lon_distance = (longitude - values["longitude"]) * 111320
            if ((lat_distance ** 2) + (lon_distance ** 2)) ** 0.5 <= offset_meters:
                return space_id
        return None

    return matcher


class JetsonContractTests(unittest.TestCase):
    def test_normalize_telemetry_payload_derives_latest_detection(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

        normalized = normalize_telemetry_payload(payload)

        self.assertEqual(normalized["device_id"], "jetson-01")
        self.assertEqual(normalized["robot_status"], "Patrol")
        self.assertEqual(len(normalized["plate_detections"]), 2)
        self.assertEqual(normalized["latest_detection"]["event_id"], "ocr:cam0_plate6:1774975961478")
        self.assertNotIn("space_id", normalized["plate_detections"][0])

    def test_update_telemetry_resolves_spaces_from_detection_gps(self):
        spaces, matcher = build_test_spaces()
        with tempfile.TemporaryDirectory() as temp_dir:
            state = BackendState(
                copy.deepcopy(spaces),
                matcher,
                runtime_dir=temp_dir,
                default_device_id="jetson-01",
            )

            telemetry = normalize_telemetry_payload(
                {
                    "device_id": "jetson-01",
                    "camera_on": True,
                    "stream_enabled": True,
                    "robot_status": "Patrol",
                    "timestamp": "2026-03-31T18:14:44.537Z",
                    "plate_detections": [
                        {
                            "event_id": "evt-a1",
                            "plate_text": "AAA-111",
                            "detected_at": "2026-03-31T18:14:40.000Z",
                            "source_camera": "/dev/video0",
                            "gps": {"lat": 43.000000, "lon": -79.000000},
                        },
                        {
                            "event_id": "evt-a2",
                            "plate_text": "BBB-222",
                            "detected_at": "2026-03-31T18:14:44.000Z",
                            "source_camera": "/dev/video0",
                            "gps": {"lat": 43.000080, "lon": -79.000080},
                        },
                    ],
                }
            )

            result = state.update_telemetry("jetson-01", telemetry)
            snapshot = state.get_system_snapshot()

            self.assertEqual(result["device"]["status"], "Patrol")
            self.assertEqual(result["device"]["latest_detection"]["event_id"], "evt-a2")
            self.assertEqual(
                result["device"]["last_telemetry"]["plate_detections"][0]["resolved_space_id"],
                "A1",
            )
            self.assertEqual(
                result["device"]["last_telemetry"]["plate_detections"][1]["resolved_space_id"],
                "A2",
            )
            self.assertEqual(snapshot["parking_spaces"]["A1"]["vehicle_data"]["license_plate"], "AAA-111")
            self.assertEqual(snapshot["parking_spaces"]["A2"]["vehicle_data"]["license_plate"], "BBB-222")

    def test_newer_detection_moves_same_plate_to_new_space(self):
        spaces, matcher = build_test_spaces()
        with tempfile.TemporaryDirectory() as temp_dir:
            state = BackendState(
                copy.deepcopy(spaces),
                matcher,
                runtime_dir=temp_dir,
                default_device_id="jetson-01",
            )

            first_payload = normalize_telemetry_payload(
                {
                    "device_id": "jetson-01",
                    "robot_status": "Patrol",
                    "timestamp": "2026-03-31T18:14:00.000Z",
                    "plate_detections": [
                        {
                            "event_id": "evt-old",
                            "plate_text": "MOVE-001",
                            "detected_at": "2026-03-31T18:14:00.000Z",
                            "gps": {"lat": 43.000000, "lon": -79.000000},
                        }
                    ],
                }
            )
            second_payload = normalize_telemetry_payload(
                {
                    "device_id": "jetson-01",
                    "robot_status": "Patrol",
                    "timestamp": "2026-03-31T18:15:00.000Z",
                    "plate_detections": [
                        {
                            "event_id": "evt-new",
                            "plate_text": "MOVE-001",
                            "detected_at": "2026-03-31T18:15:00.000Z",
                            "gps": {"lat": 43.000080, "lon": -79.000080},
                        }
                    ],
                }
            )

            state.update_telemetry("jetson-01", first_payload)
            result = state.update_telemetry("jetson-01", second_payload)
            snapshot = state.get_system_snapshot()

            self.assertIn("A1", result["updated_spaces"])
            self.assertIn("A2", result["updated_spaces"])
            self.assertFalse(snapshot["parking_spaces"]["A1"]["occupied"])
            self.assertEqual(snapshot["parking_spaces"]["A2"]["vehicle_data"]["license_plate"], "MOVE-001")

    def test_update_telemetry_uses_mapped_gps_for_space_resolution(self):
        gps_route = [
            (43.000000, -79.000000),
            (43.000000, -79.000100),
        ]
        ref_route = [
            (43.000100, -79.000000),
            (43.000100, -79.000100),
        ]
        mapper = SegmentMapper(gps_route, ref_route)
        spaces = {
            "A1": {
                "section_id": "A",
                "polygon": [],
                "latitude": 43.000100,
                "longitude": -79.000050,
                "occupied": False,
                "vehicle_data": None,
            }
        }
        matcher = build_distance_matcher(spaces)

        with tempfile.TemporaryDirectory() as temp_dir:
            state = BackendState(
                copy.deepcopy(spaces),
                matcher,
                runtime_dir=temp_dir,
                default_device_id="jetson-01",
                route_mapper=mapper,
            )

            telemetry = normalize_telemetry_payload(
                {
                    "device_id": "jetson-01",
                    "robot_status": "Patrol",
                    "timestamp": "2026-03-31T18:14:44.537Z",
                    "plate_detections": [
                        {
                            "event_id": "evt-mapped",
                            "plate_text": "MAP-101",
                            "detected_at": "2026-03-31T18:14:44.000Z",
                            "gps": {"lat": 43.000000, "lon": -79.000050},
                        }
                    ],
                }
            )

            result = state.update_telemetry("jetson-01", telemetry)
            detection = result["device"]["last_telemetry"]["plate_detections"][0]
            vehicle = state.get_system_snapshot()["parking_spaces"]["A1"]["vehicle_data"]

            self.assertEqual(detection["resolved_space_id"], "A1")
            self.assertEqual(detection["gps"]["lat"], 43.000000)
            self.assertAlmostEqual(detection["mapped_gps"]["lat"], 43.000100, places=6)
            self.assertAlmostEqual(vehicle["latitude"], 43.000100, places=6)
            self.assertEqual(vehicle["gps"]["lat"], 43.000000)
            self.assertAlmostEqual(vehicle["mapped_gps"]["lat"], 43.000100, places=6)

    def test_bbox_area_priority_prefers_larger_detection_with_same_candidates(self):
        spaces = {
            "A1": {
                "section_id": "A",
                "polygon": [],
                "latitude": 43.000000,
                "longitude": -79.000000,
                "occupied": False,
                "vehicle_data": None,
            },
            "A2": {
                "section_id": "A",
                "polygon": [],
                "latitude": 43.000000,
                "longitude": -79.000100,
                "occupied": False,
                "vehicle_data": None,
            },
        }
        matcher = build_distance_matcher(spaces)

        with tempfile.TemporaryDirectory() as temp_dir:
            state = BackendState(
                copy.deepcopy(spaces),
                matcher,
                runtime_dir=temp_dir,
                default_device_id="jetson-01",
                gps_route_calibration_enabled=False,
            )

            telemetry = normalize_telemetry_payload(
                {
                    "device_id": "jetson-01",
                    "robot_status": "Patrol",
                    "timestamp": "2026-03-31T18:14:44.537Z",
                    "plate_detections": [
                        {
                            "event_id": "evt-small",
                            "plate_text": "SMALL-1",
                            "detected_at": "2026-03-31T18:14:44.000Z",
                            "image_id": "frame-1",
                            "bbox_xyxy": {"x1": 0, "y1": 0, "x2": 100, "y2": 50},
                            "gps": {"lat": 43.000000, "lon": -79.000050},
                        },
                        {
                            "event_id": "evt-large",
                            "plate_text": "LARGE-1",
                            "detected_at": "2026-03-31T18:14:44.000Z",
                            "image_id": "frame-1",
                            "bbox_xyxy": {"x1": 0, "y1": 0, "x2": 200, "y2": 120},
                            "gps": {"lat": 43.000000, "lon": -79.000050},
                        },
                    ],
                }
            )

            state.update_telemetry("jetson-01", telemetry)
            snapshot = state.get_system_snapshot()

            self.assertEqual(snapshot["parking_spaces"]["A1"]["vehicle_data"]["license_plate"], "LARGE-1")
            self.assertEqual(snapshot["parking_spaces"]["A2"]["vehicle_data"]["license_plate"], "SMALL-1")

    def test_bbox_area_priority_treats_similar_areas_as_a_tie(self):
        spaces = {
            "A1": {
                "section_id": "A",
                "polygon": [],
                "latitude": 43.000000,
                "longitude": -79.000000,
                "occupied": False,
                "vehicle_data": None,
            },
            "A2": {
                "section_id": "A",
                "polygon": [],
                "latitude": 43.000000,
                "longitude": -79.000100,
                "occupied": False,
                "vehicle_data": None,
            },
        }
        matcher = build_distance_matcher(spaces)

        with tempfile.TemporaryDirectory() as temp_dir:
            state = BackendState(
                copy.deepcopy(spaces),
                matcher,
                runtime_dir=temp_dir,
                default_device_id="jetson-01",
                gps_route_calibration_enabled=False,
                bbox_area_similarity_ratio=0.1,
            )

            telemetry = normalize_telemetry_payload(
                {
                    "device_id": "jetson-01",
                    "robot_status": "Patrol",
                    "timestamp": "2026-03-31T18:14:44.537Z",
                    "plate_detections": [
                        {
                            "event_id": "evt-first",
                            "plate_text": "FIRST-1",
                            "detected_at": "2026-03-31T18:14:44.000Z",
                            "image_id": "frame-2",
                            "bbox_xyxy": {"x1": 0, "y1": 0, "x2": 102, "y2": 100},
                            "gps": {"lat": 43.000000, "lon": -79.000050},
                        },
                        {
                            "event_id": "evt-second",
                            "plate_text": "SECOND-1",
                            "detected_at": "2026-03-31T18:14:44.000Z",
                            "image_id": "frame-2",
                            "bbox_xyxy": {"x1": 0, "y1": 0, "x2": 100, "y2": 100},
                            "gps": {"lat": 43.000000, "lon": -79.000050},
                        },
                    ],
                }
            )

            state.update_telemetry("jetson-01", telemetry)
            snapshot = state.get_system_snapshot()

            self.assertEqual(snapshot["parking_spaces"]["A1"]["vehicle_data"]["license_plate"], "FIRST-1")
            self.assertEqual(snapshot["parking_spaces"]["A2"]["vehicle_data"]["license_plate"], "SECOND-1")

    def test_command_aliases_map_to_current_contract(self):
        self.assertEqual(normalize_command_name("cmd_patrol"), "cmd_patrol")
        self.assertEqual(normalize_command_name("camera_on"), "cmd_patrol")
        self.assertEqual(normalize_command_name("cmd_post_patrol"), "cmd_standby")
        self.assertIsNone(normalize_command_name("capture_image"))


if __name__ == "__main__":
    unittest.main()
