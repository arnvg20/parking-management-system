from __future__ import annotations

import json
import queue
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend_state import BackendState, first_present
from Tab1 import (
    environmental_detections,
    find_matching_space,
    load_sample_vehicles,
    lot_bounds,
    parking_sections,
    parking_spaces,
)

from .admin import create_admin_router
from .config import Settings
from .mediamtx import (
    build_forward_headers,
    build_upstream_url,
    filter_response_headers,
    rewrite_location_header,
)
from .schemas import PowerTelemetryPayload, TelemetryUpdate
from .gps_calibration import ConstantOffsetMapper, default_mapper as _default_gps_mapper
from .space_assignment import LotSpaceAssociationConfig, LotSpaceAssociationService
from .telemetry import DemoTelemetryPublisher, TelemetryHub


settings = Settings.from_env()
telemetry_hub = TelemetryHub()
demo_publisher = DemoTelemetryPublisher(telemetry_hub, settings)
WHEP_PROXY_PREFIX = "/api/webrtc"
BASE_DIR = Path(__file__).resolve().parent.parent
SUPPORTED_COMMANDS = {"camera_on", "camera_off", "capture_image"}
MAX_COMMAND_WAIT_SECONDS = 25
if settings.demo_telemetry_enabled:
    load_sample_vehicles()
state = BackendState(
    parking_spaces,
    find_matching_space,
    runtime_dir=settings.runtime_dir,
    default_device_id=settings.default_device_id,
)
_gps_mapper = None
if settings.gps_calibration_enabled:
    _gps_mapper = _default_gps_mapper
elif settings.gps_offset_lat != 0.0 or settings.gps_offset_lon != 0.0:
    _gps_mapper = ConstantOffsetMapper(settings.gps_offset_lat, settings.gps_offset_lon)

lot_space_association = LotSpaceAssociationService(
    parking_spaces,
    gps_mapper=_gps_mapper,
    config=LotSpaceAssociationConfig(
        outside_space_max_distance_m=settings.gps_assignment_max_distance_m,
        min_stable_confidence=settings.gps_min_stable_confidence,
        drive_by_clear_radius_m=settings.gps_drive_by_clear_radius_m,
        bbox_filter_enabled=settings.bbox_filter_enabled,
        bbox_window_sec=settings.bbox_window_sec,
        bbox_top_k_per_window=settings.bbox_top_k_per_window,
        bbox_min_relative_height_ratio=settings.bbox_min_relative_height_ratio,
        bbox_min_absolute_height_px=settings.bbox_min_absolute_height_px,
        bbox_use_area_tiebreak=settings.bbox_use_area_tiebreak,
    ),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=False,
    )
    if settings.demo_telemetry_enabled:
        demo_publisher.start()

    yield

    if settings.demo_telemetry_enabled:
        await demo_publisher.stop()
    await app.state.http_client.aclose()


app = FastAPI(
    title="Capstone Live Stream Website",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
app.include_router(create_admin_router(state))


def _telemetry_key_is_valid(header_value: str | None) -> bool:
    if not settings.telemetry_api_key:
        return True
    return header_value == settings.telemetry_api_key


def _require_jetson_auth(request: Request) -> None:
    if not settings.jetson_api_token:
        return

    auth_header = request.headers.get("Authorization", "")
    api_key_value = request.headers.get("X-API-Key")
    bearer_value = f"Bearer {settings.jetson_api_token}"
    if auth_header == bearer_value or api_key_value == settings.jetson_api_token:
        return

    raise HTTPException(status_code=401, detail="Unauthorized Jetson request")


def _resolve_device_id(
    request: Request,
    explicit_value: str | None = None,
    required: bool = True,
    default_to_config: bool = False,
) -> str | None:
    value = explicit_value or request.query_params.get("device_id") or request.headers.get("X-Device-Id")
    if not value and default_to_config:
        value = settings.default_device_id

    if required and not value:
        raise HTTPException(status_code=400, detail="device_id is required")
    return value


def _normalize_power_payload(power_payload: Any) -> dict[str, Any] | None:
    if not isinstance(power_payload, dict):
        return None
    return PowerTelemetryPayload.model_validate(power_payload).model_dump(exclude_none=False)


def _normalize_frontend_telemetry(payload: dict[str, Any]) -> dict[str, Any]:
    telemetry_payload = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else payload
    power_present = isinstance(telemetry_payload, dict) and "power" in telemetry_payload
    normalized_power = _normalize_power_payload(telemetry_payload.get("power")) if power_present else None
    space_resolution = (
        telemetry_payload.get("space_resolution")
        if isinstance(telemetry_payload.get("space_resolution"), list)
        else []
    )
    plate_detections = (
        telemetry_payload.get("plate_detections")
        if isinstance(telemetry_payload.get("plate_detections"), list)
        else []
    )
    resolved_decision = next(
        (
            item
            for item in space_resolution
            if isinstance(item, dict)
            and item.get("status") == "OCCUPIED"
            and item.get("plate_read")
        ),
        None,
    )
    first_detection = plate_detections[0] if plate_detections and isinstance(plate_detections[0], dict) else {}
    location = first_detection.get("location") if isinstance(first_detection.get("location"), dict) else {}
    if not location:
        location = first_detection.get("gps") if isinstance(first_detection.get("gps"), dict) else {}
    resolved_location = (
        resolved_decision.get("location")
        if isinstance(resolved_decision, dict) and isinstance(resolved_decision.get("location"), dict)
        else {}
    )
    enriched_payload = dict(telemetry_payload or {})
    if resolved_decision:
        enriched_payload.setdefault("detected_plate", resolved_decision.get("plate_read"))
        enriched_payload.setdefault("confidence", resolved_decision.get("confidence"))
        enriched_payload.setdefault("timestamp", resolved_decision.get("source_detection_time"))
        if resolved_location:
            enriched_payload.setdefault("latitude", resolved_location.get("lat"))
            enriched_payload.setdefault("longitude", resolved_location.get("lon"))
    if first_detection:
        enriched_payload.setdefault(
            "detected_plate",
            first_detection.get("plate_read")
            or first_detection.get("plate_text")
            or first_detection.get("detected_plate"),
        )
        enriched_payload.setdefault(
            "confidence",
            first_detection.get("confidence_level") if first_detection.get("confidence_level") is not None else first_detection.get("confidence"),
        )
        enriched_payload.setdefault("timestamp", first_detection.get("time") or first_detection.get("detected_at"))
        if location:
            enriched_payload.setdefault("latitude", location.get("lat"))
            enriched_payload.setdefault("longitude", location.get("lon"))
    orientation_raw = telemetry_payload.get("orientation") if isinstance(telemetry_payload.get("orientation"), dict) else {}
    location_raw = telemetry_payload.get("location") if isinstance(telemetry_payload.get("location"), dict) else {}
    heading_deg = first_present(
        orientation_raw.get("heading_deg"),
        location_raw.get("heading_deg"),
    )
    heading_source = first_present(
        orientation_raw.get("heading_source"),
        location_raw.get("heading_source"),
    )
    if heading_deg is not None:
        enriched_payload.setdefault("heading_deg", heading_deg)
    if heading_source is not None:
        enriched_payload.setdefault("heading_source", heading_source)

    parsed = TelemetryUpdate.model_validate(enriched_payload or {})
    normalized = parsed.model_dump(exclude_none=True)
    if enriched_payload.get("detected_plate") and "detected_plate" not in normalized:
        normalized["detected_plate"] = enriched_payload.get("detected_plate")
    if enriched_payload.get("confidence") is not None and "confidence" not in normalized:
        normalized["confidence"] = enriched_payload.get("confidence")
    if enriched_payload.get("timestamp") and "timestamp" not in normalized:
        normalized["timestamp"] = enriched_payload.get("timestamp")
    if enriched_payload.get("latitude") is not None and "latitude" not in normalized:
        normalized["latitude"] = enriched_payload.get("latitude")
    if enriched_payload.get("longitude") is not None and "longitude" not in normalized:
        normalized["longitude"] = enriched_payload.get("longitude")
    normalized["power"] = normalized_power
    normalized.setdefault("source", payload.get("source") or "jetson")
    return normalized


def _validate_command_name(command_name: str) -> str:
    if command_name not in SUPPORTED_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported command '{command_name}'. Supported commands: {sorted(SUPPORTED_COMMANDS)}",
        )
    return command_name


def _serialize_point(point: Any) -> dict[str, float | None]:
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        return {"latitude": point[0], "longitude": point[1]}
    return {
        "latitude": point.get("latitude"),
        "longitude": point.get("longitude"),
    }


def _serialize_detection(detection: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    point = _serialize_point(detection)
    detection_id = detection.get("id") or fallback_id
    label = detection.get("label") or detection.get("name") or detection_id
    payload = {
        "id": detection_id,
        "label": label,
        "latitude": point["latitude"],
        "longitude": point["longitude"],
    }
    kind = detection.get("kind")
    if kind:
        payload["kind"] = kind
    return payload


def _serialize_space(space_id: str, values: dict[str, Any]) -> dict[str, Any]:
    return {
        "space_id": space_id,
        "section_id": values.get("section_id"),
        "latitude": values.get("latitude"),
        "longitude": values.get("longitude"),
        "polygon": [_serialize_point(point) for point in values.get("polygon", [])],
        "occupied": values.get("occupied", False),
        "status": values.get("status", "EMPTY"),
        "decision_confidence": values.get("decision_confidence"),
        "decision_reason": values.get("decision_reason"),
        "source_detection_time": values.get("source_detection_time"),
        "vehicle_data": values.get("vehicle_data"),
    }


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "Website.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/config")
async def get_client_config() -> dict[str, Any]:
    return {
        "streamPath": settings.stream_label,
        "whepEndpoint": settings.whep_proxy_path,
        "telemetryWebSocketPath": "/ws/telemetry",
        "demoTelemetryEnabled": settings.demo_telemetry_enabled,
        "deviceId": settings.default_device_id,
        "supportedCommands": sorted(SUPPORTED_COMMANDS),
    }


@app.get("/api/health")
async def health_check() -> dict[str, Any]:
    return {
        "status": "ok",
        "streamPath": settings.stream_label,
        "demoTelemetryEnabled": settings.demo_telemetry_enabled,
        "defaultDeviceId": settings.default_device_id,
    }


@app.get("/api/system/state")
async def get_system_state() -> dict[str, Any]:
    return await run_in_threadpool(state.get_system_snapshot)


@app.get("/api/parking-spaces")
async def get_parking_spaces() -> dict[str, Any]:
    spaces = await run_in_threadpool(state.get_parking_spaces)
    return {space_id: _serialize_space(space_id, values) for space_id, values in spaces.items()}


@app.get("/api/map-data")
async def get_map_data() -> dict[str, Any]:
    spaces = await run_in_threadpool(state.get_parking_spaces)
    sections: dict[str, Any] = {}
    for section_id, values in parking_sections.items():
        sections[section_id] = {
            "name": values["name"],
            "spaces": values["spaces"],
            "center": values["center"],
            "corners": [_serialize_point(point) for point in values["corners"]],
        }

    return {
        "lot_bounds": [_serialize_point(point) for point in lot_bounds],
        "sections": sections,
        "spaces": {space_id: _serialize_space(space_id, values) for space_id, values in spaces.items()},
        "environmental_detections": {
            "cracks": [
                _serialize_detection(detection, f"crack-{index}")
                for index, detection in enumerate(environmental_detections.get("cracks", []), start=1)
            ],
            "signs": [
                _serialize_detection(detection, f"sign-{index}")
                for index, detection in enumerate(environmental_detections.get("signs", []), start=1)
            ],
        },
    }


@app.get("/api/devices")
async def list_devices() -> list[dict[str, Any]]:
    return await run_in_threadpool(state.list_devices)


@app.get("/api/devices/{device_id}")
async def get_device(device_id: str) -> dict[str, Any]:
    device = await run_in_threadpool(state.get_device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@app.get("/api/devices/{device_id}/status")
async def get_device_status(device_id: str) -> dict[str, Any]:
    return await get_device(device_id)


@app.get("/api/devices/{device_id}/commands")
async def get_device_commands(device_id: str) -> list[dict[str, Any]]:
    device = await run_in_threadpool(state.get_device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return await run_in_threadpool(state.get_commands_for_device, device_id)


@app.get("/api/devices/{device_id}/observations")
async def get_device_observations(device_id: str) -> list[dict[str, Any]]:
    device = await run_in_threadpool(state.get_device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return await run_in_threadpool(state.get_observations_for_device, device_id)


@app.get("/api/devices/{device_id}/observations/{observation_id}")
async def get_device_observation(device_id: str, observation_id: str) -> dict[str, Any]:
    observation = await run_in_threadpool(state.get_observation, device_id, observation_id)
    if not observation:
        raise HTTPException(status_code=404, detail="Observation not found")
    return observation


@app.get("/api/devices/{device_id}/observations/{observation_id}/raw")
async def get_device_observation_raw(device_id: str, observation_id: str) -> FileResponse:
    observation_path = await run_in_threadpool(state.get_observation_file_path, device_id, observation_id)
    if not observation_path:
        raise HTTPException(status_code=404, detail="Observation not found")

    return FileResponse(
        observation_path,
        media_type="application/json",
        filename=Path(observation_path).name,
    )


@app.post("/api/devices/{device_id}/commands")
async def queue_device_command(device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    command_name = _validate_command_name(str(payload.get("command", "")).strip())
    command = await run_in_threadpool(
        state.queue_command,
        device_id,
        command_name,
        payload.get("payload") or {},
        payload.get("requested_by", "website"),
    )
    return {"status": "queued", "command": command}


@app.post("/api/system/on")
async def system_on(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    device_id = _resolve_device_id(request, explicit_value=payload.get("device_id"), default_to_config=True)
    command = await run_in_threadpool(state.queue_command, device_id, "camera_on", {}, "legacy-api")
    return {"status": "queued", "command": command}


@app.post("/api/system/off")
async def system_off(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    device_id = _resolve_device_id(request, explicit_value=payload.get("device_id"), default_to_config=True)
    command = await run_in_threadpool(state.queue_command, device_id, "camera_off", {}, "legacy-api")
    return {"status": "queued", "command": command}


@app.get("/api/uploads/{upload_id}")
async def get_uploaded_image(upload_id: str) -> FileResponse:
    record = await run_in_threadpool(state.get_upload, upload_id)
    if not record:
        raise HTTPException(status_code=404, detail="Upload not found")

    return FileResponse(
        record["path"],
        media_type=record.get("content_type", "image/jpeg"),
        filename=record.get("original_filename") or record["filename"],
    )


@app.get("/api/telemetry/latest")
async def get_latest_telemetry() -> dict[str, object | None]:
    return await telemetry_hub.get_snapshot()


@app.post("/api/telemetry")
async def publish_telemetry(
    payload: TelemetryUpdate,
    x_telemetry_key: str | None = Header(default=None),
):
    # Point your upstream telemetry producer here.
    # Example source: a Jetson-side process that publishes GPS / plate metadata
    # separately from the video pipeline.
    if not _telemetry_key_is_valid(x_telemetry_key):
        raise HTTPException(status_code=401, detail="Invalid telemetry API key")

    snapshot = await telemetry_hub.publish(payload)
    return {"status": "ok", "telemetry": snapshot}


@app.post("/api/jetson/register")
@app.post("/api/jetson/heartbeat")
async def jetson_heartbeat(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    _require_jetson_auth(request)
    device_id = _resolve_device_id(request, explicit_value=payload.get("device_id"))
    snapshot = await run_in_threadpool(state.update_heartbeat, device_id, payload)
    return {
        "status": "ok",
        "device": snapshot,
        "pending_commands": snapshot["pending_command_count"],
    }


@app.post("/api/jetson/telemetry")
async def jetson_telemetry(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    _require_jetson_auth(request)
    device_id = _resolve_device_id(request, explicit_value=payload.get("device_id"))

    telemetry_payload = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else payload
    plate_detections = (
        telemetry_payload.get("plate_detections")
        if isinstance(telemetry_payload.get("plate_detections"), list)
        else None
    )
    parking_updates = payload.get("parking_updates") or payload.get("events") or []
    normalized_updates = []
    for update in parking_updates:
        if not isinstance(update, dict):
            continue
        normalized = dict(update)
        normalized.setdefault("device_id", device_id)
        normalized_updates.append(normalized)

    publish_payload = payload
    if plate_detections is not None:
        resolution = await run_in_threadpool(lot_space_association.ingest, device_id, telemetry_payload or {})
        enriched_telemetry_payload = dict(telemetry_payload or {})
        enriched_telemetry_payload["space_resolution"] = [decision.to_dict() for decision in resolution.space_decisions]
        enriched_telemetry_payload["detection_associations"] = [decision.to_dict() for decision in resolution.detection_results]

        if isinstance(payload.get("telemetry"), dict):
            observation_payload = dict(payload)
            observation_payload["telemetry"] = enriched_telemetry_payload
        else:
            observation_payload = dict(enriched_telemetry_payload)

        await run_in_threadpool(state.save_observation, device_id, observation_payload, "jetson.telemetry")
        result = await run_in_threadpool(
            state.apply_space_decisions,
            device_id,
            resolution.space_decisions,
            enriched_telemetry_payload,
        )
        publish_payload = observation_payload
    else:
        await run_in_threadpool(state.save_observation, device_id, payload, "jetson.telemetry")
        result = await run_in_threadpool(state.update_telemetry, device_id, telemetry_payload or {}, normalized_updates)
    await telemetry_hub.publish(_normalize_frontend_telemetry(publish_payload))
    return {
        "status": "ok",
        "device": result["device"],
        "updated_spaces": result["updated_spaces"],
        "summary": (await run_in_threadpool(state.get_system_snapshot))["summary"],
    }


@app.post("/api/jetson/upload-image")
async def jetson_upload_image(
    request: Request,
    image: UploadFile = File(...),
    device_id: str | None = Form(default=None),
) -> dict[str, Any]:
    _require_jetson_auth(request)
    resolved_device_id = _resolve_device_id(request, explicit_value=device_id)
    form = await request.form()
    metadata = {
        key: value
        for key, value in form.items()
        if key not in {"image", "device_id"}
    }

    image_bytes = await image.read()
    image_record = await run_in_threadpool(
        state.save_image,
        resolved_device_id,
        image.filename,
        image_bytes,
        metadata,
        image.content_type or "image/jpeg",
    )
    return {"status": "stored", "image": image_record}


@app.post("/api/jetson/upload-frame")
async def jetson_upload_frame(
    request: Request,
    frame: UploadFile = File(...),
    device_id: str | None = Form(default=None),
    source_id: str | None = Form(default=None),
    meta: str | None = Form(default=None),
) -> dict[str, Any]:
    _require_jetson_auth(request)
    resolved_device_id = _resolve_device_id(request, explicit_value=device_id)
    resolved_source_id = (source_id or "").strip() or "processor"

    try:
        meta_dict = json.loads(meta) if meta else {}
    except (ValueError, TypeError):
        meta_dict = {}

    frame_bytes = await frame.read()
    result = await run_in_threadpool(
        state.save_source_frame,
        resolved_device_id,
        resolved_source_id,
        meta_dict,
        frame_bytes,
    )
    return {"status": "stored", "source": result}


@app.get("/api/devices/{device_id}/sources/{source_id}/snapshot")
async def get_source_snapshot(device_id: str, source_id: str) -> Response:
    result = await run_in_threadpool(state.get_source_frame_bytes, device_id, source_id)
    if not result:
        raise HTTPException(status_code=404, detail="No frame available for this source")
    return Response(content=result["frame_bytes"], media_type="image/jpeg")


@app.get("/api/devices/{device_id}/sources/{source_id}/stream.mjpeg")
async def stream_source_mjpeg(device_id: str, source_id: str, request: Request) -> StreamingResponse:
    async def generate():
        last_version = 0
        while True:
            if await request.is_disconnected():
                break
            result = await run_in_threadpool(
                state.wait_for_next_source_frame, device_id, source_id, last_version, 10
            )
            if result is None:
                continue
            last_version = result["frame_version"]
            frame_bytes = result["frame_bytes"]
            yield (
                b"--mjpegframe\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame_bytes)).encode() + b"\r\n"
                b"\r\n" + frame_bytes + b"\r\n"
            )

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=mjpegframe",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jetson/commands/next")
async def jetson_next_command(request: Request, device_id: str, wait: int = 20) -> Response:
    _require_jetson_auth(request)
    wait_seconds = max(0, min(wait, MAX_COMMAND_WAIT_SECONDS))
    command = await run_in_threadpool(state.get_next_command, device_id, wait_seconds)
    if not command:
        return Response(status_code=204)
    return JSONResponse(command)


@app.post("/api/jetson/commands/{command_id}/ack")
async def jetson_ack_command(request: Request, command_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    _require_jetson_auth(request)
    device_id = _resolve_device_id(request, explicit_value=payload.get("device_id"))
    command = await run_in_threadpool(
        state.acknowledge_command,
        device_id,
        command_id,
        bool(payload.get("success", True)),
        payload.get("result") or {},
    )
    if not command:
        raise HTTPException(status_code=404, detail="Command not found")
    return {"status": "acknowledged", "command": command}


@app.websocket("/ws/telemetry")
async def telemetry_socket(websocket: WebSocket) -> None:
    await telemetry_hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await telemetry_hub.disconnect(websocket)


@app.get("/api/events")
async def event_stream() -> StreamingResponse:
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

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.api_route(
    "/api/webrtc/{proxy_path:path}",
    methods=["OPTIONS", "POST", "PATCH", "DELETE"],
)
async def proxy_webrtc(proxy_path: str, request: Request) -> Response:
    client: httpx.AsyncClient = request.app.state.http_client
    upstream_url = build_upstream_url(settings.media_mtx_base_url, proxy_path)
    forwarded_headers = build_forward_headers(request.headers)
    body = await request.body()

    try:
        upstream_response = await client.request(
            method=request.method,
            url=upstream_url,
            params=list(request.query_params.multi_items()),
            headers=forwarded_headers,
            content=body,
        )
    except httpx.RequestError:
        return JSONResponse(
            status_code=502,
            content={"error": "Unable to reach the MediaMTX WHEP endpoint"},
        )

    response_headers = filter_response_headers(upstream_response.headers)
    location = response_headers.pop("location", None) or response_headers.pop("Location", None)
    if location:
        rewritten = rewrite_location_header(location, WHEP_PROXY_PREFIX)
        response_headers["Location"] = rewritten

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )
