from __future__ import annotations

from contextlib import asynccontextmanager
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
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend_state import BackendState
from Tab1 import find_matching_space, parking_spaces

from .config import Settings
from .mediamtx import (
    build_forward_headers,
    build_upstream_url,
    filter_response_headers,
    rewrite_location_header,
)
from .schemas import TelemetryUpdate
from .telemetry import DemoTelemetryPublisher, TelemetryHub


settings = Settings.from_env()
telemetry_hub = TelemetryHub()
demo_publisher = DemoTelemetryPublisher(telemetry_hub, settings)
WHEP_PROXY_PREFIX = "/api/webrtc"
SUPPORTED_COMMANDS = {"camera_on", "camera_off", "capture_image"}
MAX_COMMAND_WAIT_SECONDS = 25
state = BackendState(
    parking_spaces,
    find_matching_space,
    runtime_dir=settings.runtime_dir,
    default_device_id=settings.default_device_id,
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


def _normalize_frontend_telemetry(payload: dict[str, Any]) -> dict[str, Any]:
    telemetry_payload = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else payload
    parsed = TelemetryUpdate.model_validate(telemetry_payload or {})
    normalized = parsed.model_dump(exclude_none=True)
    normalized.setdefault("source", payload.get("source") or "jetson")
    return normalized


def _validate_command_name(command_name: str) -> str:
    if command_name not in SUPPORTED_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported command '{command_name}'. Supported commands: {sorted(SUPPORTED_COMMANDS)}",
        )
    return command_name


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(settings.static_dir / "index.html")


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
    parking_updates = payload.get("parking_updates") or payload.get("events") or []
    normalized_updates = []
    for update in parking_updates:
        if not isinstance(update, dict):
            continue
        normalized = dict(update)
        normalized.setdefault("device_id", device_id)
        normalized_updates.append(normalized)

    result = await run_in_threadpool(state.update_telemetry, device_id, telemetry_payload or {}, normalized_updates)
    await telemetry_hub.publish(_normalize_frontend_telemetry(payload))
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
