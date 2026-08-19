$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidPath = Join-Path $projectRoot "runtime\state\service.pid"

$allProcesses = @(Get-CimInstance Win32_Process)
$serviceProcesses = @(
    $allProcesses | Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and
        $_.CommandLine -like "*$projectRoot*" -and
        $_.CommandLine -like "*bybit_signal*serve*"
    }
)

if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $servicePid = [int](Get-Content -Raw -LiteralPath $pidPath)
    $pidProcess = $allProcesses | Where-Object { $_.ProcessId -eq $servicePid }
    if (
        $null -ne $pidProcess -and
        ($pidProcess.CommandLine -notlike "*$projectRoot*" -or
         $pidProcess.CommandLine -notlike "*bybit_signal*serve*")
    ) {
        throw "PID $servicePid does not belong to this bybit_signal service; refusing to stop it."
    }
}

if ($serviceProcesses.Count -eq 0) {
    if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
        Remove-Item -LiteralPath $pidPath -Force
        Write-Output "Removed stale service PID file."
    } else {
        Write-Output "Signal service is not running."
    }
    exit 0
}

# The Windows venv launcher may own a second Python worker. Stop every verified
# bybit_signal serve process under this project, children before parents, so an
# old worker cannot survive a deploy.
$serviceIds = @($serviceProcesses | Select-Object -ExpandProperty ProcessId)
$targetIds = @($serviceIds)
do {
    $children = @(
        $allProcesses | Where-Object {
            $_.ParentProcessId -in $targetIds -and $_.ProcessId -notin $targetIds
        }
    )
    $targetIds += @($children | Select-Object -ExpandProperty ProcessId)
} while ($children.Count -gt 0)
$ordered = @(
    $allProcesses |
        Where-Object { $_.ProcessId -in $targetIds } |
        Sort-Object -Property @{ Expression = {
            $depth = 0
            $parentId = $_.ParentProcessId
            while ($parentId -in $targetIds) {
                $depth++
                $parent = $allProcesses | Where-Object { $_.ProcessId -eq $parentId }
                if ($null -eq $parent) { break }
                $parentId = $parent.ParentProcessId
            }
            $depth
        }; Descending = $true }
)
foreach ($processInfo in $ordered) {
    Stop-Process -Id $processInfo.ProcessId -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
$remaining = @(
    Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -in $targetIds }
)
foreach ($processInfo in $remaining) {
    Stop-Process -Id $processInfo.ProcessId -Force -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    Remove-Item -LiteralPath $pidPath -Force
}
Write-Output "Signal service process tree stopped: PID(s) $($targetIds -join ', ')"
