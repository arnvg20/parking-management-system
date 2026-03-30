param(
    [string]$Host = "0.0.0.0",
    [int]$Port = 5000,
    [string]$MediaMtxBaseUrl = "http://127.0.0.1:8889",
    [string]$MediaMtxStreamPath = "jetson-01",
    [string]$DefaultDeviceId = "jetson-01",
    [string]$JetsonApiToken = "dev-jetson-token",
    [string]$TelemetryApiKey = "dev-telemetry-token",
    [string]$DemoTelemetryEnabled = "false"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$uvExe = Join-Path $env:USERPROFILE ".local\bin\uv.exe"

Set-Location $repoRoot

$env:HOST = $Host
$env:PORT = "$Port"
$env:MEDIA_MTX_BASE_URL = $MediaMtxBaseUrl
$env:MEDIA_MTX_STREAM_PATH = $MediaMtxStreamPath
$env:DEFAULT_DEVICE_ID = $DefaultDeviceId
$env:JETSON_API_TOKEN = $JetsonApiToken
$env:TELEMETRY_API_KEY = $TelemetryApiKey
$env:DEMO_TELEMETRY_ENABLED = $DemoTelemetryEnabled
$env:UV_CACHE_DIR = Join-Path $repoRoot ".uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $repoRoot ".uv-python"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "Starting live website on http://127.0.0.1:$Port"
Write-Host "MediaMTX proxy target: $MediaMtxBaseUrl"
Write-Host "Stream path: /$MediaMtxStreamPath"

if (Test-Path $uvExe) {
    & $uvExe run --python 3.12 --with-requirements requirements.txt WebPageRun.py
    exit $LASTEXITCODE
}

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCmd) {
    & $pythonCmd.Source WebPageRun.py
    exit $LASTEXITCODE
}

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    & $pyLauncher.Source -3 WebPageRun.py
    exit $LASTEXITCODE
}

throw "Neither uv.exe, python.exe, nor py.exe was found. Install one of them before running the website backend."
