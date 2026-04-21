from __future__ import annotations

from datetime import datetime, timezone

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PowerTelemetryPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    battery_channel: str | None = None
    pack_voltage_v: float | None = None
    shutdown_threshold_v: float | None = None
    power_action: str | None = None
    will_shutdown: bool | None = None
    status: str | None = None
    message: str | None = None
    low_voltage_duration_sec: float | None = None


class OrientationPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    heading_deg: float | None = None
    heading_source: str | None = None
    heading_reference: str | None = None
    compass_sensor: str | None = None
    fix_timestamp_utc: str | None = None
    updated_at_utc: str | None = None
    status: str | None = None
    device_path: str | None = None


class LocationPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    lat: float | None = Field(default=None, validation_alias=AliasChoices("lat", "latitude"))
    lon: float | None = Field(default=None, validation_alias=AliasChoices("lon", "longitude"))
    alt_m: float | None = None
    heading_deg: float | None = None
    heading_source: str | None = None
    heading_reference: str | None = None
    compass_sensor: str | None = None
    speed_mps: float | None = None
    fix_timestamp_utc: str | None = None
    updated_at_utc: str | None = None
    status: str | None = None
    device_path: str | None = None


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
    power: PowerTelemetryPayload | None = None
    robot_status: str | None = Field(
        default=None,
        validation_alias=AliasChoices("robot_status", "status"),
    )
    source: str | None = None
    heading_deg: float | None = None
    heading_source: str | None = None


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


class StreamInfoPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str | None = None
    label: str | None = None
    provider: str | None = None
    transport: str | None = None
    stream_path: str | None = None
    iframe_url: str | None = None
    webrtc_url: str | None = None
    whep_url: str | None = None
    width: int | None = None
    height: int | None = None
    nominal_fps: float | None = None
    device_path: str | None = None
    running: bool | None = None
    source_id: int | None = None
    updated_at_utc: str | None = None


class DeviceStreamsPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    viewer: StreamInfoPayload | None = None
    processor: StreamInfoPayload | None = None


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
    power: PowerTelemetryPayload | None = None
    plate_detections: list[PlateDetectionPayload] = Field(default_factory=list)
    orientation: OrientationPayload | None = None
    location: LocationPayload | None = None
    streams: DeviceStreamsPayload | None = None


def empty_telemetry_snapshot() -> dict[str, object | None]:
    return {
        "latitude": None,
        "longitude": None,
        "detected_plate": None,
        "confidence": None,
        "timestamp": None,
        "power": None,
        "robot_status": None,
        "source": "waiting-for-telemetry",
        "received_at": utc_now_iso(),
    }
