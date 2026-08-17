$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidPath = Join-Path $projectRoot "runtime\state\service.pid"

if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    Write-Output "Signal service is not running (PID file absent)."
    exit 1
}

$servicePid = [int](Get-Content -Raw -LiteralPath $pidPath)
$process = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
if ($null -eq $process) {
    Write-Output "Signal service is not running (stale PID $servicePid)."
    exit 1
}

Write-Output "Signal service is running: PID=$servicePid process=$($process.ProcessName)"
