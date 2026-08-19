$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidPath = Join-Path $projectRoot "runtime\state\service.pid"
$serviceProcesses = @(
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and
        $_.CommandLine -like "*$projectRoot*" -and
        $_.CommandLine -like "*bybit_signal*serve*"
    }
)

if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    if ($serviceProcesses.Count -gt 0) {
        $orphanIds = ($serviceProcesses | Select-Object -ExpandProperty ProcessId) -join ", "
        Write-Output "Signal service has orphan process(es) but no PID file: $orphanIds"
    } else {
        Write-Output "Signal service is not running (PID file absent)."
    }
    exit 1
}

$servicePid = [int](Get-Content -Raw -LiteralPath $pidPath)
$process = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
if ($null -eq $process -or $serviceProcesses.Count -eq 0) {
    Write-Output "Signal service is not running (stale PID $servicePid)."
    exit 1
}

$processIds = ($serviceProcesses | Select-Object -ExpandProperty ProcessId) -join ", "
Write-Output "Signal service is running: launcher PID=$servicePid process tree PID(s)=$processIds"
