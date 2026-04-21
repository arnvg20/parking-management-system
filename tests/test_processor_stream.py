from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend_state import BackendState


FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"

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


def _find_space(lat, lon, offset_meters=1):
    return None


class ProcessorStreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = BackendState(
            dict(FAKE_SPACES),
            _find_space,
            runtime_dir=self.tmp.name,
            default_device_id="jetson-01",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_source_frame_creates_source_entry(self):
        result = self.state.save_source_frame(
            "jetson-01", "processor", {"camera_role": "processor", "label": "CV Feed"}, FAKE_JPEG
        )

        self.assertEqual(result["device_id"], "jetson-01")
        self.assertEqual(result["source_id"], "processor")
        self.assertEqual(result["frame_version"], 1)
        self.assertIn("/api/devices/jetson-01/sources/processor/snapshot", result["snapshot_url"])
        self.assertIn("/api/devices/jetson-01/sources/processor/stream.mjpeg", result["mjpeg_url"])

    def test_get_source_frame_bytes_returns_stored_bytes(self):
        self.state.save_source_frame("jetson-01", "processor", {}, FAKE_JPEG)
        result = self.state.get_source_frame_bytes("jetson-01", "processor")

        self.assertIsNotNone(result)
        self.assertEqual(result["frame_bytes"], FAKE_JPEG)
        self.assertEqual(result["content_type"], "image/jpeg")

    def test_get_source_frame_bytes_unknown_source_returns_none(self):
        result = self.state.get_source_frame_bytes("jetson-01", "nonexistent")
        self.assertIsNone(result)

    def test_wait_for_next_source_frame_returns_current_frame(self):
        self.state.save_source_frame("jetson-01", "processor", {}, FAKE_JPEG)
        result = self.state.wait_for_next_source_frame("jetson-01", "processor", last_version=0, timeout=1)

        self.assertIsNotNone(result)
        self.assertEqual(result["frame_bytes"], FAKE_JPEG)
        self.assertEqual(result["frame_version"], 1)

    def test_wait_for_next_source_frame_already_seen_version_times_out(self):
        self.state.save_source_frame("jetson-01", "processor", {}, FAKE_JPEG)
        result = self.state.wait_for_next_source_frame("jetson-01", "processor", last_version=1, timeout=0)

        self.assertIsNone(result)

    def test_device_snapshot_includes_source_with_urls(self):
        self.state.save_source_frame("jetson-01", "processor", {"camera_role": "processor"}, FAKE_JPEG)
        device = self.state.get_device("jetson-01")

        self.assertIn("latest_stream_by_source", device)
        sources = device["latest_stream_by_source"]
        self.assertIn("processor", sources)
        source = sources["processor"]
        self.assertEqual(source["camera_role"], "processor")
        self.assertIn("snapshot_url", source)
        self.assertIn("mjpeg_url", source)
        self.assertNotIn("_frame_bytes", source)

    def test_device_snapshot_excludes_raw_bytes(self):
        self.state.save_source_frame("jetson-01", "processor", {}, FAKE_JPEG)
        device = self.state.get_device("jetson-01")
        source = device["latest_stream_by_source"]["processor"]
        self.assertNotIn("_frame_bytes", source)

    def test_frame_version_increments_on_repeated_saves(self):
        self.state.save_source_frame("jetson-01", "processor", {}, FAKE_JPEG)
        result2 = self.state.save_source_frame("jetson-01", "processor", {}, FAKE_JPEG)
        self.assertEqual(result2["frame_version"], 2)

    def test_multiple_sources_are_independent(self):
        jpeg2 = b"\xff\xd8\xff\xe0" + b"\x01" * 16 + b"\xff\xd9"
        self.state.save_source_frame("jetson-01", "processor", {"camera_role": "processor"}, FAKE_JPEG)
        self.state.save_source_frame("jetson-01", "wide", {"camera_role": "wide"}, jpeg2)

        device = self.state.get_device("jetson-01")
        sources = device["latest_stream_by_source"]
        self.assertIn("processor", sources)
        self.assertIn("wide", sources)
        self.assertEqual(sources["processor"]["camera_role"], "processor")
        self.assertEqual(sources["wide"]["camera_role"], "wide")


if __name__ == "__main__":
    unittest.main()
