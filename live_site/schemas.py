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

    lat: float = Field(validation_alias=AliasChoices("lat", "latitude"))
    lon: float = Field(validation_alias=AliasChoices("lon", "longitude"))


class PlateDetectionPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    plate_read: str | None = Field(
        default=None,
        validation_alias=AliasChoices("plate_read", "detected_plate", "plate", "license_plate", "plate_text"),
    )
    time: str | None = Field(
        default=None,
        validation_alias=AliasChoices("time", "timestamp", "detected_at"),
    )
    location: DetectionLocationPayload | None = Field(
        default=None,
        validation_alias=AliasChoices("location", "gps"),
    )
    confidence_level: float | None = Field(
        default=None,
        validation_alias=AliasChoices("confidence_level", "confidence"),
    )
    bbox_xyxy: list[float] | None = None
    source_camera: str | None = None


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
