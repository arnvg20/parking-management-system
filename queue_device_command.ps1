param(
    [Parameter(Mandatory = $true)]
    [string]$Command,
    [string]$BackendUrl = "http://127.0.0.1:5000",
    [string]$DeviceId = "jetson-01"
)

$ErrorActionPreference = "Stop"

$backendBase = $BackendUrl.TrimEnd("/")
$queueUrl = "$backendBase/api/devices/$DeviceId/commands"
$statusUrl = "$backendBase/api/devices/$DeviceId/status"
$commandsUrl = "$backendBase/api/devices/$DeviceId/commands"

$body = @{
    command = $Command
    payload = @{
        requested_at = (Get-Date).ToUniversalTime().ToString("o")
        source = "terminal"
    }
} | ConvertTo-Json -Depth 5

Write-Host "Queueing '$Command' for device '$DeviceId' against $backendBase"
$queued = Invoke-RestMethod -Method Post -Uri $queueUrl -ContentType "application/json" -Body $body
$status = Invoke-RestMethod -Method Get -Uri $statusUrl
$commands = Invoke-RestMethod -Method Get -Uri $commandsUrl

Write-Host ""
Write-Host "Queued command:"
$queued | ConvertTo-Json -Depth 6

Write-Host ""
Write-Host "Device status snapshot:"
$status | ConvertTo-Json -Depth 6

Write-Host ""
Write-Host "Recent commands:"
$commands | ConvertTo-Json -Depth 6
