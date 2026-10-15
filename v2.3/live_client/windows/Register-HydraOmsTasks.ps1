param(
    [Parameter(Mandatory = $true)]
    [string]$InspectionDirectory,
    [switch]$DisableLegacyTasks,
    [switch]$StageOnly
)

# Stage the OMS agent tasks disabled, verify them, then (with -DisableLegacyTasks)
# disable every legacy Hydra-Live-* task and enable the OMS ones. Legacy tasks are
# disabled, never deleted. Rollback requires broker reconciliation by the operator;
# this script never automatically re-enables a legacy trading schedule on failure.
# -InspectionDirectory must hold tasks.json exported by Inspect-V713SharedOms.ps1
# (or the prior operator Inspect-HydraTasks.ps1) first.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ((Get-TimeZone).Id -ne "China Standard Time") {
    throw "OMS schedules require Windows timezone China Standard Time; no tasks were modified"
}
if ($StageOnly -and $DisableLegacyTasks) {
    throw "-StageOnly cannot be combined with -DisableLegacyTasks"
}
$running = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
    ($_.TaskName -like "Hydra-Live-*" -or $_.TaskName -like "Hydra-Oms-*") -and $_.State -eq "Running"
})
if ($running.Count -gt 0) { throw "Wait for all Hydra tasks to finish before registration" }

if (-not (Test-Path -LiteralPath (Join-Path $InspectionDirectory "tasks.json") -PathType Leaf)) {
    throw "Export the current tasks first with Inspect-V713SharedOms.ps1 -OutputDirectory $InspectionDirectory"
}
$runner = "C:\hydra-live\scripts\Run-HydraOms.ps1"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "OMS runner is missing: $runner" }

# Cycle steps wait for the account lock (oms_agent.LOCK_WAIT_SECONDS) because they can start in
# the same second as the every-minute intraday task; LimitMinutes covers that wait plus the step.
$items = @(
    @{ Name = "Hydra-Oms-Pre-0900";     Args = "-Command pre";       Hour = 9;  Minute = 0;  LimitMinutes = 5;   RestartCount = 1 },
    @{ Name = "Hydra-Oms-Buy-0914";     Args = "-Command buy";       Hour = 9;  Minute = 14; LimitMinutes = 10;  RestartCount = 0 },
    @{ Name = "Hydra-Oms-Pre-1445";     Args = "-Command pre";       Hour = 14; Minute = 45; LimitMinutes = 5;   RestartCount = 1 },
    @{ Name = "Hydra-Oms-Cancel-1455";  Args = "-Command cancel";    Hour = 14; Minute = 55; LimitMinutes = 4;   RestartCount = 0 },
    @{ Name = "Hydra-Oms-Sell-1456";    Args = "-Command sell";      Hour = 14; Minute = 56; LimitMinutes = 6;   RestartCount = 0 },
    @{ Name = "Hydra-Oms-Eod-1505";     Args = "-Command eod";       Hour = 15; Minute = 5;  LimitMinutes = 10;  RestartCount = 1 },
    @{ Name = "Hydra-Oms-Eod-1530";     Args = "-Command eod";       Hour = 15; Minute = 30; LimitMinutes = 10;  RestartCount = 1 },
    @{ Name = "Hydra-Oms-Upload-1800";  Args = "-Command upload-spool"; Hour = 18; Minute = 0;  LimitMinutes = 10;  RestartCount = 0 },
    # Every minute 09:15-15:00: dashboard orders/cancels, then one status snapshot. Each run takes
    # seconds and skips its minute (exit 0) while a cycle step holds the account.
    @{ Name = "Hydra-Oms-Intraday";     Args = "-Command intraday";  Hour = 9;  Minute = 15; LimitMinutes = 2;   RestartCount = 0; RepeatMinutes = 1; RepeatHours = 5.75 }
)
$legacyTasks = @(Get-ScheduledTask -TaskName "Hydra-Live-*" -ErrorAction SilentlyContinue)
if ($legacyTasks.Count -gt 0 -and -not $DisableLegacyTasks -and -not $StageOnly) {
    throw "Legacy Hydra-Live tasks exist ($($legacyTasks.Count)). Rerun with -DisableLegacyTasks; two schedules must never run together."
}
$registeredNames = @()
try {
    # Preserve the old task as evidence. Staging does not change its state.
    if (-not $StageOnly) {
        Get-ScheduledTask -TaskName "Hydra-Oms-Manual" -ErrorAction SilentlyContinue | Disable-ScheduledTask | Out-Null
    }
    foreach ($item in $items) {
        # Restart only the read-only steps; a restarted sell/buy/cancel could act twice.
        $action = New-ScheduledTaskAction -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" $($item.Args)"
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At ([datetime]::Today.Date.AddHours($item.Hour).AddMinutes($item.Minute))
        if ($item.ContainsKey("RepeatMinutes")) {
            $at = [datetime]::Today.Date.AddHours($item.Hour).AddMinutes($item.Minute)
            $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $at -RepetitionInterval (New-TimeSpan -Minutes $item.RepeatMinutes) -RepetitionDuration (New-TimeSpan -Minutes ([int]($item.RepeatHours * 60)))).Repetition
        }
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
    if (-not $StageOnly) {
        if ($DisableLegacyTasks) {
            $legacyTasks | Disable-ScheduledTask | Out-Null
        }
        $registeredNames | ForEach-Object { Enable-ScheduledTask -TaskName $_ | Out-Null }
        $notEnabled = @($registeredNames | Where-Object { (Get-ScheduledTask -TaskName $_).State -eq "Disabled" })
        if ($notEnabled) { throw "Not every OMS task was enabled: $notEnabled" }
    }
}
catch {
    $registeredNames | ForEach-Object { Disable-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue | Out-Null }
    throw
}
if ($StageOnly) {
    Write-Output "OMS tasks staged disabled: $($registeredNames -join ', '); legacy task states unchanged"
}
else {
    Write-Output "OMS tasks enabled: $($registeredNames -join ', ')"
}
