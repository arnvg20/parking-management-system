# Capstone Live Stream Website

This replaces the slow snapshot-style browser view with a proper WebRTC player that talks to MediaMTX through a small FastAPI website backend. Video and telemetry are intentionally split: MediaMTX handles low-latency media delivery, while the website backend serves the page, proxies WHEP signaling, keeps the Jetson command API contract intact, and broadcasts telemetry over WebSocket.

## Why this approach

MediaMTX already documents WHEP-based WebRTC playback for browser pages. That is the simplest reliable fit here because it keeps latency low, avoids MJPEG refresh hacks, and does not require a separate transcoding service inside the website stack.

## Folder structure

- `WebPageRun.py`: simple entrypoint that starts the FastAPI app with Uvicorn
- `live_site/app.py`: backend routes, WHEP proxy, WebSocket endpoint, and health/config APIs
- `live_site/telemetry.py`: in-memory telemetry hub plus optional demo telemetry generator
- `live_site/mediamtx.py`: helper functions for proxying MediaMTX WHEP requests
- `live_site/static/index.html`: browser page layout
- `live_site/static/webrtc-player.js`: plain JavaScript WHEP/WebRTC player with reconnect logic
- `live_site/static/app.js`: page controller, status UI, operator command queue, telemetry socket, and video event handling
- `live_site/static/styles.css`: responsive styling
- `.env.example`: environment variables for the website backend
- `mediamtx.sample.yml`: sample MediaMTX settings for AWS

## Exact run steps

1. Install Python 3.11+ on the AWS instance.
2. Copy `.env.example` to `.env` and edit the values you need.
3. Install Python packages:

```bash
python -m pip install -r requirements.txt
```

4. Start the website backend:

```bash
python WebPageRun.py
```

5. Open the page in your browser:

```text
http://YOUR_EC2_PUBLIC_IP_OR_DOMAIN:5000
```

6. For Windows, you can also use:

```powershell
.\run_website_backend.ps1
```

## Environment variables

- `HOST`: bind address for the website backend. Use `0.0.0.0` on AWS.
- `PORT`: website backend port. Default is `5000`.
- `MEDIA_MTX_BASE_URL`: internal MediaMTX WHEP HTTP address. If MediaMTX runs on the same EC2 instance, use `http://127.0.0.1:8889`.
- `MEDIA_MTX_STREAM_PATH`: MediaMTX stream path name. Default is `jetson-01`.
- `DEFAULT_DEVICE_ID`: website-side device ID for command queueing. Default is `jetson-01`.
- `JETSON_API_TOKEN`: token used by the Jetson for the preserved `/api/jetson/*` contract.
- `TELEMETRY_API_KEY`: header value expected by `POST /api/telemetry`.
- `DEMO_TELEMETRY_ENABLED`: set to `true` if you want fake telemetry for a quick demo.
- `DEMO_TELEMETRY_INTERVAL_SECONDS`: fake telemetry update interval.
- `MEDIA_MTX_REQUEST_TIMEOUT_SECONDS`: backend timeout when proxying WHEP signaling.
- `JETSON_UPLOAD_IMAGE_READ_TIMEOUT_SECONDS`: backend-side guard while reading a crop upload. Default is `10`.
- `JETSON_UPLOAD_IMAGE_MAX_BYTES`: maximum accepted crop image size. Default is `5242880`.
- `BBOX_FILTER_ENABLED`: enables the server-side bbox pre-filter before GPS stall matching.
- `BBOX_WINDOW_SEC`: camera-local detection window used for bbox ranking. Default is `2.0`.
- `BBOX_TOP_K_PER_WINDOW`: number of strongest bbox detections allowed through each window. Default is `1`.
- `BBOX_MIN_RELATIVE_HEIGHT_RATIO`: second-stage guard for smaller detections relative to the largest bbox in the same window. Default is `0.65`.
- `BBOX_MIN_ABSOLUTE_HEIGHT_PX`: optional floor for bbox height. Default is `0`.
- `BBOX_USE_AREA_TIEBREAK`: uses bbox area after height when ranking same-window detections.
- `GPS_ASSIGNMENT_MAX_DISTANCE_M`: maximum server-side distance from the robot/detection GPS point to a parking-space center before rejecting the detection. Default is `8.0`.

## MediaMTX setup

Use `mediamtx.sample.yml` as your starting point. The important AWS-specific detail is `webrtcAdditionalHosts`: put the EC2 public IP or domain there so MediaMTX advertises a browser-reachable address during ICE negotiation.

Recommended AWS security group rules:

- Allow `5000/tcp` for the website backend, or put it behind Nginx on `80/443`.
- Allow `1935/tcp` for Jetson RTMP ingest into MediaMTX.
- Allow `8189/udp` for WebRTC media.
- Allow `8189/tcp` only if you enable the TCP fallback in MediaMTX.

Because the backend proxies WHEP signaling, MediaMTX port `8889` does not need to be internet-facing when both services live on the same AWS instance.
If your website is served over HTTPS, the backend proxy avoids browser mixed-content issues because the frontend talks to same-origin `/api/webrtc/...` instead of directly calling `http://YOUR_MEDIAMTX_HOST:8889/...`.

## Telemetry feed

The browser connects to:

- `GET /api/config` for frontend configuration
- `WS /ws/telemetry` for telemetry updates

Your telemetry producer should send JSON to:

```bash
curl -X POST http://YOUR_EC2_PUBLIC_IP_OR_DOMAIN:5000/api/telemetry \
  -H "Content-Type: application/json" \
  -H "X-Telemetry-Key: dev-telemetry-token" \
  -d "{\"latitude\":43.6532,\"longitude\":-79.3832,\"detected_plate\":\"ABC123\",\"confidence\":0.94,\"timestamp\":\"2026-03-30T18:00:00Z\",\"robot_status\":\"Patrolling\"}"
```

Expected telemetry fields:

- `latitude`
- `longitude`
- `detected_plate`
- `confidence`
- `timestamp`
- `robot_status`
- `power`: optional Jetson battery state. The object can include `battery_channel`, `pack_voltage_v`, `shutdown_threshold_v`, `power_action`, `will_shutdown`, `status`, `message`, and `low_voltage_duration_sec`.

For raw Jetson bridge payloads, the server-side plate matcher also accepts `gps.lat`, `gps.lon`, `plate_text`, `detected_at`, `bbox_xyxy`, and `source_camera`. When bbox metadata is present, the backend first keeps only the strongest detections per camera/time window, then runs the existing GPS polygon matcher and temporal smoothing logic on the survivors.

## Preserved Jetson backend contract

These endpoints are available for the Jetson and remain separate from the MediaMTX live video path:

- `GET /api/jetson/commands/next?device_id=jetson-01&wait=20`
- `POST /api/jetson/commands/<id>/ack`
- `POST /api/jetson/register`
- `POST /api/jetson/heartbeat`
- `POST /api/jetson/telemetry`
- `POST /api/jetson/upload-image`

`jetson_remote_bridge.py` provides a Jetson-side helper for this contract. Heartbeat, telemetry, command polling, and command ack use the normal `HTTP_TIMEOUT_SEC` path, while OCR crop uploads are queued to a background best-effort uploader with `IMAGE_UPLOAD_TIMEOUT_SEC` defaulting to `4` seconds. The uploader coalesces duplicate plate/track crops, caps pending work, retries with bounded backoff, drops stale crops, and only counts an upload after `/api/jetson/upload-image` confirms storage.

Jetson-side bridge knobs: `HTTP_TIMEOUT_SEC=15`, `IMAGE_UPLOAD_TIMEOUT_SEC=4`, `IMAGE_UPLOAD_MAX_PENDING=64`, `IMAGE_UPLOAD_MAX_ATTEMPTS=3`, `IMAGE_UPLOAD_MAX_AGE_SEC=120`, `IMAGE_UPLOAD_BACKOFF_BASE_SEC=1`, `IMAGE_UPLOAD_BACKOFF_MAX_SEC=15`, and `FRAME_UPLOAD_ENABLED=false`.

Supported queued commands:

- `camera_on`
- `camera_off`
- `capture_image`

Website/operator helpers are also exposed for demo use:

- `GET /api/devices`
- `GET /api/devices/<device_id>/status`
- `GET /api/devices/<device_id>/commands`
- `POST /api/devices/<device_id>/commands`
- `GET /api/uploads/<upload_id>`

## How the Jetson connects to the website

The website does not ingest video itself. The Jetson should publish into MediaMTX, and the website only reads the MediaMTX path over WebRTC.

Recommended flow:

1. Jetson publishes H.264 video to MediaMTX over RTMP on a path such as `jetson-01`.
2. MediaMTX exposes that path as WHEP/WebRTC.
3. The website backend proxies WHEP signaling at `/api/webrtc/jetson-01/whep`.
4. The browser plays the live stream from the website page.

If you already have a Jetson publishing path, just make sure `MEDIA_MTX_STREAM_PATH` matches it. With the current Jetson-side contract, the publish target is:

```text
rtmp://YOUR_MEDIAMTX_HOST:1935/jetson-01
```

Browser playback target equivalents are:

```text
http://YOUR_MEDIAMTX_HOST:8889/jetson-01/
http://YOUR_MEDIAMTX_HOST:8889/jetson-01/whep
```

In this website stack, the browser normally uses the backend proxy path instead:

```text
https://YOUR_WEBSITE_HOST/api/webrtc/jetson-01/whep
```

## Demo and reconnect behavior

- The player uses browser-native WebRTC, not MJPEG or repeated JPEG fetches.
- Stream reconnect is automatic if the WebRTC session fails or the video element errors.
- Telemetry WebSocket reconnect is automatic.
- The UI exposes connection state, playback state, stream path, operator buttons, and a simple last-event field for demos.

## Notes

Legacy files like `Website.html` and `backend_state.py` are left in the repo, but the new website stack is the code that `WebPageRun.py` now serves.
