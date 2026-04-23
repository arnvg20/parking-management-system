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
                        "plate_read": "ABCD123",
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
            self.assertEqual(state.parking_spaces["A1"]["vehicle_data"]["license_plate"], "ABCD123")

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
                        "license_plate": "ABCD123",
                        "latitude": 43.0,
                        "longitude": -79.0,
                        "confidence": 0.91,
                    }
                ],
            )

            self.assertEqual(result["updated_spaces"], [])
            self.assertFalse(state.parking_spaces["A1"]["occupied"])

    def test_manual_clear_blocks_stale_reoccupancy(self) -> None:
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
                        "plate_read": "ABCD123",
                        "confidence": 0.91,
                        "source_detection_time": "2026-04-21T15:20:00Z",
                        "location": {"lat": 43.0, "lon": -79.0},
                    }
                ],
                telemetry={},
            )
            state.apply_manual_parking_update(
                {"space_id": "A1", "occupied": False, "captured_at": "2026-04-21T15:21:00Z"}
            )

            state.apply_space_decisions(
                "jetson-01",
                [
                    {
                        "space_id": "A1",
                        "status": "OCCUPIED",
                        "plate_read": "ZZZZ999",
                        "confidence": 0.96,
                        "source_detection_time": "2026-04-21T15:20:05Z",
                        "location": {"lat": 43.0, "lon": -79.0},
                    }
                ],
                telemetry={},
            )

            self.assertFalse(state.parking_spaces["A1"]["occupied"])
            self.assertIsNone(state.parking_spaces["A1"]["vehicle_data"])

            state.apply_space_decisions(
                "jetson-01",
                [
                    {
                        "space_id": "A1",
                        "status": "OCCUPIED",
                        "plate_read": "ZZZZ999",
                        "confidence": 0.96,
                        "source_detection_time": "2026-04-21T15:21:05Z",
                        "location": {"lat": 43.0, "lon": -79.0},
                    }
                ],
                telemetry={},
            )

            self.assertTrue(state.parking_spaces["A1"]["occupied"])
            self.assertEqual(state.parking_spaces["A1"]["vehicle_data"]["license_plate"], "ZZZZ999")

    def test_space_decision_without_image_does_not_fallback_to_latest_upload(self) -> None:
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
            state.save_image("jetson-01", "latest.jpg", b"jpeg-bytes")
            state.apply_space_decisions(
                "jetson-01",
                [
                    {
                        "space_id": "A1",
                        "status": "OCCUPIED",
                        "plate_read": "ABCD123",
                        "confidence": 0.91,
                        "source_detection_time": "2026-04-21T15:20:00Z",
                        "location": {"lat": 43.0, "lon": -79.0},
                    }
                ],
                telemetry={},
            )

            vehicle_data = state.parking_spaces["A1"]["vehicle_data"]
            self.assertIsNone(vehicle_data["image_id"])
            self.assertIsNone(vehicle_data["image_url"])


if __name__ == "__main__":
    unittest.main()
