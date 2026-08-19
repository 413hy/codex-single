param(
    [string]$Config = "config/system.local.yaml"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$configPath = Join-Path $projectRoot $Config
$stateRoot = Join-Path $projectRoot "runtime\state"
$logRoot = Join-Path $projectRoot "runtime\logs"
$pidPath = Join-Path $stateRoot "service.pid"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Virtual environment Python not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Configuration not found: $configPath"
}
New-Item -ItemType Directory -Force -Path $stateRoot, $logRoot | Out-Null

$running = @(
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and
        $_.CommandLine -like "*$projectRoot*" -and
        $_.CommandLine -like "*bybit_signal*serve*"
    }
)
if ($running.Count -gt 0) {
    $runningIds = ($running | Select-Object -ExpandProperty ProcessId) -join ", "
    throw "Signal service process tree is already running: PID(s) $runningIds"
}

if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $existingPid = [int](Get-Content -Raw -LiteralPath $pidPath)
    if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
        throw "Signal service is already running with PID $existingPid"
    }
    Remove-Item -LiteralPath $pidPath -Force
}

$process = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList @("-m", "bybit_signal", "serve", "--config", $configPath) `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logRoot "service.stdout.log") `
    -RedirectStandardError (Join-Path $logRoot "service.stderr.log") `
    -PassThru

Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ascii
Start-Sleep -Seconds 2
if ($process.HasExited) {
    throw "Signal service exited during startup. Check runtime/logs/service.stderr.log"
}
Write-Output "Signal service started with PID $($process.Id)"
