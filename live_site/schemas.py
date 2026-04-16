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


class DetectionLocationPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    lat: float
    lon: float


class PlateDetectionPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    plate_read: str | None = Field(
        default=None,
        validation_alias=AliasChoices("plate_read", "detected_plate", "plate", "license_plate"),
    )
    time: str | None = Field(
        default=None,
        validation_alias=AliasChoices("time", "timestamp"),
    )
    location: DetectionLocationPayload | None = None
    confidence_level: float | None = Field(
        default=None,
        validation_alias=AliasChoices("confidence_level", "confidence"),
    )


class JetsonTelemetryEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    device_id: str | None = None
    cpu: float | None = None
    memory: float | None = None
    temp_c: float | None = None
    camera_on: bool | None = None
    stream_enabled: bool | None = None
    robot_status: str | None = Field(
        default=None,
        validation_alias=AliasChoices("robot_status", "status"),
    )
    timestamp: str | None = None
    plate_detections: list[PlateDetectionPayload] = Field(default_factory=list)


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
