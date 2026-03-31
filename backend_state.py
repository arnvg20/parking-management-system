import copy
import json
import math
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.utils import secure_filename

from jetson_contract import normalize_robot_status, parse_timestamp, select_latest_detection


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def coerce_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


class BackendState:
    def __init__(self, parking_spaces, find_matching_space, runtime_dir="runtime_data", default_device_id="jetson-01"):
        self.lock = threading.RLock()
        self.command_condition = threading.Condition(self.lock)
        self.frame_condition = threading.Condition(self.lock)

        self.parking_spaces = parking_spaces
        self.find_matching_space = find_matching_space
        self.devices = {}
        self.commands = []
        self.uploads = {}
        self.subscribers = set()
        self.command_sequence = 1
        self.default_device_id = default_device_id
        self.space_resolution_offset_meters = 12

        self.runtime_dir = Path(runtime_dir)
        self.images_dir = self.runtime_dir / "images"
        self.frames_dir = self.runtime_dir / "frames"
        self.state_file = self.runtime_dir / "state.json"

        self._ensure_runtime_dirs()
        self._load_state()
        self.ensure_device(default_device_id, name="Jetson Primary")

    def _ensure_runtime_dirs(self):
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir.mkdir(parents=True, exist_ok=True)

    def _load_state(self):
        if not self.state_file.exists():
            return

        try:
            persisted = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        persisted_spaces = persisted.get("parking_spaces", {})
        for space_id, values in persisted_spaces.items():
            if space_id not in self.parking_spaces:
                continue
            self.parking_spaces[space_id]["occupied"] = bool(values.get("occupied"))
            self.parking_spaces[space_id]["vehicle_data"] = values.get("vehicle_data")

        self.devices = persisted.get("devices", {})
        self.commands = persisted.get("commands", [])
        self.uploads = persisted.get("uploads", {})
        self.command_sequence = persisted.get("command_sequence", 1)

        for device in self.devices.values():
            frame_path = device.get("latest_frame_path")
            device.setdefault("latest_frame_version", 0)
            device.setdefault("recent_image_ids", [])
            device.setdefault("last_heartbeat", {})
            device.setdefault("last_telemetry", {})
            device.setdefault("latest_detection", None)
            device.setdefault("camera_on", False)
            device.setdefault("stream_enabled", False)
            device.setdefault("latest_image_id", None)
            device.setdefault("latest_image_path", None)
            device.setdefault("last_command_result", None)
            device["status"] = normalize_robot_status(device.get("status"))

            if frame_path and Path(frame_path).exists():
                try:
                    device["latest_frame_bytes"] = Path(frame_path).read_bytes()
                except OSError:
                    device["latest_frame_bytes"] = None
            else:
                device["latest_frame_bytes"] = None
                device["latest_frame_path"] = None

    def _serializable_state(self):
        devices = {}
        for device_id, device in self.devices.items():
            device_copy = copy.deepcopy(device)
            device_copy.pop("latest_frame_bytes", None)
            devices[device_id] = device_copy

        return {
            "parking_spaces": {
                space_id: {
                    "occupied": values["occupied"],
                    "vehicle_data": values["vehicle_data"],
                }
                for space_id, values in self.parking_spaces.items()
            },
            "devices": devices,
            "commands": self.commands[-200:],
            "uploads": self.uploads,
            "command_sequence": self.command_sequence,
        }

    def _persist_state_locked(self):
        payload = self._serializable_state()
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _emit_event_locked(self, topic, payload):
        event = {
            "topic": topic,
            "payload": payload,
            "emitted_at": utcnow_iso(),
        }

        stale_subscribers = []
        for subscriber in self.subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                stale_subscribers.append(subscriber)

        for subscriber in stale_subscribers:
            self.subscribers.discard(subscriber)

    def subscribe(self):
        subscriber = queue.Queue(maxsize=50)
        with self.lock:
            self.subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        with self.lock:
            self.subscribers.discard(subscriber)

    def _device_template(self, device_id, name=None):
        display_name = name or device_id
        return {
            "device_id": device_id,
            "name": display_name,
            "status": "Pending",
            "camera_on": False,
            "stream_enabled": False,
            "last_seen_at": None,
            "last_heartbeat": {},
            "last_telemetry": {},
            "latest_detection": None,
            "latest_frame_path": None,
            "latest_frame_updated_at": None,
            "latest_frame_version": 0,
            "latest_frame_bytes": None,
            "latest_image_id": None,
            "latest_image_path": None,
            "recent_image_ids": [],
            "last_command_result": None,
            "updated_at": utcnow_iso(),
        }

    def ensure_device(self, device_id, name=None):
        with self.lock:
            if device_id not in self.devices:
                self.devices[device_id] = self._device_template(device_id, name=name)
                self._persist_state_locked()
            elif name:
                self.devices[device_id]["name"] = name
                self._persist_state_locked()
            return self._device_snapshot_locked(device_id)

    def _device_snapshot_locked(self, device_id):
        device = copy.deepcopy(self.devices[device_id])
        device.pop("latest_frame_bytes", None)

        last_seen_value = device.get("last_seen_at")
        is_online = False
        if last_seen_value:
            try:
                last_seen = datetime.fromisoformat(last_seen_value)
                is_online = datetime.now(timezone.utc) - last_seen <= timedelta(seconds=45)
            except ValueError:
                is_online = False

        device["is_online"] = is_online
        device["latest_frame_available"] = bool(device.get("latest_frame_path"))
        device["latest_frame_url"] = f"/api/devices/{device_id}/latest-frame"
        device["stream_url"] = f"/api/devices/{device_id}/stream.mjpeg"
        if device.get("latest_image_id"):
            device["latest_image_url"] = f"/api/uploads/{device['latest_image_id']}"
        else:
            device["latest_image_url"] = None
        device["recent_images"] = [
            self.uploads[image_id]
            for image_id in device.get("recent_image_ids", [])
            if image_id in self.uploads
        ]
        device["pending_command_count"] = sum(
            1
            for command in self.commands
            if command["device_id"] == device_id and command["status"] in {"queued", "dispatched"}
        )
        return device

    def list_devices(self):
        with self.lock:
            return [self._device_snapshot_locked(device_id) for device_id in sorted(self.devices.keys())]

    def get_device(self, device_id):
        with self.lock:
            if device_id not in self.devices:
                return None
            return self._device_snapshot_locked(device_id)

    def get_default_device_id(self):
        return self.default_device_id

    def get_parking_spaces(self):
        with self.lock:
            return copy.deepcopy(self.parking_spaces)

    def get_recent_commands(self, limit=20):
        with self.lock:
            return copy.deepcopy(list(reversed(self.commands[-limit:])))

    def get_commands_for_device(self, device_id, limit=50):
        with self.lock:
            filtered = [command for command in self.commands if command["device_id"] == device_id]
            return copy.deepcopy(list(reversed(filtered[-limit:])))

    def get_system_snapshot(self):
        with self.lock:
            occupied_count = sum(1 for values in self.parking_spaces.values() if values["occupied"])
            total_count = len(self.parking_spaces)
            return {
                "server_time": utcnow_iso(),
                "default_device_id": self.default_device_id,
                "parking_spaces": copy.deepcopy(self.parking_spaces),
                "devices": [self._device_snapshot_locked(device_id) for device_id in sorted(self.devices.keys())],
                "recent_commands": copy.deepcopy(list(reversed(self.commands[-20:]))),
                "summary": {
                    "total_spaces": total_count,
                    "occupied_spaces": occupied_count,
                    "available_spaces": total_count - occupied_count,
                },
            }

    def update_heartbeat(self, device_id, payload):
        name = payload.get("name")
        with self.lock:
            if device_id not in self.devices:
                self.devices[device_id] = self._device_template(device_id, name=name)

            device = self.devices[device_id]
            if name:
                device["name"] = name
            device["status"] = normalize_robot_status(
                payload.get("robot_status") or payload.get("status"),
                default=device.get("status", "Pending"),
            )
            device["camera_on"] = coerce_bool(payload.get("camera_on"), default=device.get("camera_on", False))
            device["stream_enabled"] = coerce_bool(
                payload.get("stream_enabled"),
                default=device.get("stream_enabled", False),
            )
            device["last_seen_at"] = utcnow_iso()
            device["last_heartbeat"] = payload
            device["updated_at"] = utcnow_iso()

            snapshot = self._device_snapshot_locked(device_id)
            self._persist_state_locked()
            self._emit_event_locked("device.updated", {"device_id": device_id})
            self.command_condition.notify_all()
            return snapshot

    def update_telemetry(self, device_id, telemetry):
        with self.lock:
            if device_id not in self.devices:
                self.devices[device_id] = self._device_template(device_id)

            device = self.devices[device_id]
            device["last_seen_at"] = utcnow_iso()
            device["status"] = normalize_robot_status(
                telemetry.get("robot_status"),
                default=device.get("status", "Pending"),
            )
            device["camera_on"] = coerce_bool(telemetry.get("camera_on"), default=device.get("camera_on", False))
            device["stream_enabled"] = coerce_bool(
                telemetry.get("stream_enabled"),
                default=device.get("stream_enabled", False),
            )

            applied_spaces = []
            enriched_detections = []
            detections = telemetry.get("plate_detections") or []
            sorted_detections = sorted(
                detections,
                key=lambda item: parse_timestamp(item.get("detected_at")) or datetime.min.replace(tzinfo=timezone.utc),
            )
            for detection in sorted_detections:
                enriched_detection, changed_spaces = self._apply_plate_detection_locked(
                    device_id,
                    detection,
                    telemetry_timestamp=telemetry.get("timestamp"),
                )
                enriched_detections.append(enriched_detection)
                applied_spaces.extend(changed_spaces)

            stored_telemetry = copy.deepcopy(telemetry or {})
            stored_telemetry["plate_detections"] = enriched_detections
            stored_telemetry["latest_detection"] = select_latest_detection(enriched_detections)
            device["last_telemetry"] = stored_telemetry
            device["latest_detection"] = copy.deepcopy(stored_telemetry["latest_detection"])
            device["updated_at"] = utcnow_iso()

            snapshot = self._device_snapshot_locked(device_id)
            self._persist_state_locked()
            self._emit_event_locked("device.updated", {"device_id": device_id})
            if applied_spaces:
                unique_spaces = list(dict.fromkeys(applied_spaces))
                self._emit_event_locked("parking.updated", {"spaces": unique_spaces})
            return {
                "device": snapshot,
                "updated_spaces": list(dict.fromkeys(applied_spaces)),
            }

    def _distance_between_points_locked(self, lat1, lon1, lat2, lon2):
        radius_meters = 6371000

        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)

        a_value = (
            math.sin(delta_lat / 2) ** 2
            + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
        )
        c_value = 2 * math.asin(math.sqrt(a_value))
        return radius_meters * c_value

    def _find_nearest_space_locked(self, latitude, longitude):
        closest_space_id = None
        closest_distance = None

        for candidate_space_id, values in self.parking_spaces.items():
            candidate_distance = self._distance_between_points_locked(
                latitude,
                longitude,
                values["latitude"],
                values["longitude"],
            )
            if closest_distance is None or candidate_distance < closest_distance:
                closest_space_id = candidate_space_id
                closest_distance = candidate_distance

        return closest_space_id, closest_distance

    def _resolve_space_id_locked(self, latitude, longitude):
        if latitude is None or longitude is None:
            return None, None

        direct_match = self.find_matching_space(latitude, longitude, offset_meters=self.space_resolution_offset_meters)
        if direct_match:
            distance = self._distance_between_points_locked(
                latitude,
                longitude,
                self.parking_spaces[direct_match]["latitude"],
                self.parking_spaces[direct_match]["longitude"],
            )
            return direct_match, distance

        closest_space_id, closest_distance = self._find_nearest_space_locked(latitude, longitude)
        if closest_space_id and closest_distance is not None and closest_distance <= self.space_resolution_offset_meters:
            return closest_space_id, closest_distance
        return None, closest_distance

    def _find_space_for_plate_locked(self, plate_text):
        if not plate_text:
            return None

        for candidate_space_id, values in self.parking_spaces.items():
            vehicle_data = values.get("vehicle_data") or {}
            if vehicle_data.get("license_plate") == plate_text:
                return candidate_space_id
        return None

    def _clear_space_locked(self, space_id):
        if space_id in self.parking_spaces:
            self.parking_spaces[space_id]["occupied"] = False
            self.parking_spaces[space_id]["vehicle_data"] = None

    def _is_newer_or_equal_event(self, next_time, current_time):
        next_timestamp = parse_timestamp(next_time)
        current_timestamp = parse_timestamp(current_time)

        if next_timestamp and current_timestamp:
            return next_timestamp >= current_timestamp
        if next_timestamp:
            return True
        return current_time in (None, "")

    def _apply_plate_detection_locked(self, device_id, detection, telemetry_timestamp=None):
        telemetry_timestamp = telemetry_timestamp or utcnow_iso()
        normalized_detection = copy.deepcopy(detection or {})
        gps = normalized_detection.get("gps") or {}
        latitude = gps.get("lat")
        longitude = gps.get("lon")
        detected_at = normalized_detection.get("detected_at") or telemetry_timestamp
        plate_text = normalized_detection.get("plate_text")

        resolved_space_id, resolved_distance_meters = self._resolve_space_id_locked(latitude, longitude)
        normalized_detection["resolved_space_id"] = resolved_space_id
        normalized_detection["resolved_distance_meters"] = (
            round(resolved_distance_meters, 3) if resolved_distance_meters is not None else None
        )

        if not resolved_space_id:
            return normalized_detection, []

        target_space = self.parking_spaces[resolved_space_id]
        target_vehicle = target_space.get("vehicle_data") or {}
        if not self._is_newer_or_equal_event(detected_at, target_vehicle.get("time")):
            return normalized_detection, []

        changed_spaces = []
        previous_space_id = self._find_space_for_plate_locked(plate_text)
        if previous_space_id and previous_space_id != resolved_space_id:
            previous_vehicle = self.parking_spaces[previous_space_id].get("vehicle_data") or {}
            if self._is_newer_or_equal_event(detected_at, previous_vehicle.get("time")):
                self._clear_space_locked(previous_space_id)
                changed_spaces.append(previous_space_id)

        target_space["occupied"] = True
        target_space["vehicle_data"] = {
            "license_plate": plate_text,
            "time": detected_at,
            "detected_at": detected_at,
            "latitude": latitude,
            "longitude": longitude,
            "gps": {
                "lat": latitude,
                "lon": longitude,
            },
            "confidence": normalized_detection.get("confidence"),
            "device_id": device_id,
            "image_id": normalized_detection.get("image_id"),
            "event_id": normalized_detection.get("event_id"),
            "source_camera": normalized_detection.get("source_camera"),
            "resolved_space_id": resolved_space_id,
        }
        changed_spaces.append(resolved_space_id)
        return normalized_detection, changed_spaces

    def _apply_parking_update_locked(self, update):
        space_id = update.get("space_id")
        if not space_id:
            latitude = update.get("latitude")
            longitude = update.get("longitude")
            if latitude is None or longitude is None:
                return None
            space_id, _ = self._resolve_space_id_locked(latitude, longitude)

        if not space_id or space_id not in self.parking_spaces:
            return None

        occupied = coerce_bool(update.get("occupied"), default=True)
        captured_at = update.get("captured_at") or utcnow_iso()

        self.parking_spaces[space_id]["occupied"] = occupied
        if occupied:
            self.parking_spaces[space_id]["vehicle_data"] = {
                "license_plate": update.get("license_plate"),
                "time": captured_at,
                "detected_at": captured_at,
                "latitude": update.get("latitude"),
                "longitude": update.get("longitude"),
                "gps": {
                    "lat": update.get("latitude"),
                    "lon": update.get("longitude"),
                },
                "confidence": update.get("confidence"),
                "device_id": update.get("device_id"),
                "image_id": update.get("image_id"),
                "event_id": update.get("event_id"),
                "source_camera": update.get("source_camera"),
                "resolved_space_id": space_id,
            }
        else:
            self.parking_spaces[space_id]["vehicle_data"] = None

        return space_id

    def apply_manual_parking_update(self, update):
        with self.lock:
            changed_space = self._apply_parking_update_locked(update)
            if not changed_space:
                return None
            self._persist_state_locked()
            self._emit_event_locked("parking.updated", {"spaces": [changed_space]})
            return copy.deepcopy(self.parking_spaces[changed_space])

    def toggle_space(self, space_id):
        with self.lock:
            if space_id not in self.parking_spaces:
                return None

            current = self.parking_spaces[space_id]
            current["occupied"] = not current["occupied"]
            if not current["occupied"]:
                current["vehicle_data"] = None
            elif not current.get("vehicle_data"):
                captured_at = utcnow_iso()
                current["vehicle_data"] = {
                    "license_plate": "MANUAL",
                    "time": captured_at,
                    "detected_at": captured_at,
                    "latitude": current["latitude"],
                    "longitude": current["longitude"],
                    "gps": {
                        "lat": current["latitude"],
                        "lon": current["longitude"],
                    },
                    "resolved_space_id": space_id,
                }

            self._persist_state_locked()
            self._emit_event_locked("parking.updated", {"spaces": [space_id]})
            return copy.deepcopy(current)

    def queue_command(self, device_id, command_type, payload=None, requested_by="operator"):
        payload = payload or {}
        with self.lock:
            if device_id not in self.devices:
                self.devices[device_id] = self._device_template(device_id)

            command = {
                "id": self.command_sequence,
                "device_id": device_id,
                "command": command_type,
                "payload": payload,
                "requested_by": requested_by,
                "status": "queued",
                "created_at": utcnow_iso(),
                "dispatched_at": None,
                "completed_at": None,
                "result": None,
            }
            self.command_sequence += 1
            self.commands.append(command)
            self._persist_state_locked()
            self._emit_event_locked("command.updated", {"command_id": command["id"], "status": "queued"})
            self.command_condition.notify_all()
            return copy.deepcopy(command)

    def get_next_command(self, device_id, wait_seconds=0):
        with self.command_condition:
            deadline = time.time() + max(wait_seconds, 0)
            while True:
                if device_id not in self.devices:
                    self.devices[device_id] = self._device_template(device_id)

                self.devices[device_id]["last_seen_at"] = utcnow_iso()
                self.devices[device_id]["updated_at"] = utcnow_iso()

                for command in self.commands:
                    if command["device_id"] == device_id and command["status"] == "queued":
                        command["status"] = "dispatched"
                        command["dispatched_at"] = utcnow_iso()
                        self._persist_state_locked()
                        self._emit_event_locked(
                            "command.updated",
                            {"command_id": command["id"], "status": "dispatched"},
                        )
                        return copy.deepcopy(command)

                remaining = deadline - time.time()
                if remaining <= 0:
                    self._persist_state_locked()
                    return None

                self.command_condition.wait(timeout=min(1.0, remaining))

    def acknowledge_command(self, device_id, command_id, success, result=None):
        with self.lock:
            for command in self.commands:
                if command["device_id"] == device_id and command["id"] == command_id:
                    command["status"] = "completed" if success else "failed"
                    command["completed_at"] = utcnow_iso()
                    command["result"] = result or {}
                    if device_id in self.devices:
                        self.devices[device_id]["last_command_result"] = {
                            "command_id": command_id,
                            "status": command["status"],
                            "result": result or {},
                            "completed_at": command["completed_at"],
                        }
                    self._persist_state_locked()
                    self._emit_event_locked(
                        "command.updated",
                        {"command_id": command_id, "status": command["status"]},
                    )
                    return copy.deepcopy(command)
        return None

    def save_image(self, device_id, filename, image_bytes, metadata=None, content_type="image/jpeg"):
        metadata = metadata or {}
        with self.lock:
            if device_id not in self.devices:
                self.devices[device_id] = self._device_template(device_id)

            suffix = Path(secure_filename(filename or "capture.jpg")).suffix or ".jpg"
            upload_id = uuid.uuid4().hex
            file_path = self.images_dir / f"{upload_id}{suffix}"
            file_path.write_bytes(image_bytes)

            record = {
                "id": upload_id,
                "device_id": device_id,
                "filename": file_path.name,
                "original_filename": filename,
                "path": str(file_path),
                "content_type": content_type,
                "metadata": metadata,
                "created_at": utcnow_iso(),
                "url": f"/api/uploads/{upload_id}",
            }
            self.uploads[upload_id] = record

            device = self.devices[device_id]
            device["latest_image_id"] = upload_id
            device["latest_image_path"] = str(file_path)
            device["recent_image_ids"] = [upload_id] + [
                image_id for image_id in device.get("recent_image_ids", []) if image_id != upload_id
            ]
            device["recent_image_ids"] = device["recent_image_ids"][:10]
            device["updated_at"] = utcnow_iso()

            self._persist_state_locked()
            self._emit_event_locked("image.uploaded", {"device_id": device_id, "image_id": upload_id})
            return copy.deepcopy(record)

    def save_frame(self, device_id, filename, frame_bytes, metadata=None):
        metadata = metadata or {}
        with self.frame_condition:
            if device_id not in self.devices:
                self.devices[device_id] = self._device_template(device_id)

            suffix = Path(secure_filename(filename or "frame.jpg")).suffix or ".jpg"
            file_path = self.frames_dir / f"{device_id}_latest{suffix}"
            file_path.write_bytes(frame_bytes)

            device = self.devices[device_id]
            device["latest_frame_path"] = str(file_path)
            device["latest_frame_bytes"] = frame_bytes
            device["latest_frame_updated_at"] = utcnow_iso()
            device["latest_frame_version"] = int(device.get("latest_frame_version", 0)) + 1
            device["stream_enabled"] = coerce_bool(metadata.get("stream_enabled"), default=True)
            device["last_seen_at"] = utcnow_iso()
            device["updated_at"] = utcnow_iso()

            self._persist_state_locked()
            self._emit_event_locked("frame.updated", {"device_id": device_id})
            self.frame_condition.notify_all()

            return {
                "device_id": device_id,
                "frame_version": device["latest_frame_version"],
                "updated_at": device["latest_frame_updated_at"],
                "stream_url": f"/api/devices/{device_id}/stream.mjpeg",
                "latest_frame_url": f"/api/devices/{device_id}/latest-frame",
            }

    def wait_for_next_frame(self, device_id, last_version=0, timeout=25):
        with self.frame_condition:
            deadline = time.time() + max(timeout, 0)
            while True:
                device = self.devices.get(device_id)
                if device and device.get("latest_frame_bytes") and device.get("latest_frame_version", 0) > last_version:
                    return {
                        "frame_bytes": device["latest_frame_bytes"],
                        "frame_version": device["latest_frame_version"],
                    }

                remaining = deadline - time.time()
                if remaining <= 0:
                    return None

                self.frame_condition.wait(timeout=min(1.0, remaining))

    def get_latest_frame(self, device_id):
        with self.lock:
            device = self.devices.get(device_id)
            if not device or not device.get("latest_frame_bytes"):
                return None
            return {
                "frame_bytes": device["latest_frame_bytes"],
                "content_type": "image/jpeg",
                "updated_at": device.get("latest_frame_updated_at"),
            }

    def get_upload(self, upload_id):
        with self.lock:
            record = self.uploads.get(upload_id)
            if not record:
                return None
            return copy.deepcopy(record)
