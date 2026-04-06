from __future__ import annotations

from datetime import datetime, timezone

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TelemetryUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    latitude: float | None = None
    longitude: float | None = None
    detected_plate: str | None = Field(
        default=None,
        validation_alias=AliasChoices("detected_plate", "plate", "license_plate"),
    )
    confidence: float | None = None
    timestamp: str | None = None
    robot_status: str | None = Field(
        default=None,
        validation_alias=AliasChoices("robot_status", "status"),
    )
    source: str | None = None


def empty_telemetry_snapshot() -> dict[str, object | None]:
    return {
        "latitude": None,
        "longitude": None,
        "detected_plate": None,
        "confidence": None,
        "timestamp": None,
        "robot_status": None,
        "source": "waiting-for-telemetry",
        "received_at": utc_now_iso(),
    }
