$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidPath = Join-Path $projectRoot "runtime\state\service.pid"

if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    Write-Output "Signal service is not running (PID file absent)."
    exit 0
}

$servicePid = [int](Get-Content -Raw -LiteralPath $pidPath)
$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $servicePid" -ErrorAction SilentlyContinue
if ($null -eq $processInfo) {
    Remove-Item -LiteralPath $pidPath -Force
    Write-Output "Removed stale PID file for $servicePid."
    exit 0
}
if ($processInfo.CommandLine -notlike "*bybit_signal*serve*") {
    throw "PID $servicePid does not belong to bybit_signal serve; refusing to stop it."
}

Stop-Process -Id $servicePid
Remove-Item -LiteralPath $pidPath -Force
Write-Output "Signal service stopped: PID=$servicePid"
