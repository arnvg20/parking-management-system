@echo off
setlocal

set "HOST=%~1"
if "%HOST%"=="" set "HOST=0.0.0.0"

set "PORT=%~2"
if "%PORT%"=="" set "PORT=8000"

set "MEDIA_MTX_BASE_URL=%~3"
if "%MEDIA_MTX_BASE_URL%"=="" set "MEDIA_MTX_BASE_URL=http://127.0.0.1:8889"

set "MEDIA_MTX_STREAM_PATH=%~4"
if "%MEDIA_MTX_STREAM_PATH%"=="" set "MEDIA_MTX_STREAM_PATH=jetson-01"

set "DEFAULT_DEVICE_ID=%~5"
if "%DEFAULT_DEVICE_ID%"=="" set "DEFAULT_DEVICE_ID=jetson-01"

set "JETSON_API_TOKEN=%~6"
if "%JETSON_API_TOKEN%"=="" set "JETSON_API_TOKEN=dev-jetson-token"

set "TELEMETRY_API_KEY=%~7"
if "%TELEMETRY_API_KEY%"=="" set "TELEMETRY_API_KEY=dev-telemetry-token"

set "DEMO_TELEMETRY_ENABLED=%~8"
if "%DEMO_TELEMETRY_ENABLED%"=="" set "DEMO_TELEMETRY_ENABLED=false"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_website_backend.ps1" -Host "%HOST%" -Port "%PORT%" -MediaMtxBaseUrl "%MEDIA_MTX_BASE_URL%" -MediaMtxStreamPath "%MEDIA_MTX_STREAM_PATH%" -DefaultDeviceId "%DEFAULT_DEVICE_ID%" -JetsonApiToken "%JETSON_API_TOKEN%" -TelemetryApiKey "%TELEMETRY_API_KEY%" -DemoTelemetryEnabled "%DEMO_TELEMETRY_ENABLED%"

endlocal
