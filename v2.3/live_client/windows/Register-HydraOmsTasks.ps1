param(
    [Parameter(Mandatory = $true)]
    [string]$InspectionDirectory,
    [switch]$DisableLegacyTasks
)

# Stage the OMS agent tasks disabled, verify them, then (with -DisableLegacyTasks)
# disable every legacy Hydra-Live-* task and enable the OMS ones. Legacy tasks are
# disabled, never deleted, so a rollback is Enable-ScheduledTask on them.
# -InspectionDirectory must hold tasks.json exported by Inspect-HydraTasks.ps1 first.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath (Join-Path $InspectionDirectory "tasks.json") -PathType Leaf)) {
    throw "Export the current tasks first with Inspect-HydraTasks.ps1 -OutputDirectory $InspectionDirectory"
}
$runner = "C:\hydra-live\scripts\Run-HydraOms.ps1"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "OMS runner is missing: $runner" }

$items = @(
    @{ Name = "Hydra-Oms-Pre-0900";     Args = "-Command pre";                     Hour = 9;  Minute = 0;  LimitMinutes = 5;   RestartCount = 1 },
    @{ Name = "Hydra-Oms-Buy-0914";     Args = "-Command buy -PollUntil 1000";     Hour = 9;  Minute = 14; LimitMinutes = 60;  RestartCount = 0 },
    @{ Name = "Hydra-Oms-Pre-1445";     Args = "-Command pre";                     Hour = 14; Minute = 45; LimitMinutes = 5;   RestartCount = 1 },
    @{ Name = "Hydra-Oms-Cancel-1455";  Args = "-Command cancel";                  Hour = 14; Minute = 55; LimitMinutes = 2;   RestartCount = 0 },
    @{ Name = "Hydra-Oms-Sell-1456";    Args = "-Command sell -PollUntil 1501";    Hour = 14; Minute = 56; LimitMinutes = 8;   RestartCount = 0 },
    @{ Name = "Hydra-Oms-Eod-1505";     Args = "-Command eod";                     Hour = 15; Minute = 5;  LimitMinutes = 10;  RestartCount = 1 },
    @{ Name = "Hydra-Oms-Eod-1530";     Args = "-Command eod";                     Hour = 15; Minute = 30; LimitMinutes = 10;  RestartCount = 1 },
    @{ Name = "Hydra-Oms-Upload-1800";  Args = "-Command upload-spool";            Hour = 18; Minute = 0;  LimitMinutes = 10;  RestartCount = 0 }
)
$legacyTasks = @(Get-ScheduledTask -TaskName "Hydra-Live-*" -ErrorAction SilentlyContinue)
if ($legacyTasks.Count -gt 0 -and -not $DisableLegacyTasks) {
    throw "Legacy Hydra-Live tasks exist ($($legacyTasks.Count)). Rerun with -DisableLegacyTasks; two schedules must never run together."
}
$legacyEnabledBefore = @{}
foreach ($legacy in $legacyTasks) { $legacyEnabledBefore[$legacy.TaskName] = $legacy.State -ne "Disabled" }
$registeredNames = @()
try {
    foreach ($item in $items) {
        # Restart only the read-only steps; a restarted sell/buy/cancel could act twice.
        $action = New-ScheduledTaskAction -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" $($item.Args)"
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At ([datetime]::Today.Date.AddHours($item.Hour).AddMinutes($item.Minute))
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
        $settingsParameters = @{
            ExecutionTimeLimit = New-TimeSpan -Minutes $item.LimitMinutes
            MultipleInstances = "IgnoreNew"
            AllowStartIfOnBatteries = $false
            StartWhenAvailable = $false
            Disable = $true
        }
        if ($item.RestartCount -gt 0) {
            $settingsParameters.RestartCount = $item.RestartCount
            $settingsParameters.RestartInterval = New-TimeSpan -Minutes 1
        }
        $settings = New-ScheduledTaskSettingsSet @settingsParameters
        Register-ScheduledTask -TaskName $item.Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Hydra OMS agent: $($item.Args)" -Force | Out-Null
        if ((Get-ScheduledTask -TaskName $item.Name -ErrorAction Stop).State -ne "Disabled") {
            throw "Staged OMS task is unexpectedly enabled: $($item.Name)"
        }
        $registeredNames += $item.Name
    }
    if ($DisableLegacyTasks) {
        $legacyTasks | Disable-ScheduledTask | Out-Null
    }
    $registeredNames | ForEach-Object { Enable-ScheduledTask -TaskName $_ | Out-Null }
    $notEnabled = @($registeredNames | Where-Object { (Get-ScheduledTask -TaskName $_).State -eq "Disabled" })
    if ($notEnabled) { throw "Not every OMS task was enabled: $notEnabled" }
}
catch {
    $registeredNames | ForEach-Object { Disable-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue | Out-Null }
    foreach ($legacy in $legacyTasks) {
        if ($legacyEnabledBefore[$legacy.TaskName]) {
            Enable-ScheduledTask -TaskName $legacy.TaskName -ErrorAction SilentlyContinue | Out-Null
        }
    }
    throw
}
Write-Output "OMS tasks enabled: $($registeredNames -join ', ')"
