param([switch]$ReplaceLegacyTasks)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskScript = "C:\hydra-live\scripts\Invoke-HydraLiveOperations.ps1"
if (-not (Test-Path -LiteralPath $taskScript -PathType Leaf)) { throw "Operations script is missing: $taskScript" }
$items = @(
    @{ Name = "Hydra-Live-SettleClose-1510"; Stage = "settle-close"; Hour = 15; Minute = 10; LimitMinutes = 30 },
    @{ Name = "Hydra-Live-MarketBackup-1530"; Stage = "market-backup"; Hour = 15; Minute = 30; LimitMinutes = 90 },
    @{ Name = "Hydra-Live-Retry-1600"; Stage = "retry"; Hour = 16; Minute = 0; LimitMinutes = 20 },
    @{ Name = "Hydra-Live-QueryPreflight-1800"; Stage = "query-preflight"; Hour = 18; Minute = 0; LimitMinutes = 30 }
)
$legacyNames = @(
    "Hydra-Live-Settle-1510",
    "Hydra-Live-Trigger-1600",
    "Hydra-Live-Query-1800"
)
$legacyTasks = @($legacyNames | ForEach-Object {
    Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue
})
if ($legacyTasks -and -not $ReplaceLegacyTasks) {
    throw "Legacy Hydra tasks exist. Review them, then rerun with -ReplaceLegacyTasks to disable them."
}
if ($ReplaceLegacyTasks) {
    $legacyTasks | Disable-ScheduledTask | Out-Null
}
foreach ($item in $items) {
    $action = New-ScheduledTaskAction -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$taskScript`" -Stage $($item.Stage)"
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At ([datetime]::Today.Date.AddHours($item.Hour).AddMinutes($item.Minute))
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes $item.LimitMinutes) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries:$false
    Register-ScheduledTask -TaskName $item.Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Hydra live weekday $($item.Stage); server makes residual-order decisions." -Force | Out-Null
}
