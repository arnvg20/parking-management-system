import json
import os
import queue
from pathlib import Path

from flask import Flask, Response, jsonify, make_response, request, send_file, stream_with_context
from flask_cors import CORS

from backend_state import BackendState
from jetson_contract import JETSON_COMMANDS, normalize_command_name, normalize_telemetry_payload, parse_float
from Tab1 import (
    environmental_detections,
    find_matching_space,
    load_sample_vehicles,
    lot_bounds,
    parking_sections,
    parking_spaces,
)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DEVICE_ID = os.getenv("DEFAULT_DEVICE_ID", "jetson-01")
JETSON_API_TOKEN = os.getenv("JETSON_API_TOKEN", "dev-jetson-token")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")
MAX_COMMAND_WAIT_SECONDS = 25

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGIN}})

# Keep demo occupancy seeding opt-in so real telemetry stays the source of truth.
if os.getenv("LOAD_SAMPLE_VEHICLES", "0") == "1":
    load_sample_vehicles()
state = BackendState(
    parking_spaces,
    find_matching_space,
    runtime_dir=BASE_DIR / "runtime_data",
    default_device_id=DEFAULT_DEVICE_ID,
)


def read_json_body():
    return request.get_json(silent=True) or {}


def cache_busting_response(content, content_type):
    response = make_response(content)
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


def get_device_id_from_request(required=True):
    value = (
        request.args.get("device_id")
        or request.form.get("device_id")
        or request.headers.get("X-Device-Id")
    )
    if not value and request.is_json:
        value = read_json_body().get("device_id")

    if required and not value:
        return None, (jsonify({"error": "device_id is required"}), 400)
    return value, None


def serialize_point(point):
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        return {"latitude": point[0], "longitude": point[1]}
    return {
        "latitude": point.get("latitude"),
        "longitude": point.get("longitude"),
    }


def serialize_detection(detection, fallback_id):
    point = serialize_point(detection)
    if isinstance(detection, dict):
        detection_id = detection.get("id") or fallback_id
        label = detection.get("label") or detection.get("name") or detection_id
        kind = detection.get("kind")
    else:
        detection_id = fallback_id
        label = fallback_id
        kind = None

    payload = {
        "id": detection_id,
        "label": label,
        "latitude": point["latitude"],
        "longitude": point["longitude"],
    }
    if kind:
        payload["kind"] = kind
    return payload


def serialize_space(space_id, values):
    return {
        "space_id": space_id,
        "section_id": values.get("section_id"),
        "latitude": values.get("latitude"),
        "longitude": values.get("longitude"),
        "polygon": [serialize_point(point) for point in values.get("polygon", [])],
        "occupied": values.get("occupied", False),
        "vehicle_data": values.get("vehicle_data"),
    }


@app.before_request
def require_jetson_auth():
    if not request.path.startswith("/api/jetson/"):
        return None

    auth_header = request.headers.get("Authorization", "")
    bearer_value = f"Bearer {JETSON_API_TOKEN}"
    api_key_value = request.headers.get("X-API-Key")

    if auth_header == bearer_value or api_key_value == JETSON_API_TOKEN:
        return None

    return jsonify({"error": "Unauthorized Jetson request"}), 401


@app.route("/")
def index():
    return send_file(BASE_DIR / "Website.html")


@app.route("/api/health", methods=["GET"])
def health_check():
    snapshot = state.get_system_snapshot()
    return jsonify(
        {
            "status": "ok",
            "server_time": snapshot["server_time"],
            "device_count": len(snapshot["devices"]),
            "default_device_id": snapshot["default_device_id"],
        }
    )


@app.route("/api/system/state", methods=["GET"])
def get_system_state():
    return jsonify(state.get_system_snapshot())


@app.route("/api/parking-spaces", methods=["GET"])
def get_parking_spaces():
    spaces = state.get_parking_spaces()
    return jsonify({space_id: serialize_space(space_id, values) for space_id, values in spaces.items()})


@app.route("/api/map-data", methods=["GET"])
def get_map_data():
    sections = {}
    for section_id, values in parking_sections.items():
        sections[section_id] = {
            "name": values["name"],
            "spaces": values["spaces"],
            "center": values["center"],
            "corners": [serialize_point(point) for point in values["corners"]],
        }

    spaces = state.get_parking_spaces()
    return jsonify(
        {
            "lot_bounds": [serialize_point(point) for point in lot_bounds],
            "sections": sections,
            "spaces": {
                space_id: serialize_space(space_id, values)
                for space_id, values in spaces.items()
            },
            "environmental_detections": {
                "cracks": [
                    serialize_detection(detection, f"crack-{index}")
                    for index, detection in enumerate(environmental_detections.get("cracks", []), start=1)
                ],
                "signs": [
                    serialize_detection(detection, f"sign-{index}")
                    for index, detection in enumerate(environmental_detections.get("signs", []), start=1)
                ],
            },
        }
    )


@app.route("/api/space-locations", methods=["GET"])
def get_space_locations():
    locations = {}
    for space_id, values in state.get_parking_spaces().items():
        locations[space_id] = {
            "latitude": values["latitude"],
            "longitude": values["longitude"],
        }
    return jsonify(locations)


@app.route("/api/add-vehicle", methods=["POST"])
def add_vehicle():
    payload = read_json_body()
    space_id = payload.get("space_id")
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")

    if not space_id and (latitude is None or longitude is None):
        return jsonify({"error": "Provide either space_id or latitude/longitude"}), 400

    update = {
        "space_id": space_id,
        "latitude": latitude,
        "longitude": longitude,
        "occupied": True,
        "license_plate": payload.get("license_plate"),
        "captured_at": payload.get("captured_at"),
        "device_id": payload.get("device_id"),
        "confidence": payload.get("confidence"),
        "image_id": payload.get("image_id"),
    }
    changed_space = state.apply_manual_parking_update(update)
    if not changed_space:
        return jsonify({"status": "error", "message": "No matching parking space found"}), 404

    resolved_space_id = space_id or ((changed_space.get("vehicle_data") or {}).get("resolved_space_id"))
    return jsonify(
        {
            "status": "success",
            "message": f"Vehicle parked in space {resolved_space_id}",
            "parking_space": resolved_space_id,
            "space_state": changed_space,
        }
    )


@app.route("/api/remove-vehicle", methods=["POST"])
def remove_vehicle():
    payload = read_json_body()
    space_id = payload.get("space_id")
    if not space_id:
        return jsonify({"error": "space_id is required"}), 400

    changed_space = state.apply_manual_parking_update({"space_id": space_id, "occupied": False})
    if not changed_space:
        return jsonify({"error": "Invalid space ID"}), 400

    return jsonify(
        {
            "status": "success",
            "message": f"Vehicle removed from space {space_id}",
            "space_state": changed_space,
        }
    )


@app.route("/api/toggle-space", methods=["POST"])
def toggle_space():
    payload = read_json_body()
    space_id = payload.get("space_id")
    if not space_id:
        return jsonify({"error": "space_id is required"}), 400

    updated_space = state.toggle_space(space_id)
    if not updated_space:
        return jsonify({"error": "Invalid space ID"}), 400

    return jsonify(
        {
            "status": "success",
            "space_id": space_id,
            "occupied": updated_space["occupied"],
            "space_state": updated_space,
        }
    )


@app.route("/api/devices", methods=["GET"])
def list_devices():
    return jsonify(state.list_devices())


@app.route("/api/devices/<device_id>", methods=["GET"])
def get_device(device_id):
    device = state.get_device(device_id)
    if not device:
        return jsonify({"error": "Device not found"}), 404
    return jsonify(device)


@app.route("/api/devices/<device_id>/status", methods=["GET"])
def get_device_status(device_id):
    return get_device(device_id)


@app.route("/api/devices/<device_id>/lot-status", methods=["GET"])
def get_device_lot_status(device_id):
    device = state.get_device(device_id)
    if not device:
        return jsonify({"error": "Device not found"}), 404

    snapshot = state.get_system_snapshot()
    return jsonify(
        {
            "device_id": device_id,
            "updated_at": device.get("updated_at"),
            "summary": snapshot["summary"],
            "parking_spaces": snapshot["parking_spaces"],
        }
    )


@app.route("/api/devices/<device_id>/commands", methods=["GET"])
def get_device_commands(device_id):
    device = state.get_device(device_id)
    if not device:
        return jsonify({"error": "Device not found"}), 404
    return jsonify(state.get_commands_for_device(device_id))


@app.route("/api/devices/<device_id>/commands", methods=["POST"])
def queue_device_command(device_id):
    payload = read_json_body()
    command_name = payload.get("command")
    if not command_name:
        return jsonify({"error": "command is required"}), 400

    normalized_command = normalize_command_name(command_name)
    if not normalized_command:
        return (
            jsonify(
                {
                    "error": f"Unsupported command '{command_name}'",
                    "supported_commands": list(JETSON_COMMANDS),
                }
            ),
            400,
        )

    command = state.queue_command(
        device_id=device_id,
        command_type=normalized_command,
        payload=payload.get("payload") or {},
        requested_by=payload.get("requested_by", "website"),
    )
    return jsonify({"status": "queued", "command": command}), 202


@app.route("/api/devices/<device_id>/latest-frame", methods=["GET"])
def get_latest_frame(device_id):
    frame = state.get_latest_frame(device_id)
    if not frame:
        return jsonify({"error": "No frame available for this device"}), 404
    return cache_busting_response(frame["frame_bytes"], frame["content_type"])


@app.route("/api/devices/<device_id>/stream.mjpeg", methods=["GET"])
def stream_mjpeg(device_id):
    if not state.get_device(device_id):
        return jsonify({"error": "Device not found"}), 404

    def generate():
        last_version = 0
        while True:
            frame = state.wait_for_next_frame(device_id, last_version=last_version, timeout=30)
            if not frame:
                continue
            last_version = frame["frame_version"]
            frame_bytes = frame["frame_bytes"]
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                + f"Content-Length: {len(frame_bytes)}\r\n\r\n".encode("utf-8")
                + frame_bytes
                + b"\r\n"
            )

    return Response(
        stream_with_context(generate()),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.route("/api/uploads/<upload_id>", methods=["GET"])
def get_uploaded_image(upload_id):
    record = state.get_upload(upload_id)
    if not record:
        return jsonify({"error": "Upload not found"}), 404

    return send_file(
        record["path"],
        mimetype=record.get("content_type", "image/jpeg"),
        download_name=record["original_filename"] or record["filename"],
        max_age=0,
    )


@app.route("/api/events", methods=["GET"])
def event_stream():
    subscriber = state.subscribe()

    def generate():
        yield "retry: 3000\n\n"
        try:
            while True:
                try:
                    event = subscriber.get(timeout=20)
                    yield f"event: state.changed\ndata: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            state.unsubscribe(subscriber)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/system/on", methods=["POST"])
def start_camera_legacy():
    payload = read_json_body()
    device_id = payload.get("device_id", DEFAULT_DEVICE_ID)
    command = state.queue_command(device_id, "cmd_patrol", requested_by="legacy-api")
    return jsonify({"status": "queued", "command": command}), 202


@app.route("/api/system/off", methods=["POST"])
def stop_camera_legacy():
    payload = read_json_body()
    device_id = payload.get("device_id", DEFAULT_DEVICE_ID)
    command = state.queue_command(device_id, "cmd_standby", requested_by="legacy-api")
    return jsonify({"status": "queued", "command": command}), 202


@app.route("/api/jetson/register", methods=["POST"])
@app.route("/api/jetson/heartbeat", methods=["POST"])
def jetson_heartbeat():
    payload = read_json_body()
    device_id = payload.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    snapshot = state.update_heartbeat(device_id, payload)
    return jsonify(
        {
            "status": "ok",
            "device": snapshot,
            "pending_commands": snapshot["pending_command_count"],
        }
    )


@app.route("/api/jetson/telemetry", methods=["POST"])
def jetson_telemetry():
    payload = read_json_body()
    telemetry = normalize_telemetry_payload(payload, fallback_device_id=payload.get("device_id"))
    device_id = telemetry.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    result = state.update_telemetry(device_id, telemetry)
    return jsonify(
        {
            "status": "ok",
            "device": result["device"],
            "updated_spaces": result["updated_spaces"],
            "summary": state.get_system_snapshot()["summary"],
        }
    )


@app.route("/api/jetson/upload-image", methods=["POST"])
def jetson_upload_image():
    image_file = request.files.get("image")
    if not image_file:
        return jsonify({"error": "Missing image file field named 'image'"}), 400

    device_id, error = get_device_id_from_request(required=True)
    if error:
        return error

    metadata = {key: value for key, value in request.form.items() if key != "device_id"}
    image_record = state.save_image(
        device_id,
        image_file.filename,
        image_file.read(),
        metadata,
        content_type=image_file.mimetype or "image/jpeg",
    )

    latitude = parse_float(metadata.get("latitude"))
    longitude = parse_float(metadata.get("longitude"))
    if latitude is not None and longitude is not None:
        state.apply_manual_parking_update(
            {
                "latitude": latitude,
                "longitude": longitude,
                "occupied": str(metadata.get("occupied", "true")).lower() != "false",
                "license_plate": metadata.get("license_plate"),
                "captured_at": metadata.get("captured_at"),
                "device_id": device_id,
                "image_id": image_record["id"],
                "confidence": parse_float(metadata.get("confidence")),
                "event_id": metadata.get("event_id"),
                "source_camera": metadata.get("source_camera"),
            }
        )

    return jsonify({"status": "stored", "image": image_record}), 201


@app.route("/api/jetson/upload-frame", methods=["POST"])
def jetson_upload_frame():
    frame_file = request.files.get("frame")
    if not frame_file:
        return jsonify({"error": "Missing frame file field named 'frame'"}), 400

    device_id, error = get_device_id_from_request(required=True)
    if error:
        return error

    metadata = {key: value for key, value in request.form.items() if key != "device_id"}
    frame_record = state.save_frame(device_id, frame_file.filename, frame_file.read(), metadata)
    return jsonify({"status": "stream_updated", "frame": frame_record}), 201


@app.route("/api/jetson/commands/next", methods=["GET"])
def jetson_next_command():
    device_id, error = get_device_id_from_request(required=True)
    if error:
        return error

    wait_seconds = request.args.get("wait", default=20, type=int)
    wait_seconds = max(0, min(wait_seconds, MAX_COMMAND_WAIT_SECONDS))
    command = state.get_next_command(device_id, wait_seconds=wait_seconds)
    if not command:
        return ("", 204)
    return jsonify(command)


@app.route("/api/jetson/commands/<int:command_id>/ack", methods=["POST"])
def jetson_ack_command(command_id):
    payload = read_json_body()
    device_id = payload.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    command = state.acknowledge_command(
        device_id=device_id,
        command_id=command_id,
        success=bool(payload.get("success", True)),
        result=payload.get("result") or {},
    )
    if not command:
        return jsonify({"error": "Command not found"}), 404
    return jsonify({"status": "acknowledged", "command": command})


if __name__ == "__main__":
    app.run(
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
        threaded=True,
    )
