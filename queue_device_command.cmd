@echo off
setlocal

set "COMMAND_NAME=%~1"
if "%COMMAND_NAME%"=="" (
    echo Usage: queue_device_command.cmd ^<camera_on^|camera_off^|capture_image^> [backendUrl] [deviceId]
    exit /b 1
)

set "BACKEND_URL=%~2"
if "%BACKEND_URL%"=="" set "BACKEND_URL=http://127.0.0.1:8000"

set "DEVICE_ID=%~3"
if "%DEVICE_ID%"=="" set "DEVICE_ID=jetson-01"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0queue_device_command.ps1" -Command "%COMMAND_NAME%" -BackendUrl "%BACKEND_URL%" -DeviceId "%DEVICE_ID%"

endlocal
