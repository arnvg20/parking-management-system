from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count
from typing import Any, Callable

import httpx


LOGGER = logging.getLogger(__name__)

HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "15"))
IMAGE_UPLOAD_TIMEOUT_SEC = float(os.getenv("IMAGE_UPLOAD_TIMEOUT_SEC", "4"))
IMAGE_UPLOAD_MAX_PENDING = int(os.getenv("IMAGE_UPLOAD_MAX_PENDING", "64"))
IMAGE_UPLOAD_MAX_ATTEMPTS = int(os.getenv("IMAGE_UPLOAD_MAX_ATTEMPTS", "3"))
IMAGE_UPLOAD_MAX_AGE_SEC = float(os.getenv("IMAGE_UPLOAD_MAX_AGE_SEC", "120"))
IMAGE_UPLOAD_BACKOFF_BASE_SEC = float(os.getenv("IMAGE_UPLOAD_BACKOFF_BASE_SEC", "1"))
IMAGE_UPLOAD_BACKOFF_MAX_SEC = float(os.getenv("IMAGE_UPLOAD_BACKOFF_MAX_SEC", "15"))
FRAME_UPLOAD_ENABLED = os.getenv("FRAME_UPLOAD_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}

_PLATE_RE = re.compile(r"[^A-Z0-9]")
_SEQUENCE = count(1)


def _normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def _normalize_plate(value: Any) -> str | None:
    if value is None:
        return None
    normalized = _PLATE_RE.sub("", str(value).upper())
    return normalized or None


def _parse_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


@dataclass
class ImageUploadTask:
    image_bytes: bytes
    filename: str
    metadata: dict[str, Any] = field(default_factory=dict)
    device_id: str = "jetson-01"
    content_type: str = "image/jpeg"
    created_monotonic: float = field(default_factory=time.monotonic)
    sequence: int = field(default_factory=lambda: next(_SEQUENCE))
    attempt: int = 0
    next_attempt_at: float = 0.0
    uploaded: bool = False
    upload_id: str | None = None

    @property
    def plate_text(self) -> str | None:
        return _first_present(
            self.metadata.get("plate_text"),
            self.metadata.get("plate"),
            self.metadata.get("license_plate"),
            self.metadata.get("detected_plate"),
            self.metadata.get("plate_read"),
        )

    @property
    def track_id(self) -> str | None:
        return _first_present(
            self.metadata.get("track_id"),
            self.metadata.get("plate_track_id"),
            self.metadata.get("vehicle_track_id"),
        )

    @property
    def confidence(self) -> float:
        value = _first_present(
            self.metadata.get("confidence"),
            self.metadata.get("confidence_level"),
            self.metadata.get("ocr_confidence"),
            self.metadata.get("score"),
        )
        return _as_float(value) or 0.0

    @property
    def timestamp(self) -> float:
        value = _first_present(
            self.metadata.get("detected_at"),
            self.metadata.get("timestamp"),
            self.metadata.get("time"),
            self.metadata.get("created_at"),
        )
        return _parse_timestamp(value) or self.created_monotonic

    @property
    def dedupe_key(self) -> str:
        if self.track_id:
            return f"track:{self.track_id}"
        plate = _normalize_plate(self.plate_text)
        if plate:
            return f"plate:{plate}"
        event_id = _first_present(self.metadata.get("event_id"), self.metadata.get("detection_id"))
        if event_id:
            return f"event:{event_id}"
        return f"crop:{self.sequence}"


class ImageUploadQueue:
    def __init__(
        self,
        upload_func: Callable[[ImageUploadTask, float], tuple[bool, dict[str, Any] | None]],
        *,
        timeout_sec: float = IMAGE_UPLOAD_TIMEOUT_SEC,
        max_pending: int = IMAGE_UPLOAD_MAX_PENDING,
        max_attempts: int = IMAGE_UPLOAD_MAX_ATTEMPTS,
        max_age_sec: float = IMAGE_UPLOAD_MAX_AGE_SEC,
        backoff_base_sec: float = IMAGE_UPLOAD_BACKOFF_BASE_SEC,
        backoff_max_sec: float = IMAGE_UPLOAD_BACKOFF_MAX_SEC,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._upload_func = upload_func
        self._timeout_sec = timeout_sec
        self._max_pending = max(1, max_pending)
        self._max_attempts = max(1, max_attempts)
        self._max_age_sec = max_age_sec
        self._backoff_base_sec = max(0.0, backoff_base_sec)
        self._backoff_max_sec = max(backoff_base_sec, backoff_max_sec)
        self._logger = logger
        self._condition = threading.Condition()
        self._pending: dict[str, ImageUploadTask] = {}
        self._inflight: set[str] = set()
        self._counters: Counter[str] = Counter(
            {
                "queued": 0,
                "uploaded": 0,
                "failed": 0,
                "skipped": 0,
                "dropped": 0,
                "backoff": 0,
                "coalesced": 0,
            }
        )
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="jetson-image-uploader", daemon=True)
        self._thread.start()

    def enqueue(self, task: ImageUploadTask) -> bool:
        key = task.dedupe_key
        now = time.monotonic()
        task.next_attempt_at = now
        with self._condition:
            if self._stopped:
                self._counters["skipped"] += 1
                return False

            existing = self._pending.get(key)
            if existing is not None:
                if self._is_better_crop(task, existing):
                    self._pending[key] = task
                    self._counters["queued"] += 1
                    self._counters["coalesced"] += 1
                    self._logger.debug("coalesced image upload key=%s queue_depth=%s", key, len(self._pending))
                    self._condition.notify()
                    return True
                self._counters["skipped"] += 1
                self._logger.debug("skipped older/weaker image upload key=%s", key)
                return False

            if len(self._pending) >= self._max_pending:
                dropped_key = self._oldest_pending_key()
                if dropped_key is None:
                    self._counters["dropped"] += 1
                    self._logger.warning("dropped image upload key=%s reason=queue_full_no_candidate", key)
                    return False
                self._pending.pop(dropped_key, None)
                self._counters["dropped"] += 1
                self._logger.warning(
                    "dropped image upload key=%s reason=queue_full replacement=%s",
                    dropped_key,
                    key,
                )

            self._pending[key] = task
            self._counters["queued"] += 1
            self._logger.debug("queued image upload key=%s queue_depth=%s", key, len(self._pending))
            self._condition.notify()
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            now = time.monotonic()
            next_attempts = [task.next_attempt_at for task in self._pending.values() if task.next_attempt_at > now]
            next_delay = min(next_attempts) - now if next_attempts else 0.0
            return {
                "pending": len(self._pending),
                "inflight": len(self._inflight),
                "backing_off": sum(1 for task in self._pending.values() if task.next_attempt_at > now),
                "next_upload_delay_sec": round(max(0.0, next_delay), 3),
                "counters": dict(self._counters),
            }

    def stop(self, timeout: float = 2.0) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        self._thread.join(timeout=timeout)

    def _is_better_crop(self, new: ImageUploadTask, old: ImageUploadTask) -> bool:
        if new.confidence > old.confidence:
            return True
        if new.confidence == old.confidence and new.timestamp >= old.timestamp:
            return True
        return new.timestamp > old.timestamp and new.confidence >= max(0.0, old.confidence - 0.05)

    def _oldest_pending_key(self) -> str | None:
        if not self._pending:
            return None
        return min(
            self._pending,
            key=lambda key: (self._pending[key].created_monotonic, self._pending[key].sequence),
        )

    def _drop_expired_locked(self, now: float) -> None:
        expired = [
            key
            for key, task in self._pending.items()
            if now - task.created_monotonic > self._max_age_sec
        ]
        for key in expired:
            self._pending.pop(key, None)
            self._counters["dropped"] += 1
            self._logger.warning("dropped image upload key=%s reason=max_age", key)

    def _next_ready_task_locked(self, now: float) -> tuple[str, ImageUploadTask] | None:
        self._drop_expired_locked(now)
        ready = [
            (task.created_monotonic, task.sequence, key, task)
            for key, task in self._pending.items()
            if task.next_attempt_at <= now
        ]
        if not ready:
            return None
        _, _, key, task = min(ready)
        self._pending.pop(key, None)
        self._inflight.add(key)
        return key, task

    def _backoff_seconds(self, attempt: int) -> float:
        delay = self._backoff_base_sec * (2 ** max(0, attempt - 1))
        return min(self._backoff_max_sec, delay)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._stopped:
                    now = time.monotonic()
                    selected = self._next_ready_task_locked(now)
                    if selected:
                        key, task = selected
                        break
                    next_times = [task.next_attempt_at for task in self._pending.values()]
                    wait_for = 0.5
                    if next_times:
                        wait_for = max(0.05, min(0.5, min(next_times) - now))
                    self._condition.wait(timeout=wait_for)
                else:
                    return

            success = False
            response_payload = None
            try:
                success, response_payload = self._upload_func(task, self._timeout_sec)
            except Exception:
                self._logger.exception("image upload failed before response key=%s", key)

            with self._condition:
                self._inflight.discard(key)
                if success:
                    task.uploaded = True
                    image_payload = (response_payload or {}).get("image") if isinstance(response_payload, dict) else {}
                    task.upload_id = image_payload.get("id") if isinstance(image_payload, dict) else None
                    self._counters["uploaded"] += 1
                    self._logger.info("uploaded image key=%s upload_id=%s", key, task.upload_id)
                    continue

                task.attempt += 1
                self._counters["failed"] += 1
                now = time.monotonic()
                if task.attempt >= self._max_attempts or now - task.created_monotonic > self._max_age_sec:
                    self._counters["dropped"] += 1
                    self._logger.warning(
                        "dropped image upload key=%s reason=retry_exhausted attempts=%s",
                        key,
                        task.attempt,
                    )
                    continue

                delay = self._backoff_seconds(task.attempt)
                task.next_attempt_at = now + delay
                self._pending[key] = task
                self._counters["backoff"] += 1
                self._logger.warning(
                    "image upload backoff key=%s attempt=%s delay_sec=%.2f",
                    key,
                    task.attempt,
                    delay,
                )
                self._condition.notify()


class JetsonRemoteBridge:
    def __init__(
        self,
        backend_url: str,
        *,
        device_id: str = "jetson-01",
        api_token: str | None = None,
        http_timeout_sec: float = HTTP_TIMEOUT_SEC,
        image_upload_timeout_sec: float = IMAGE_UPLOAD_TIMEOUT_SEC,
        frame_upload_enabled: bool = FRAME_UPLOAD_ENABLED,
        client: httpx.Client | None = None,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self.backend_url = _normalize_base_url(backend_url)
        self.device_id = device_id
        self.api_token = api_token
        self.http_timeout_sec = http_timeout_sec
        self.image_upload_timeout_sec = image_upload_timeout_sec
        self.frame_upload_enabled = frame_upload_enabled
        self._logger = logger
        self._client = client or httpx.Client(follow_redirects=False)
        self._owns_client = client is None
        self.image_uploads = ImageUploadQueue(
            self._upload_image_task,
            timeout_sec=image_upload_timeout_sec,
            logger=logger,
        )

    def close(self) -> None:
        self.image_uploads.stop()
        if self._owns_client:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        if not self.api_token:
            return {}
        return {"Authorization": f"Bearer {self.api_token}", "X-API-Key": self.api_token}

    def _url(self, path: str) -> str:
        return f"{self.backend_url}/{path.lstrip('/')}"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any] | None:
        response = self._client.request(
            method,
            self._url(path),
            json=payload,
            params=params,
            headers=self._headers(),
            timeout=timeout_sec or self.http_timeout_sec,
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response.json()

    def send_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        body = {"device_id": self.device_id, **payload}
        return self._request_json("POST", "/api/jetson/heartbeat", payload=body)

    def send_telemetry(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        body = {"device_id": self.device_id, **payload}
        return self._request_json("POST", "/api/jetson/telemetry", payload=body)

    def poll_next_command(self, wait_sec: int = 20) -> dict[str, Any] | None:
        return self._request_json(
            "GET",
            "/api/jetson/commands/next",
            params={"device_id": self.device_id, "wait": wait_sec},
        )

    def ack_command(self, command_id: int, success: bool, result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        return self._request_json(
            "POST",
            f"/api/jetson/commands/{command_id}/ack",
            payload={"device_id": self.device_id, "success": success, "result": result or {}},
        )

    def enqueue_image_upload(
        self,
        image_bytes: bytes,
        filename: str,
        *,
        metadata: dict[str, Any] | None = None,
        content_type: str = "image/jpeg",
    ) -> bool:
        task = ImageUploadTask(
            image_bytes=image_bytes,
            filename=filename,
            metadata=metadata or {},
            device_id=self.device_id,
            content_type=content_type,
        )
        return self.image_uploads.enqueue(task)

    def upload_frame(
        self,
        frame_bytes: bytes,
        filename: str = "frame.jpg",
        *,
        source_id: str = "processor",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self.frame_upload_enabled:
            self._logger.debug("skipped frame upload reason=disabled source_id=%s", source_id)
            return {"status": "skipped", "reason": "frame_upload_disabled"}
        response = self._client.request(
            "POST",
            self._url("/api/jetson/upload-frame"),
            headers=self._headers(),
            data={"device_id": self.device_id, "source_id": source_id, "meta": json.dumps(metadata or {})},
            files={"frame": (filename, frame_bytes, "image/jpeg")},
            timeout=self.http_timeout_sec,
        )
        response.raise_for_status()
        return response.json()

    def upload_stats(self) -> dict[str, Any]:
        return self.image_uploads.snapshot()

    def _upload_image_task(self, task: ImageUploadTask, timeout_sec: float) -> tuple[bool, dict[str, Any] | None]:
        data = {"device_id": task.device_id, **task.metadata}
        response = self._client.request(
            "POST",
            self._url("/api/jetson/upload-image"),
            headers=self._headers(),
            data=data,
            files={"image": (task.filename, task.image_bytes, task.content_type)},
            timeout=timeout_sec,
        )
        if response.status_code >= 400:
            self._logger.warning("image upload failed status=%s filename=%s", response.status_code, task.filename)
            return False, None

        try:
            payload = response.json()
        except ValueError:
            self._logger.warning("image upload response was not json filename=%s", task.filename)
            return False, None

        image_payload = payload.get("image") if isinstance(payload, dict) else None
        confirmed = payload.get("status") in {"stored", "ok", "success"} and isinstance(image_payload, dict)
        if not confirmed:
            self._logger.warning("image upload response did not confirm storage filename=%s", task.filename)
            return False, payload if isinstance(payload, dict) else None
        return True, payload


__all__ = [
    "HTTP_TIMEOUT_SEC",
    "IMAGE_UPLOAD_TIMEOUT_SEC",
    "ImageUploadQueue",
    "ImageUploadTask",
    "JetsonRemoteBridge",
]
