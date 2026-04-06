import copy
from functools import cmp_to_key
import json
import math
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.utils import secure_filename

from gps_mapping import build_segment_mapper
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

    def __init__(
        self,
        parking_spaces,
        find_matching_space,
        runtime_dir="runtime_data",
        default_device_id="jetson-01",
        route_mapper=None,
        gps_route_calibration_enabled=True,
        route_mapping_max_distance_meters=30,
        bbox_area_priority_enabled=True,
        bbox_area_priority_weight=1.0,
        bbox_area_similarity_ratio=0.1,
    ):
        self.lock = threading.RLock()
        self.command_condition = threading.Condition(self.lock)
        self.frame_condition = threading.Condition(self.lock)

        self.parking_spaces = parking_spaces
        self.find_matching_space = find_matching_space
        self.route_mapper = route_mapper if gps_route_calibration_enabled else None
        if gps_route_calibration_enabled and self.route_mapper is None:
            self.route_mapper = build_segment_mapper()
        self.devices = {}
        self.commands = []
        self.uploads = {}
        self.observations = {}
        self.subscribers = set()
        self.command_sequence = 1
        self.default_device_id = default_device_id
        self.space_resolution_offset_meters = 12
        self.route_mapping_max_distance_meters = (
            max(0.0, float(route_mapping_max_distance_meters))
            if route_mapping_max_distance_meters is not None
            else None
        )
        self.bbox_area_priority_enabled = bool(bbox_area_priority_enabled)
        self.bbox_area_priority_weight = max(0.0, float(bbox_area_priority_weight or 0))
        self.bbox_area_similarity_ratio = max(0.0, float(bbox_area_similarity_ratio or 0))

        self.runtime_dir = Path(runtime_dir)
        self.images_dir = self.runtime_dir / "images"
        self.frames_dir = self.runtime_dir / "frames"
        self.observations_dir = self.runtime_dir / "observations"
        self.state_file = self.runtime_dir / "state.json"

        self._ensure_runtime_dirs()
        self._load_state()
        self.ensure_device(default_device_id, name="Jetson Primary")

    def _ensure_runtime_dirs(self):
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.observations_dir.mkdir(parents=True, exist_ok=True)

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
        self.observations = persisted.get("observations", {})
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
            device.setdefault("latest_observation_id", None)
            device.setdefault("recent_observation_ids", [])

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
            "latest_observation_id": None,
            "recent_observation_ids": [],
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
        parking_updates = payload.get("parking_updates") or payload.get("events") or []
        parking_updates = [item for item in parking_updates if isinstance(item, dict)]

        primary_detection = detections[0] if detections else {}
        primary_update = parking_updates[0] if parking_updates else {}
        lot_status = telemetry.get("lot_status") if isinstance(telemetry.get("lot_status"), dict) else {}
        plate_status = lot_status.get("plate") if isinstance(lot_status.get("plate"), dict) else {}
        gps_status = lot_status.get("gps") if isinstance(lot_status.get("gps"), dict) else {}

        plate_text = first_present(
            primary_detection.get("plate_text"),
            primary_detection.get("text"),
            primary_detection.get("license_plate"),
            telemetry.get("detected_plate"),
            telemetry.get("plate"),
            telemetry.get("license_plate"),
            primary_update.get("license_plate"),
            plate_status.get("text"),
        )
        confidence = first_present(
            primary_detection.get("confidence"),
            telemetry.get("confidence"),
            primary_update.get("confidence"),
            plate_status.get("confidence"),
        )
        timestamp = first_present(
            primary_detection.get("timestamp"),
            telemetry.get("timestamp"),
            telemetry.get("sent_at_utc"),
            primary_update.get("captured_at"),
            lot_status.get("observed_at_utc"),
            payload.get("timestamp"),
            created_at,
        )
        latitude = first_present(
            primary_detection.get("latitude"),
            telemetry.get("latitude"),
            telemetry.get("lat"),
            primary_update.get("latitude"),
            gps_status.get("lat"),
        )
        longitude = first_present(
            primary_detection.get("longitude"),
            telemetry.get("longitude"),
            telemetry.get("lon"),
            primary_update.get("longitude"),
            gps_status.get("lon"),
        )
        space_id = first_present(
            primary_detection.get("space_id"),
            primary_update.get("space_id"),
            telemetry.get("space_id"),
            lot_status.get("space_id"),
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
            "latitude": latitude,
            "longitude": longitude,
            "robot_status": robot_status,
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
            prepared_detections = self._prepare_detection_batch_locked(detections, telemetry.get("timestamp"))
            for detection_batch in self._group_prepared_detections_locked(prepared_detections):
                reserved_space_ids = set()
                for prepared_detection in self._sort_detection_batch_locked(detection_batch):
                    enriched_detection, changed_spaces = self._apply_plate_detection_locked(
                        device_id,
                        prepared_detection["detection"],
                        telemetry_timestamp=telemetry.get("timestamp"),
                        reserved_space_ids=reserved_space_ids,
                    )
                    resolved_space_id = enriched_detection.get("resolved_space_id")
                    if resolved_space_id:
                        reserved_space_ids.add(resolved_space_id)
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

    def _map_coordinate_locked(self, latitude, longitude):
        if latitude is None or longitude is None or not self.route_mapper:
            return latitude, longitude
        return self.route_mapper.map_point(
            latitude,
            longitude,
            max_distance_meters=self.route_mapping_max_distance_meters,
        )

    def _bbox_metrics_locked(self, detection):
        bbox = detection.get("bbox_xyxy") if isinstance(detection, dict) else None
        if not isinstance(bbox, dict):
            return None, None, None

        x1 = bbox.get("x1")
        y1 = bbox.get("y1")
        x2 = bbox.get("x2")
        y2 = bbox.get("y2")
        if None in {x1, y1, x2, y2}:
            return None, None, None

        width = max(0.0, x2 - x1)
        height = max(0.0, y2 - y1)
        return width, height, width * height

    def _bbox_priority_score_locked(self, bbox_area):
        if not self.bbox_area_priority_enabled or self.bbox_area_priority_weight <= 0 or bbox_area is None:
            return None
        return bbox_area * self.bbox_area_priority_weight

    def _annotate_detection_locked(self, detection, telemetry_timestamp=None):
        normalized_detection = copy.deepcopy(detection or {})
        gps = normalized_detection.get("gps") if isinstance(normalized_detection.get("gps"), dict) else {}
        raw_latitude = gps.get("lat")
        raw_longitude = gps.get("lon")
        mapped_latitude, mapped_longitude = self._map_coordinate_locked(raw_latitude, raw_longitude)
        bbox_width, bbox_height, bbox_area = self._bbox_metrics_locked(normalized_detection)

        # Preserve raw GPS and add a calibrated coordinate so stall lookup can
        # use the route-mapped position without changing ingestion payloads.
        normalized_detection["mapped_gps"] = {
            "lat": mapped_latitude,
            "lon": mapped_longitude,
        }
        normalized_detection["bbox_metrics"] = {
            "width": bbox_width,
            "height": bbox_height,
            "area": bbox_area,
        }
        normalized_detection["bbox_area_priority_score"] = self._bbox_priority_score_locked(bbox_area)
        if not normalized_detection.get("detected_at"):
            normalized_detection["detected_at"] = telemetry_timestamp
        return normalized_detection

    def _prepare_detection_batch_locked(self, detections, telemetry_timestamp=None):
        prepared_detections = []
        fallback_timestamp = parse_timestamp(telemetry_timestamp) or datetime.min.replace(tzinfo=timezone.utc)

        for sequence_index, detection in enumerate(detections):
            annotated_detection = self._annotate_detection_locked(detection, telemetry_timestamp=telemetry_timestamp)
            detected_at = parse_timestamp(annotated_detection.get("detected_at")) or fallback_timestamp
            prepared_detections.append(
                {
                    "detection": annotated_detection,
                    "batch_key": (
                        annotated_detection.get("image_id") or "",
                        annotated_detection.get("detected_at") or telemetry_timestamp or "",
                        annotated_detection.get("source_camera") or "",
                    ),
                    "detected_at": detected_at,
                    "sequence_index": sequence_index,
                }
            )

        return sorted(prepared_detections, key=lambda item: (item["detected_at"], item["sequence_index"]))

    def _group_prepared_detections_locked(self, prepared_detections):
        grouped_detections = {}
        for prepared_detection in prepared_detections:
            grouped_detections.setdefault(prepared_detection["batch_key"], []).append(prepared_detection)
        return list(grouped_detections.values())

    def _compare_detection_priority_locked(self, left_item, right_item):
        left_score = (left_item.get("detection") or {}).get("bbox_area_priority_score")
        right_score = (right_item.get("detection") or {}).get("bbox_area_priority_score")

        if left_score is None or right_score is None:
            return 0

        max_score = max(left_score, right_score)
        if max_score <= 0:
            return 0

        relative_gap = abs(left_score - right_score) / max_score
        if relative_gap <= self.bbox_area_similarity_ratio:
            return 0

        if left_score > right_score:
            return -1
        if left_score < right_score:
            return 1
        return 0

    def _sort_detection_batch_locked(self, prepared_detections):
        ordered_detections = sorted(prepared_detections, key=lambda item: item["sequence_index"])
        if not self.bbox_area_priority_enabled or self.bbox_area_priority_weight <= 0:
            return ordered_detections
        return sorted(ordered_detections, key=cmp_to_key(self._compare_detection_priority_locked))

    def _find_candidate_spaces_locked(self, latitude, longitude):
        if latitude is None or longitude is None:
            return [], None

        direct_match = self.find_matching_space(latitude, longitude, offset_meters=self.space_resolution_offset_meters)
        direct_distance = None
        if direct_match and direct_match in self.parking_spaces:
            direct_distance = self._distance_between_points_locked(
                latitude,
                longitude,
                self.parking_spaces[direct_match]["latitude"],
                self.parking_spaces[direct_match]["longitude"],
            )

        candidates = []
        for candidate_space_id, values in self.parking_spaces.items():
            candidate_distance = self._distance_between_points_locked(
                latitude,
                longitude,
                values["latitude"],
                values["longitude"],
            )
            if candidate_distance <= self.space_resolution_offset_meters:
                candidates.append(
                    {
                        "space_id": candidate_space_id,
                        "distance_meters": candidate_distance,
                    }
                )

        if direct_distance is not None and all(item["space_id"] != direct_match for item in candidates):
            candidates.append({"space_id": direct_match, "distance_meters": direct_distance})

        candidates.sort(
            key=lambda item: (
                item["space_id"] != direct_match,
                item["distance_meters"],
                item["space_id"],
            )
        )

        closest_distance = candidates[0]["distance_meters"] if candidates else None
        if closest_distance is None:
            _, closest_distance = self._find_nearest_space_locked(latitude, longitude)

        return candidates, closest_distance

    def _resolve_space_id_locked(self, latitude, longitude, reserved_space_ids=None):
        if latitude is None or longitude is None:
            return None, None

        reserved_space_ids = set(reserved_space_ids or [])
        candidates, closest_distance = self._find_candidate_spaces_locked(latitude, longitude)
        for candidate in candidates:
            if candidate["space_id"] not in reserved_space_ids:
                return candidate["space_id"], candidate["distance_meters"]
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

    def _apply_plate_detection_locked(self, device_id, detection, telemetry_timestamp=None, reserved_space_ids=None):
        telemetry_timestamp = telemetry_timestamp or utcnow_iso()
        normalized_detection = self._annotate_detection_locked(detection, telemetry_timestamp=telemetry_timestamp)
        gps = normalized_detection.get("gps") or {}
        raw_latitude = gps.get("lat")
        raw_longitude = gps.get("lon")
        mapped_gps = normalized_detection.get("mapped_gps") if isinstance(normalized_detection.get("mapped_gps"), dict) else {}
        latitude = mapped_gps.get("lat")
        longitude = mapped_gps.get("lon")
        detected_at = normalized_detection.get("detected_at") or telemetry_timestamp
        plate_text = normalized_detection.get("plate_text")

        resolved_space_id, resolved_distance_meters = self._resolve_space_id_locked(
            latitude,
            longitude,
            reserved_space_ids=reserved_space_ids,
        )
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
            # Space assignment uses calibrated GPS because the robot's raw GNSS
            # path drifts. Keep raw GPS separately for debugging and review.
            "latitude": latitude,
            "longitude": longitude,
            "gps": {
                "lat": raw_latitude,
                "lon": raw_longitude,
            },
            "mapped_gps": {
                "lat": latitude,
                "lon": longitude,
            },
            "bbox_metrics": normalized_detection.get("bbox_metrics"),
            # Bounding-box area is only a soft proximity signal. It can be
            # disabled or reweighted later without changing the assignment flow.
            "bbox_area_priority_score": normalized_detection.get("bbox_area_priority_score"),
            "confidence": normalized_detection.get("confidence"),
            "device_id": device_id,
            "image_id": normalized_detection.get("image_id"),
            "event_id": normalized_detection.get("event_id"),
            "source_camera": normalized_detection.get("source_camera"),
            "resolved_space_id": resolved_space_id,
            "resolved_distance_meters": normalized_detection.get("resolved_distance_meters"),
        }
        changed_spaces.append(resolved_space_id)
        return normalized_detection, changed_spaces

    def _apply_parking_update_locked(self, update):
        space_id = update.get("space_id")
        raw_latitude = update.get("latitude")
        raw_longitude = update.get("longitude")
        mapped_latitude, mapped_longitude = self._map_coordinate_locked(raw_latitude, raw_longitude)
        if not space_id:
            if mapped_latitude is None or mapped_longitude is None:
                return None
            space_id, _ = self._resolve_space_id_locked(mapped_latitude, mapped_longitude)

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
                "latitude": mapped_latitude,
                "longitude": mapped_longitude,
                "gps": {
                    "lat": raw_latitude,
                    "lon": raw_longitude,
                },
                "mapped_gps": {
                    "lat": mapped_latitude,
                    "lon": mapped_longitude,
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
