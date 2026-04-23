from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend_state import BackendState


FAKE_SPACES = {
    "A1": {
        "latitude": 43.0,
        "longitude": -79.0,
        "polygon": [],
        "occupied": False,
        "status": "EMPTY",
        "vehicle_data": None,
        "decision_confidence": 0.9,
        "decision_reason": "no_valid_detection",
        "source_detection_time": None,
        "last_resolved_at": None,
    }
}


def _find_space(_lat, _lon, offset_meters=1):
    return "A1"


class ObservationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime_dir = Path(self.tmp.name)
        self.state = BackendState(
            dict(FAKE_SPACES),
            _find_space,
            runtime_dir=self.tmp.name,
            default_device_id="jetson-01",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_observation_skips_invalid_plate_reads(self):
        result = self.state.save_observation(
            "jetson-01",
            {
                "device_id": "jetson-01",
                "timestamp": "2026-04-23T15:00:00Z",
                "plate_detections": [
                    {
                        "plate_read": "???",
                        "time": "2026-04-23T15:00:00Z",
                        "location": {"lat": 43.0, "lon": -79.0},
                        "confidence_level": 0.99,
                    }
                ],
            },
        )

        self.assertIsNone(result)
        self.assertEqual(self.state.get_observations_for_device("jetson-01"), [])
        self.assertEqual(self.state._db.load_observations(), {})
        self.assertEqual(list((self.runtime_dir / "observations").rglob("*.json")), [])

    def test_save_observation_persists_normalized_valid_plate(self):
        result = self.state.save_observation(
            "jetson-01",
            {
                "device_id": "jetson-01",
                "timestamp": "2026-04-23T15:00:00Z",
                "plate_detections": [
                    {
                        "plate_read": "ab-c 123",
                        "time": "2026-04-23T15:00:00Z",
                        "location": {"lat": 43.0, "lon": -79.0},
                        "confidence_level": 0.94,
                    }
                ],
            },
        )

        self.assertIsNotNone(result)
        observations = self.state.get_observations_for_device("jetson-01")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["summary"]["plate_text"], "ABC123")

        detail = self.state.get_observation("jetson-01", observations[0]["id"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["observation"]["summary"]["plate_text"], "ABC123")
        self.assertEqual(detail["document"]["summary"]["plate_text"], "ABC123")

    def test_invalid_observations_are_purged_when_state_reloads(self):
        bad_id = "bad-observation-1"
        bad_path = self.runtime_dir / "observations" / "jetson-01" / "bad-observation.json"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "id": bad_id,
            "device_id": "jetson-01",
            "filename": bad_path.name,
            "path": str(bad_path),
            "created_at": "2026-04-23T15:00:00Z",
            "source": "jetson.telemetry",
            "summary": {
                "device_id": "jetson-01",
                "source": "jetson.telemetry",
                "timestamp": "2026-04-23T15:00:00Z",
                "plate_text": "???",
                "confidence": 0.9,
                "space_id": None,
                "space_status": None,
                "latitude": 43.0,
                "longitude": -79.0,
                "robot_status": "Patrol",
                "detection_count": 1,
                "parking_update_count": 0,
            },
        }
        document = {
            "id": bad_id,
            "device_id": "jetson-01",
            "source": "jetson.telemetry",
            "created_at": "2026-04-23T15:00:00Z",
            "summary": dict(record["summary"]),
            "payload": {"plate_detections": [{"plate_read": "???"}]},
        }
        bad_path.write_text(json.dumps(document), encoding="utf-8")
        self.state.observations[bad_id] = record
        self.state.devices["jetson-01"]["latest_observation_id"] = bad_id
        self.state.devices["jetson-01"]["recent_observation_ids"] = [bad_id]
        self.state._db.insert_observation(record)
        self.state._db_save_device_locked("jetson-01")

        reloaded = BackendState(
            dict(FAKE_SPACES),
            _find_space,
            runtime_dir=self.tmp.name,
            default_device_id="jetson-01",
        )

        self.assertEqual(reloaded.get_observations_for_device("jetson-01"), [])
        self.assertEqual(reloaded._db.load_observations(), {})
        self.assertFalse(bad_path.exists())
        self.assertIsNone(reloaded.devices["jetson-01"]["latest_observation_id"])
        self.assertEqual(reloaded.devices["jetson-01"]["recent_observation_ids"], [])


if __name__ == "__main__":
    unittest.main()
