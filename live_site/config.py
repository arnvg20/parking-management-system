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
    bbox_filter_enabled: bool
    bbox_window_sec: float
    bbox_top_k_per_window: int
    bbox_min_relative_height_ratio: float
    bbox_min_absolute_height_px: float
    bbox_use_area_tiebreak: bool
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
            bbox_filter_enabled=_env_flag("BBOX_FILTER_ENABLED", True),
            bbox_window_sec=float(os.getenv("BBOX_WINDOW_SEC", "2.0")),
            bbox_top_k_per_window=int(os.getenv("BBOX_TOP_K_PER_WINDOW", "1")),
            bbox_min_relative_height_ratio=float(os.getenv("BBOX_MIN_RELATIVE_HEIGHT_RATIO", "0.65")),
            bbox_min_absolute_height_px=float(os.getenv("BBOX_MIN_ABSOLUTE_HEIGHT_PX", "0")),
            bbox_use_area_tiebreak=_env_flag("BBOX_USE_AREA_TIEBREAK", True),
            static_dir=Path(__file__).resolve().parent / "static",
            runtime_dir=BASE_DIR / "runtime_data",
        )

    @property
    def stream_label(self) -> str:
        return f"/{self.media_mtx_stream_path}"

    @property
    def whep_proxy_path(self) -> str:
        return f"/api/webrtc/{self.media_mtx_stream_path}/whep"
