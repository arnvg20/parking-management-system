from copy import deepcopy
from datetime import datetime, timezone


JETSON_STATES = {"Pending", "Patrol", "Safe_Shutdown"}
JETSON_COMMAND_ALIASES = {
    "cmd_patrol": "cmd_patrol",
    "cmd_standby": "cmd_standby",
    "camera_on": "cmd_patrol",
    "camera_off": "cmd_standby",
    "cmd_post_patrol": "cmd_standby",
}
JETSON_COMMANDS = tuple(sorted(set(JETSON_COMMAND_ALIASES.values())))


def parse_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_timestamp(value):
    if not value:
        return None

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_to_iso(value):
    parsed = parse_timestamp(value)
    return parsed.isoformat() if parsed else None


def normalize_robot_status(value, default="Pending"):
    if not value:
        return default

    normalized = str(value).strip().replace("-", "_").replace(" ", "_").lower()
    aliases = {
        "pending": "Pending",
        "patrol": "Patrol",
        "safe_shutdown": "Safe_Shutdown",
        "shutdown": "Safe_Shutdown",
        "standby": "Pending",
        "waiting": "Pending",
        "idle": "Pending",
        "online": default,
    }
    return aliases.get(normalized, value if value in JETSON_STATES else default)


def normalize_command_name(value):
    if not value:
        return None
    return JETSON_COMMAND_ALIASES.get(str(value).strip())


def normalize_bbox_xyxy(value):
    if not isinstance(value, dict):
        return None

    bbox = {
        "x1": parse_float(value.get("x1")),
        "y1": parse_float(value.get("y1")),
        "x2": parse_float(value.get("x2")),
        "y2": parse_float(value.get("y2")),
    }
    if all(component is None for component in bbox.values()):
        return None
    return bbox


def normalize_plate_detection(item, fallback_timestamp=None):
    if not isinstance(item, dict):
        return None

    gps = item.get("gps") if isinstance(item.get("gps"), dict) else {}
    detected_at = timestamp_to_iso(item.get("detected_at")) or timestamp_to_iso(fallback_timestamp)
    return {
        "event_id": item.get("event_id"),
        "plate_text": item.get("plate_text"),
        "detected_at": detected_at,
        "confidence": parse_float(item.get("confidence")),
        "bbox_xyxy": normalize_bbox_xyxy(item.get("bbox_xyxy")),
        "image_id": item.get("image_id"),
        "source_camera": item.get("source_camera"),
        "gps": {
            "lat": parse_float(gps.get("lat")),
            "lon": parse_float(gps.get("lon")),
        },
    }


def select_latest_detection(plate_detections):
    latest_detection = None
    latest_detected_at = None

    for detection in plate_detections or []:
        detected_at = parse_timestamp((detection or {}).get("detected_at"))
        if not detected_at:
            continue
        if latest_detected_at is None or detected_at > latest_detected_at:
            latest_detected_at = detected_at
            latest_detection = detection

    if latest_detection:
        return deepcopy(latest_detection)
    return deepcopy((plate_detections or [None])[-1])


def normalize_telemetry_payload(payload, fallback_device_id=None):
    source = payload if isinstance(payload, dict) else {}
    nested_payload = source.get("telemetry") if isinstance(source.get("telemetry"), dict) else None
    if nested_payload and "plate_detections" in nested_payload:
        source = {**nested_payload, "device_id": source.get("device_id") or nested_payload.get("device_id")}

    device_id = source.get("device_id") or fallback_device_id
    timestamp = timestamp_to_iso(source.get("timestamp"))
    raw_detections = source.get("plate_detections")
    if not isinstance(raw_detections, list):
        raw_detections = []

    normalized = {
        "device_id": device_id,
        "cpu": parse_float(source.get("cpu")),
        "memory": parse_float(source.get("memory")),
        "temp_c": parse_float(source.get("temp_c")),
        "camera_on": source.get("camera_on"),
        "stream_enabled": source.get("stream_enabled"),
        "robot_status": normalize_robot_status(source.get("robot_status") or source.get("status")),
        "timestamp": timestamp,
        "plate_detections": [],
    }

    for item in raw_detections:
        detection = normalize_plate_detection(item, fallback_timestamp=timestamp)
        if detection:
            normalized["plate_detections"].append(detection)

    normalized["latest_detection"] = select_latest_detection(normalized["plate_detections"])
    return normalized
