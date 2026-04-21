import copy
import json
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.utils import secure_filename


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


def first_present(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


class BackendState:
    OBSERVATION_HISTORY_LIMIT = 80

    def __init__(self, parking_spaces, find_matching_space, runtime_dir="runtime_data", default_device_id="jetson-01"):
        self.lock = threading.RLock()
        self.command_condition = threading.Condition(self.lock)
        self.frame_condition = threading.Condition(self.lock)

        self.parking_spaces = parking_spaces
        self.find_matching_space = find_matching_space
        self.devices = {}
        self.commands = []
        self.uploads = {}
        self.observations = {}
        self.subscribers = set()
        self.command_sequence = 1
        self.default_device_id = default_device_id

        self.runtime_dir = Path(runtime_dir)
        self.images_dir = self.runtime_dir / "images"
        self.frames_dir = self.runtime_dir / "frames"
        self.observations_dir = self.runtime_dir / "observations"
        self.state_file = self.runtime_dir / "state.json"

        self._ensure_runtime_dirs()
        self._load_state()
        self._ensure_parking_space_defaults()
        self.ensure_device(default_device_id, name="Jetson Primary")

    def _ensure_runtime_dirs(self):
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.observations_dir.mkdir(parents=True, exist_ok=True)

    def _ensure_parking_space_defaults(self):
        for values in self.parking_spaces.values():
            occupied = bool(values.get("occupied"))
            values.setdefault("status", "OCCUPIED" if occupied else "EMPTY")
            values.setdefault("decision_confidence", 1.0 if occupied else 0.9)
            values.setdefault("decision_reason", None if occupied else "no_valid_detection")
            values.setdefault("source_detection_time", None)
            values.setdefault("last_resolved_at", None)

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
            self.parking_spaces[space_id]["status"] = values.get("status") or (
                "OCCUPIED" if values.get("occupied") else "EMPTY"
            )
            self.parking_spaces[space_id]["decision_confidence"] = values.get("decision_confidence")
            self.parking_spaces[space_id]["decision_reason"] = values.get("decision_reason")
            self.parking_spaces[space_id]["source_detection_time"] = values.get("source_detection_time")
            self.parking_spaces[space_id]["last_resolved_at"] = values.get("last_resolved_at")

        self.devices = persisted.get("devices", {})
        self.commands = persisted.get("commands", [])
        self.uploads = persisted.get("uploads", {})
        self.observations = persisted.get("observations", {})
        self.command_sequence = persisted.get("command_sequence", 1)

        for device in self.devices.values():
            frame_path = device.get("latest_frame_path")
            device.setdefault("latest_frame_version", 0)
            device.setdefault("recent_image_ids", [])
            device.setdefault("last_heartbeat", {})
            device.setdefault("last_telemetry", {})
            device.setdefault("camera_on", False)
            device.setdefault("stream_enabled", False)
            device.setdefault("latest_image_id", None)
            device.setdefault("latest_image_path", None)
            device.setdefault("last_command_result", None)
            device.setdefault("latest_observation_id", None)
            device.setdefault("recent_observation_ids", [])
            device.setdefault("latest_stream_by_source", {})

            for source in device["latest_stream_by_source"].values():
                source_frame_path = source.get("frame_path")
                if source_frame_path and Path(source_frame_path).exists():
                    try:
                        source["_frame_bytes"] = Path(source_frame_path).read_bytes()
                    except OSError:
                        source["_frame_bytes"] = None
                else:
                    source["_frame_bytes"] = None

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
            for source in device_copy.get("latest_stream_by_source", {}).values():
                source.pop("_frame_bytes", None)
            devices[device_id] = device_copy

        return {
            "parking_spaces": {
                space_id: {
                    "occupied": values["occupied"],
                    "vehicle_data": values["vehicle_data"],
                    "status": values.get("status"),
                    "decision_confidence": values.get("decision_confidence"),
                    "decision_reason": values.get("decision_reason"),
                    "source_detection_time": values.get("source_detection_time"),
                    "last_resolved_at": values.get("last_resolved_at"),
                }
                for space_id, values in self.parking_spaces.items()
            },
            "devices": devices,
            "commands": self.commands[-200:],
            "uploads": self.uploads,
            "observations": self.observations,
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
            "status": "waiting",
            "camera_on": False,
            "stream_enabled": False,
            "last_seen_at": None,
            "last_heartbeat": {},
            "last_telemetry": {},
            "latest_frame_path": None,
            "latest_frame_updated_at": None,
            "latest_frame_version": 0,
            "latest_frame_bytes": None,
            "latest_image_id": None,
            "latest_image_path": None,
            "recent_image_ids": [],
            "last_command_result": None,
            "latest_observation_id": None,
            "recent_observation_ids": [],
            "latest_stream_by_source": {},
            "updated_at": utcnow_iso(),
        }

    def _observation_file_name(self, created_at, observation_id):
        safe_timestamp = created_at.replace(":", "").replace("-", "").replace("+00:00", "Z")
        safe_timestamp = safe_timestamp.replace(".", "_")
        return f"{safe_timestamp}_{observation_id[:8]}.json"

    def _normalize_detection_items(self, telemetry):
        if not isinstance(telemetry, dict):
            return []

        detection_candidates = (
            telemetry.get("plate_detections"),
            telemetry.get("detections"),
            telemetry.get("license_plate_detections"),
        )
        for candidate in detection_candidates:
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        return []

    def _build_observation_summary(self, device_id, payload, created_at, source):
        payload = payload if isinstance(payload, dict) else {}
        telemetry = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else payload
        telemetry = telemetry if isinstance(telemetry, dict) else {}
        detections = self._normalize_detection_items(telemetry)
        resolution_items = telemetry.get("space_resolution") if isinstance(telemetry.get("space_resolution"), list) else []
        parking_updates = payload.get("parking_updates") or payload.get("events") or []
        parking_updates = [item for item in parking_updates if isinstance(item, dict)]

        primary_detection = detections[0] if detections else {}
        primary_location = primary_detection.get("location") if isinstance(primary_detection.get("location"), dict) else {}
        if not primary_location:
            primary_location = primary_detection.get("gps") if isinstance(primary_detection.get("gps"), dict) else {}
        primary_update = parking_updates[0] if parking_updates else {}
        lot_status = telemetry.get("lot_status") if isinstance(telemetry.get("lot_status"), dict) else {}
        plate_status = lot_status.get("plate") if isinstance(lot_status.get("plate"), dict) else {}
        gps_status = lot_status.get("gps") if isinstance(lot_status.get("gps"), dict) else {}
        power_status = telemetry.get("power") if isinstance(telemetry.get("power"), dict) else {}
        resolved_occupied = next(
            (
                item
                for item in resolution_items
                if isinstance(item, dict) and item.get("status") == "OCCUPIED" and item.get("plate_read")
            ),
            None,
        )
        resolved_candidate = resolved_occupied or next(
            (item for item in resolution_items if isinstance(item, dict) and item.get("status")),
            None,
        )
        resolved_location = (
            resolved_candidate.get("location")
            if isinstance(resolved_candidate, dict) and isinstance(resolved_candidate.get("location"), dict)
            else {}
        )

        plate_text = first_present(
            resolved_occupied.get("plate_read") if resolved_occupied else None,
            primary_detection.get("plate_text"),
            primary_detection.get("plate_read"),
            primary_detection.get("text"),
            primary_detection.get("license_plate"),
            telemetry.get("detected_plate"),
            telemetry.get("plate"),
            telemetry.get("license_plate"),
            primary_update.get("license_plate"),
            plate_status.get("text"),
        )
        confidence = first_present(
            resolved_candidate.get("confidence") if resolved_candidate else None,
            primary_detection.get("confidence"),
            primary_detection.get("confidence_level"),
            telemetry.get("confidence"),
            primary_update.get("confidence"),
            plate_status.get("confidence"),
        )
        timestamp = first_present(
            resolved_candidate.get("source_detection_time") if resolved_candidate else None,
            primary_detection.get("timestamp"),
            primary_detection.get("time"),
            telemetry.get("timestamp"),
            telemetry.get("sent_at_utc"),
            primary_update.get("captured_at"),
            lot_status.get("observed_at_utc"),
            payload.get("timestamp"),
            created_at,
        )
        latitude = first_present(
            resolved_location.get("lat"),
            primary_detection.get("latitude"),
            primary_location.get("lat"),
            telemetry.get("latitude"),
            telemetry.get("lat"),
            primary_update.get("latitude"),
            gps_status.get("lat"),
        )
        longitude = first_present(
            resolved_location.get("lon"),
            primary_detection.get("longitude"),
            primary_location.get("lon"),
            telemetry.get("longitude"),
            telemetry.get("lon"),
            primary_update.get("longitude"),
            gps_status.get("lon"),
        )
        space_id = first_present(
            resolved_candidate.get("space_id") if resolved_candidate else None,
            primary_detection.get("space_id"),
            primary_update.get("space_id"),
            telemetry.get("space_id"),
            lot_status.get("space_id"),
        )
        space_status = first_present(
            resolved_candidate.get("status") if resolved_candidate else None,
            "OCCUPIED" if space_id and plate_text else None,
        )
        robot_status = first_present(
            telemetry.get("robot_status"),
            telemetry.get("status"),
            (telemetry.get("local_status") or {}).get("state") if isinstance(telemetry.get("local_status"), dict) else None,
            lot_status.get("status"),
        )

        return {
            "device_id": device_id,
            "source": source,
            "timestamp": timestamp,
            "plate_text": plate_text,
            "confidence": confidence,
            "space_id": space_id,
            "space_status": space_status,
            "latitude": latitude,
            "longitude": longitude,
            "robot_status": robot_status,
            "battery_channel": power_status.get("battery_channel"),
            "pack_voltage_v": power_status.get("pack_voltage_v"),
            "shutdown_threshold_v": power_status.get("shutdown_threshold_v"),
            "power_action": power_status.get("power_action"),
            "will_shutdown": power_status.get("will_shutdown"),
            "power_status": power_status.get("status"),
            "power_message": power_status.get("message"),
            "low_voltage_duration_sec": power_status.get("low_voltage_duration_sec"),
            "detection_count": len(detections),
            "parking_update_count": len(parking_updates),
        }

    def _observation_metadata_locked(self, observation_id):
        record = self.observations.get(observation_id)
        if not record:
            return None

        metadata = copy.deepcopy(record)
        metadata["detail_url"] = f"/api/devices/{metadata['device_id']}/observations/{observation_id}"
        metadata["raw_url"] = f"/api/devices/{metadata['device_id']}/observations/{observation_id}/raw"
        return metadata

    def _prune_observations_locked(self, device):
        recent_ids = device.get("recent_observation_ids", [])
        if len(recent_ids) <= self.OBSERVATION_HISTORY_LIMIT:
            return

        retained_ids = recent_ids[: self.OBSERVATION_HISTORY_LIMIT]
        stale_ids = recent_ids[self.OBSERVATION_HISTORY_LIMIT :]
        device["recent_observation_ids"] = retained_ids
        if device.get("latest_observation_id") in stale_ids:
            device["latest_observation_id"] = retained_ids[0] if retained_ids else None

        for observation_id in stale_ids:
            record = self.observations.pop(observation_id, None)
            if not record:
                continue
            path = record.get("path")
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

    def save_observation(self, device_id, payload, source="jetson.telemetry"):
        payload = copy.deepcopy(payload) if isinstance(payload, dict) else {"payload": payload}
        created_at = utcnow_iso()
        observation_id = uuid.uuid4().hex
        file_name = self._observation_file_name(created_at, observation_id)
        device_dir = self.observations_dir / device_id
        device_dir.mkdir(parents=True, exist_ok=True)
        file_path = device_dir / file_name
        summary = self._build_observation_summary(device_id, payload, created_at, source)
        document = {
            "id": observation_id,
            "device_id": device_id,
            "source": source,
            "created_at": created_at,
            "summary": summary,
            "payload": payload,
        }
        file_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

        with self.lock:
            if device_id not in self.devices:
                self.devices[device_id] = self._device_template(device_id)

            record = {
                "id": observation_id,
                "device_id": device_id,
                "filename": file_name,
                "path": str(file_path),
                "created_at": created_at,
                "source": source,
                "summary": summary,
            }
            self.observations[observation_id] = record

            device = self.devices[device_id]
            device["latest_observation_id"] = observation_id
            device["recent_observation_ids"] = [
                observation_id,
                *[value for value in device.get("recent_observation_ids", []) if value != observation_id],
            ]
            self._prune_observations_locked(device)
            device["updated_at"] = utcnow_iso()

            self._persist_state_locked()
            self._emit_event_locked("observation.saved", {"device_id": device_id, "observation_id": observation_id})
            return self._observation_metadata_locked(observation_id)

    def get_observations_for_device(self, device_id, limit=30):
        with self.lock:
            device = self.devices.get(device_id)
            if not device:
                return []

            items = []
            for observation_id in device.get("recent_observation_ids", [])[:limit]:
                metadata = self._observation_metadata_locked(observation_id)
                if metadata:
                    items.append(metadata)
            return items

    def get_observation(self, device_id, observation_id):
        with self.lock:
            metadata = self._observation_metadata_locked(observation_id)
            if not metadata or metadata["device_id"] != device_id:
                return None

            file_path = metadata.get("path")
        if not file_path:
            return None

        try:
            document = json.loads(Path(file_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        return {
            "observation": metadata,
            "document": document,
        }

    def get_observation_file_path(self, device_id, observation_id):
        with self.lock:
            record = self.observations.get(observation_id)
            if not record or record.get("device_id") != device_id:
                return None
            return record.get("path")

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

        sources_snapshot = {}
        for source_id, source in device.get("latest_stream_by_source", {}).items():
            s = {k: v for k, v in source.items() if k != "_frame_bytes"}
            s["snapshot_url"] = f"/api/devices/{device_id}/sources/{source_id}/snapshot"
            s["mjpeg_url"] = f"/api/devices/{device_id}/sources/{source_id}/stream.mjpeg"
            sources_snapshot[source_id] = s
        device["latest_stream_by_source"] = sources_snapshot

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
        device["observation_count"] = len(device.get("recent_observation_ids", []))
        if device.get("latest_observation_id"):
            device["latest_observation_url"] = (
                f"/api/devices/{device_id}/observations/{device['latest_observation_id']}"
            )
        else:
            device["latest_observation_url"] = None
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
            uncertain_count = sum(1 for values in self.parking_spaces.values() if values.get("status") == "UNCERTAIN")
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
                    "uncertain_spaces": uncertain_count,
                    "available_spaces": total_count - occupied_count - uncertain_count,
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
            device["status"] = payload.get("status", "online")
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

    def update_telemetry(self, device_id, telemetry, parking_updates=None):
        parking_updates = parking_updates or []
        with self.lock:
            if device_id not in self.devices:
                self.devices[device_id] = self._device_template(device_id)

            device = self.devices[device_id]
            device["last_seen_at"] = utcnow_iso()
            device["last_telemetry"] = telemetry or {}
            device["updated_at"] = utcnow_iso()

            applied_spaces = []
            for update in parking_updates:
                changed_space = self._apply_parking_update_locked(update)
                if changed_space:
                    applied_spaces.append(changed_space)

            snapshot = self._device_snapshot_locked(device_id)
            self._persist_state_locked()
            self._emit_event_locked("device.updated", {"device_id": device_id})
            if applied_spaces:
                self._emit_event_locked("parking.updated", {"spaces": applied_spaces})
            return {
                "device": snapshot,
                "updated_spaces": applied_spaces,
            }

    def apply_space_decisions(self, device_id, space_decisions, telemetry=None):
        telemetry = telemetry or {}
        with self.lock:
            if device_id not in self.devices:
                self.devices[device_id] = self._device_template(device_id)

            updated_spaces = []
            for decision in space_decisions:
                if hasattr(decision, "to_dict"):
                    payload = decision.to_dict()
                else:
                    payload = dict(decision)

                space_id = payload.get("space_id")
                if not space_id or space_id not in self.parking_spaces:
                    continue

                status = payload.get("status") or "UNCERTAIN"
                confidence = payload.get("confidence")
                reason = payload.get("reason")
                source_detection_time = payload.get("source_detection_time") or utcnow_iso()
                location = payload.get("location") or {}

                space = self.parking_spaces[space_id]
                space["status"] = status
                space["decision_confidence"] = confidence
                space["decision_reason"] = reason
                space["source_detection_time"] = source_detection_time
                space["last_resolved_at"] = utcnow_iso()

                if status == "OCCUPIED" and payload.get("plate_read"):
                    space["occupied"] = True
                    space["vehicle_data"] = {
                        "license_plate": payload.get("plate_read"),
                        "time": source_detection_time,
                        "latitude": location.get("lat"),
                        "longitude": location.get("lon"),
                        "confidence": confidence,
                        "device_id": device_id,
                        "image_id": payload.get("image_id"),
                        "image_url": payload.get("image_url")
                        or (f"/api/uploads/{payload.get('image_id')}" if payload.get("image_id") else None),
                        "space_status": status,
                        "reason": reason,
                    }
                else:
                    space["occupied"] = False
                    space["vehicle_data"] = None

                updated_spaces.append(space_id)

            device = self.devices[device_id]
            device["last_seen_at"] = utcnow_iso()
            device["last_telemetry"] = telemetry or {}
            device["updated_at"] = utcnow_iso()

            snapshot = self._device_snapshot_locked(device_id)
            self._persist_state_locked()
            self._emit_event_locked("device.updated", {"device_id": device_id})
            if updated_spaces:
                self._emit_event_locked("parking.updated", {"spaces": updated_spaces})
            return {
                "device": snapshot,
                "updated_spaces": updated_spaces,
            }

    def _apply_parking_update_locked(self, update):
        space_id = update.get("space_id")
        if not space_id:
            latitude = update.get("latitude")
            longitude = update.get("longitude")
            if latitude is None or longitude is None:
                return None
            space_id = self.find_matching_space(latitude, longitude, offset_meters=1)

        if not space_id or space_id not in self.parking_spaces:
            return None

        occupied = coerce_bool(update.get("occupied"), default=True)
        captured_at = update.get("captured_at") or utcnow_iso()

        self.parking_spaces[space_id]["occupied"] = occupied
        self.parking_spaces[space_id]["status"] = "OCCUPIED" if occupied else "EMPTY"
        self.parking_spaces[space_id]["decision_confidence"] = update.get("confidence")
        self.parking_spaces[space_id]["decision_reason"] = None if occupied else "legacy_empty_update"
        self.parking_spaces[space_id]["source_detection_time"] = captured_at
        self.parking_spaces[space_id]["last_resolved_at"] = utcnow_iso()
        if occupied:
            self.parking_spaces[space_id]["vehicle_data"] = {
                "license_plate": update.get("license_plate"),
                "time": captured_at,
                "latitude": update.get("latitude"),
                "longitude": update.get("longitude"),
                "confidence": update.get("confidence"),
                "device_id": update.get("device_id"),
                "image_id": update.get("image_id"),
                "space_status": "OCCUPIED",
                "reason": None,
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
                current["status"] = "EMPTY"
                current["decision_confidence"] = 1.0
                current["decision_reason"] = "manual_toggle"
                current["source_detection_time"] = utcnow_iso()
                current["last_resolved_at"] = utcnow_iso()
            elif not current.get("vehicle_data"):
                current["vehicle_data"] = {
                    "license_plate": "MANUAL",
                    "time": utcnow_iso(),
                    "latitude": current["latitude"],
                    "longitude": current["longitude"],
                    "space_status": "OCCUPIED",
                    "reason": "manual_toggle",
                }
                current["status"] = "OCCUPIED"
                current["decision_confidence"] = 1.0
                current["decision_reason"] = None
                current["source_detection_time"] = current["vehicle_data"]["time"]
                current["last_resolved_at"] = utcnow_iso()

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

    def save_source_frame(self, device_id, source_id, meta, frame_bytes):
        with self.frame_condition:
            if device_id not in self.devices:
                self.devices[device_id] = self._device_template(device_id)

            device = self.devices[device_id]
            sources = device.setdefault("latest_stream_by_source", {})

            safe_id = "".join(c for c in source_id if c.isalnum() or c in "-_")
            file_path = self.frames_dir / f"{device_id}_src_{safe_id}_latest.jpg"
            file_path.write_bytes(frame_bytes)

            prev_version = sources.get(source_id, {}).get("frame_version", 0)
            sources[source_id] = {
                "source_id": source_id,
                "camera_role": meta.get("camera_role", "processor"),
                "label": meta.get("label", source_id),
                "frame_path": str(file_path),
                "frame_updated_at": utcnow_iso(),
                "frame_version": int(prev_version) + 1,
                "_frame_bytes": frame_bytes,
            }
            device["last_seen_at"] = utcnow_iso()
            device["updated_at"] = utcnow_iso()

            self._persist_state_locked()
            self._emit_event_locked("frame.updated", {"device_id": device_id, "source_id": source_id})
            self.frame_condition.notify_all()

            return {
                "device_id": device_id,
                "source_id": source_id,
                "frame_version": sources[source_id]["frame_version"],
                "updated_at": sources[source_id]["frame_updated_at"],
                "snapshot_url": f"/api/devices/{device_id}/sources/{source_id}/snapshot",
                "mjpeg_url": f"/api/devices/{device_id}/sources/{source_id}/stream.mjpeg",
            }

    def get_source_frame_bytes(self, device_id, source_id):
        with self.lock:
            device = self.devices.get(device_id)
            if not device:
                return None
            source = device.get("latest_stream_by_source", {}).get(source_id)
            if not source or not source.get("_frame_bytes"):
                return None
            return {
                "frame_bytes": source["_frame_bytes"],
                "content_type": "image/jpeg",
                "updated_at": source.get("frame_updated_at"),
            }

    def wait_for_next_source_frame(self, device_id, source_id, last_version=0, timeout=25):
        with self.frame_condition:
            deadline = time.time() + max(timeout, 0)
            while True:
                device = self.devices.get(device_id)
                if device:
                    source = device.get("latest_stream_by_source", {}).get(source_id)
                    if source and source.get("_frame_bytes") and source.get("frame_version", 0) > last_version:
                        return {
                            "frame_bytes": source["_frame_bytes"],
                            "frame_version": source["frame_version"],
                        }
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self.frame_condition.wait(timeout=min(1.0, remaining))
