<# Historical registration for the one-time 2026-09-04 09:10 submit task. #>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskName = "Hydra-Live-Submit-20260904-0910"
$submitScript = "C:\hydra-live\scripts\Invoke-HydraLiveSubmit-20260904.ps1"
if (-not (Test-Path -LiteralPath $submitScript -PathType Leaf)) { throw "Submit script is missing: $submitScript" }

$action = New-ScheduledTaskAction -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$submitScript`""
$trigger = New-ScheduledTaskTrigger -Once -At ([datetime]"2026-09-04T09:10:00")
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries:$false
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "One-time Hydra live submit at 2026-09-04 09:10." -Force | Out-Null
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, TaskPath, State | Format-Table -AutoSize
