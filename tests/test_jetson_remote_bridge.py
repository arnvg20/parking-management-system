import threading
import time
import unittest

import httpx

from jetson_remote_bridge import ImageUploadQueue, ImageUploadTask, JetsonRemoteBridge


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.request = httpx.Request("POST", "http://backend.test")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("failed", request=self.request, response=httpx.Response(self.status_code))


class BlockingUploadClient:
    def __init__(self):
        self.upload_started = threading.Event()
        self.release_upload = threading.Event()
        self.calls = []
        self.lock = threading.Lock()

    def request(self, method, url, **kwargs):
        with self.lock:
            self.calls.append((method, url, kwargs))
        if url.endswith("/api/jetson/upload-image"):
            self.upload_started.set()
            self.release_upload.wait(timeout=2)
            return FakeResponse(200, {"status": "stored", "image": {"id": "image-1"}})
        if url.endswith("/api/jetson/heartbeat"):
            return FakeResponse(200, {"status": "ok"})
        return FakeResponse(204, {})

    def close(self):
        pass


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class JetsonRemoteBridgeTests(unittest.TestCase):
    def test_image_upload_does_not_block_heartbeat(self):
        client = BlockingUploadClient()
        bridge = JetsonRemoteBridge(
            "http://backend.test",
            client=client,
            http_timeout_sec=15,
            image_upload_timeout_sec=4,
        )
        try:
            self.assertTrue(
                bridge.enqueue_image_upload(
                    b"jpeg",
                    "crop.jpg",
                    metadata={"plate_text": "ABC123", "confidence": 0.90},
                )
            )
            self.assertTrue(client.upload_started.wait(timeout=1))

            started = time.monotonic()
            heartbeat = bridge.send_heartbeat({"status": "online"})
            elapsed = time.monotonic() - started

            self.assertEqual(heartbeat, {"status": "ok"})
            self.assertLess(elapsed, 0.1)
            timeouts = [call[2]["timeout"] for call in client.calls]
            self.assertIn(4, timeouts)
            self.assertIn(15, timeouts)
        finally:
            client.release_upload.set()
            bridge.close()

    def test_retries_then_counts_upload_only_after_confirmed_storage(self):
        attempts = []

        def upload(task, timeout):
            attempts.append((task, timeout))
            if len(attempts) == 1:
                return False, None
            return True, {"status": "stored", "image": {"id": "stored-1"}}

        queue = ImageUploadQueue(
            upload,
            timeout_sec=3,
            max_attempts=2,
            backoff_base_sec=0.01,
            backoff_max_sec=0.01,
        )
        try:
            queue.enqueue(
                ImageUploadTask(
                    image_bytes=b"jpeg",
                    filename="crop.jpg",
                    metadata={"plate_text": "ABC123", "confidence": 0.90},
                )
            )
            self.assertTrue(wait_for(lambda: queue.snapshot()["counters"].get("uploaded") == 1))
            stats = queue.snapshot()
            self.assertEqual(stats["counters"].get("failed"), 1)
            self.assertEqual(stats["counters"].get("uploaded"), 1)
            self.assertEqual(attempts[0][1], 3)
            self.assertEqual(attempts[1][0].upload_id, "stored-1")
        finally:
            queue.stop()

    def test_pending_backlog_is_capped_while_upload_is_inflight(self):
        release_upload = threading.Event()

        def upload(task, timeout):
            release_upload.wait(timeout=2)
            return False, None

        queue = ImageUploadQueue(upload, max_pending=2, max_attempts=1)
        try:
            for index in range(4):
                queue.enqueue(
                    ImageUploadTask(
                        image_bytes=b"jpeg",
                        filename=f"crop-{index}.jpg",
                        metadata={"plate_text": f"PLATE{index}", "confidence": index},
                    )
                )
            self.assertTrue(wait_for(lambda: queue.snapshot()["inflight"] == 1))
            stats = queue.snapshot()
            self.assertLessEqual(stats["pending"], 2)
            self.assertGreaterEqual(stats["counters"].get("dropped", 0), 1)
        finally:
            release_upload.set()
            queue.stop()

    def test_pending_crop_for_same_plate_is_coalesced_to_better_candidate(self):
        release_upload = threading.Event()

        def upload(task, timeout):
            release_upload.wait(timeout=2)
            return False, None

        queue = ImageUploadQueue(upload, max_pending=4, max_attempts=1)
        try:
            queue.enqueue(
                ImageUploadTask(
                    image_bytes=b"jpeg",
                    filename="inflight.jpg",
                    metadata={"plate_text": "BLOCK1", "confidence": 1.0},
                )
            )
            self.assertTrue(wait_for(lambda: queue.snapshot()["inflight"] == 1))
            queue.enqueue(
                ImageUploadTask(
                    image_bytes=b"old",
                    filename="old.jpg",
                    metadata={
                        "plate_text": "ABC123",
                        "confidence": 0.50,
                        "detected_at": "2026-04-24T10:00:00Z",
                    },
                )
            )
            queue.enqueue(
                ImageUploadTask(
                    image_bytes=b"new",
                    filename="new.jpg",
                    metadata={
                        "plate_text": "ABC123",
                        "confidence": 0.90,
                        "detected_at": "2026-04-24T10:00:01Z",
                    },
                )
            )

            stats = queue.snapshot()
            self.assertEqual(stats["pending"], 1)
            self.assertEqual(stats["counters"].get("coalesced"), 1)
        finally:
            release_upload.set()
            queue.stop()

    def test_frame_upload_is_disabled_by_default(self):
        client = BlockingUploadClient()
        bridge = JetsonRemoteBridge("http://backend.test", client=client)
        try:
            result = bridge.upload_frame(b"jpeg")
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(client.calls, [])
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main()
