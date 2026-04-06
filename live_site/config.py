from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


TRUTHY_VALUES = {"1", "true", "yes", "on"}
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_flag(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in TRUTHY_VALUES


def _normalize_stream_path(value: str) -> str:
    normalized = (value or "mystream").strip().strip("/")
    return normalized or "mystream"


def _env_path(name: str) -> Path | None:
    raw_value = os.getenv(name)
    if not raw_value:
        return None
    return Path(raw_value).expanduser()


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    reload: bool
    media_mtx_base_url: str
    media_mtx_stream_path: str
    default_device_id: str
    jetson_api_token: str
    telemetry_api_key: str
    demo_telemetry_enabled: bool
    demo_telemetry_interval_seconds: float
    request_timeout_seconds: float
    gps_route_calibration_enabled: bool
    route_reference_kml_path: Path | None
    route_mapping_max_distance_meters: float
    bbox_area_priority_enabled: bool
    bbox_area_priority_weight: float
    bbox_area_similarity_ratio: float
    static_dir: Path
    runtime_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "5000")),
            reload=_env_flag("RELOAD", False),
            media_mtx_base_url=os.getenv("MEDIA_MTX_BASE_URL", "http://127.0.0.1:8889").rstrip("/"),
            media_mtx_stream_path=_normalize_stream_path(os.getenv("MEDIA_MTX_STREAM_PATH", "jetson-01")),
            default_device_id=os.getenv("DEFAULT_DEVICE_ID", "jetson-01").strip() or "jetson-01",
            jetson_api_token=os.getenv("JETSON_API_TOKEN", "dev-jetson-token").strip(),
            telemetry_api_key=os.getenv("TELEMETRY_API_KEY", "dev-telemetry-token").strip(),
            demo_telemetry_enabled=_env_flag("DEMO_TELEMETRY_ENABLED", False),
            demo_telemetry_interval_seconds=float(os.getenv("DEMO_TELEMETRY_INTERVAL_SECONDS", "1.5")),
            request_timeout_seconds=float(os.getenv("MEDIA_MTX_REQUEST_TIMEOUT_SECONDS", "10")),
            gps_route_calibration_enabled=_env_flag("GPS_ROUTE_CALIBRATION_ENABLED", True),
            route_reference_kml_path=_env_path("GPS_ROUTE_REFERENCE_KML_PATH"),
            route_mapping_max_distance_meters=float(os.getenv("GPS_ROUTE_CALIBRATION_MAX_DISTANCE_METERS", "30")),
            bbox_area_priority_enabled=_env_flag("BBOX_AREA_PRIORITY_ENABLED", True),
            bbox_area_priority_weight=float(os.getenv("BBOX_AREA_PRIORITY_WEIGHT", "1.0")),
            bbox_area_similarity_ratio=float(os.getenv("BBOX_AREA_SIMILARITY_RATIO", "0.1")),
            static_dir=Path(__file__).resolve().parent / "static",
            runtime_dir=BASE_DIR / "runtime_data",
        )

    @property
    def stream_label(self) -> str:
        return f"/{self.media_mtx_stream_path}"

    @property
    def whep_proxy_path(self) -> str:
        return f"/api/webrtc/{self.media_mtx_stream_path}/whep"
