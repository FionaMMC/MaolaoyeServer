param([switch]$ReplaceLegacyTasks)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskScript = "C:\hydra-live\scripts\Invoke-HydraLiveOperations.ps1"
if (-not (Test-Path -LiteralPath $taskScript -PathType Leaf)) { throw "Operations script is missing: $taskScript" }
$items = @(
    @{ Name = "Hydra-Live-CancelOpen-1455"; Stage = "cancel-open"; Hour = 14; Minute = 55; LimitMinutes = 5; RestartCount = 0 },
    @{ Name = "Hydra-Live-MarketBackup-1530"; Stage = "market-backup"; Hour = 15; Minute = 30; LimitMinutes = 90; RestartCount = 0 },
    @{ Name = "Hydra-Live-SettleClose-1605"; Stage = "settle-close"; Hour = 16; Minute = 5; LimitMinutes = 10; RestartCount = 1; RestartMinutes = 5 },
    @{ Name = "Hydra-Live-Retry-1620"; Stage = "retry"; Hour = 16; Minute = 20; LimitMinutes = 20; RestartCount = 0 },
    @{ Name = "Hydra-Live-QueryPreflight-1800"; Stage = "query-preflight"; Hour = 18; Minute = 0; LimitMinutes = 30; RestartCount = 0 }
)
$legacyNames = @(
    "Hydra-Live-Settle-1510",
    "Hydra-Live-SettleClose-1510",
    "Hydra-Live-Trigger-1600",
    "Hydra-Live-Retry-1600",
    "Hydra-Live-Query-1800"
)
$legacyTasks = @($legacyNames | ForEach-Object {
    Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue
})
if ($legacyTasks -and -not $ReplaceLegacyTasks) {
    throw "Legacy Hydra tasks exist. Review them, then rerun with -ReplaceLegacyTasks to disable them."
}
# Stage and verify every replacement in the disabled state before touching a
# legacy task.  This prevents both a half-cutover outage and duplicate schedules.
$registeredNames = @()
$legacyEnabledBefore = @{}
foreach ($legacy in $legacyTasks) {
    $legacyEnabledBefore[$legacy.TaskName] = $legacy.State -ne "Disabled"
}
try {
    foreach ($item in $items) {
        $action = New-ScheduledTaskAction -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$taskScript`" -Stage $($item.Stage)"
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
            $settingsParameters.RestartInterval = New-TimeSpan -Minutes $item.RestartMinutes
        }
        $settings = New-ScheduledTaskSettingsSet @settingsParameters
        Register-ScheduledTask -TaskName $item.Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Hydra live weekday $($item.Stage); server makes residual-order decisions." -Force | Out-Null
        $registered = Get-ScheduledTask -TaskName $item.Name -ErrorAction Stop
        if ($registered.State -ne "Disabled") {
            throw "Staged Hydra operations task is unexpectedly enabled: $($item.Name)"
        }
        $registeredNames += $item.Name
    }
    if ($registeredNames.Count -ne $items.Count) {
        throw "Not every Hydra operations task was staged"
    }
    if ($ReplaceLegacyTasks) {
        $legacyTasks | Disable-ScheduledTask | Out-Null
    }
    $registeredNames | ForEach-Object {
        Enable-ScheduledTask -TaskName $_ | Out-Null
    }
    $notEnabled = @($registeredNames | Where-Object {
        (Get-ScheduledTask -TaskName $_ -ErrorAction Stop).State -eq "Disabled"
    })
    if ($notEnabled) {
        throw "Not every Hydra operations task was enabled: $notEnabled"
    }
}
catch {
    $registeredNames | ForEach-Object {
        Disable-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue | Out-Null
    }
    if ($ReplaceLegacyTasks) {
        foreach ($legacy in $legacyTasks) {
            if ($legacyEnabledBefore[$legacy.TaskName]) {
                Enable-ScheduledTask -TaskName $legacy.TaskName `
                    -ErrorAction SilentlyContinue | Out-Null
            }
        }
    }
    throw
}
