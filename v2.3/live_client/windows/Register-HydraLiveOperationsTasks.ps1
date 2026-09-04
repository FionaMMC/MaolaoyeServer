Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskScript = "C:\hydra-live\scripts\Invoke-HydraLiveOperations.ps1"
if (-not (Test-Path -LiteralPath $taskScript -PathType Leaf)) { throw "Operations script is missing: $taskScript" }
$items = @(
    @{ Name = "Hydra-Live-Settle-1510"; Stage = "settle"; Hour = 15; Minute = 10; LimitMinutes = 20 },
    @{ Name = "Hydra-Live-MarketBackup-1530"; Stage = "market-backup"; Hour = 15; Minute = 30; LimitMinutes = 90 },
    @{ Name = "Hydra-Live-Trigger-1600"; Stage = "trigger"; Hour = 16; Minute = 0; LimitMinutes = 20 },
    @{ Name = "Hydra-Live-Query-1800"; Stage = "query"; Hour = 18; Minute = 0; LimitMinutes = 20 }
)
foreach ($item in $items) {
    $action = New-ScheduledTaskAction -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$taskScript`" -Stage $($item.Stage)"
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At ([datetime]::Today.Date.AddHours($item.Hour).AddMinutes($item.Minute))
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes $item.LimitMinutes) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries:$false
    Register-ScheduledTask -TaskName $item.Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Hydra live weekday $($item.Stage); server makes residual-order decisions." -Force | Out-Null
}
