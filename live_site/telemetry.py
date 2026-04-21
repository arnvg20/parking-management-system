from __future__ import annotations

import asyncio
import math
from contextlib import suppress

from fastapi import WebSocket

from .config import Settings
from .schemas import TelemetryUpdate, empty_telemetry_snapshot, utc_now_iso


class TelemetryHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._latest_snapshot = empty_telemetry_snapshot()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
            snapshot = dict(self._latest_snapshot)
        await websocket.send_json(snapshot)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def get_snapshot(self) -> dict[str, object | None]:
        async with self._lock:
            return dict(self._latest_snapshot)

    async def publish(self, update: TelemetryUpdate | dict[str, object]) -> dict[str, object | None]:
        if isinstance(update, TelemetryUpdate):
            incoming = update.model_dump(exclude_none=True)
            if "power" in update.model_fields_set and update.power is None:
                incoming["power"] = None
        else:
            incoming = {key: value for key, value in update.items() if value is not None or key == "power"}

        async with self._lock:
            snapshot = dict(self._latest_snapshot)
            snapshot.update(incoming)
            snapshot["timestamp"] = incoming.get("timestamp") or snapshot.get("timestamp") or utc_now_iso()
            snapshot["received_at"] = utc_now_iso()
            if not snapshot.get("source"):
                snapshot["source"] = "telemetry-publisher"
            self._latest_snapshot = snapshot
            recipients = list(self._connections)

        stale_connections: list[WebSocket] = []
        for websocket in recipients:
            try:
                await websocket.send_json(snapshot)
            except Exception:
                stale_connections.append(websocket)

        if stale_connections:
            async with self._lock:
                for websocket in stale_connections:
                    self._connections.discard(websocket)

        return snapshot


class DemoTelemetryPublisher:
    def __init__(self, hub: TelemetryHub, settings: Settings) -> None:
        self._hub = hub
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return

        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        step = 0
        while True:
            offset = math.sin(step / 4)
            await self._hub.publish(
                {
                    "latitude": round(43.6532 + (offset * 0.0003), 6),
                    "longitude": round(-79.3832 + (offset * 0.0004), 6),
                    "detected_plate": f"CAP{200 + (step % 7):03d}",
                    "confidence": round(0.88 + ((step % 5) * 0.02), 2),
                    "timestamp": utc_now_iso(),
                    "robot_status": ["Patrolling", "Inspecting", "Holding position"][step % 3],
                    "power": {
                        "battery_channel": "CH1",
                        "pack_voltage_v": round(12.7 + (offset * 0.18), 2),
                        "shutdown_threshold_v": 12.0,
                        "power_action": "stay_on",
                        "will_shutdown": False,
                        "status": "monitoring",
                        "message": "Demo battery voltage is healthy.",
                        "low_voltage_duration_sec": 0.0,
                    },
                    "source": "demo-generator",
                }
            )
            step += 1
            await asyncio.sleep(self._settings.demo_telemetry_interval_seconds)
