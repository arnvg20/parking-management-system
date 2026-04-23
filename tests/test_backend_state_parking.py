from __future__ import annotations

import tempfile
import unittest

from backend_state import BackendState


class BackendStateParkingTests(unittest.TestCase):
    def test_non_occupied_decision_does_not_clear_existing_space(self) -> None:
        parking_spaces = {
            "A1": {
                "latitude": 43.0,
                "longitude": -79.0,
                "occupied": False,
                "vehicle_data": None,
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state = BackendState(parking_spaces, lambda *_args, **_kwargs: "A1", runtime_dir=tmpdir)
            state.apply_space_decisions(
                "jetson-01",
                [
                    {
                        "space_id": "A1",
                        "status": "OCCUPIED",
                        "plate_read": "ABC1234",
                        "confidence": 0.91,
                        "source_detection_time": "2026-04-21T15:20:00Z",
                        "location": {"lat": 43.0, "lon": -79.0},
                    }
                ],
                telemetry={},
            )

            state.apply_space_decisions(
                "jetson-01",
                [
                    {
                        "space_id": "A1",
                        "status": "EMPTY",
                        "confidence": 0.90,
                        "source_detection_time": "2026-04-21T15:20:20Z",
                        "reason": "no_valid_detection",
                    }
                ],
                telemetry={},
            )

            self.assertTrue(state.parking_spaces["A1"]["occupied"])
            self.assertEqual(state.parking_spaces["A1"]["vehicle_data"]["license_plate"], "ABC1234")

    def test_legacy_update_without_explicit_space_id_is_ignored(self) -> None:
        parking_spaces = {
            "A1": {
                "latitude": 43.0,
                "longitude": -79.0,
                "occupied": False,
                "vehicle_data": None,
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state = BackendState(parking_spaces, lambda *_args, **_kwargs: "A1", runtime_dir=tmpdir)
            result = state.update_telemetry(
                "jetson-01",
                telemetry={},
                parking_updates=[
                    {
                        "occupied": True,
                        "license_plate": "ABC1234",
                        "latitude": 43.0,
                        "longitude": -79.0,
                        "confidence": 0.91,
                    }
                ],
            )

            self.assertEqual(result["updated_spaces"], [])
            self.assertFalse(state.parking_spaces["A1"]["occupied"])


if __name__ == "__main__":
    unittest.main()
