[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^\d{8}$")]
    [string]$TradeDate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$installRoot = "C:\hydra-live"
$submitScript = Join-Path $installRoot "scripts\Invoke-HydraLiveSubmit.ps1"
if (-not (Test-Path -LiteralPath $submitScript -PathType Leaf)) {
    throw "Submit script is missing: $submitScript"
}

$culture = [Globalization.CultureInfo]::InvariantCulture
$runAt = [datetime]::ParseExact(
    "${TradeDate}0910", "yyyyMMddHHmm", $culture
)
if ($runAt -le (Get-Date)) {
    throw "Refusing to register a submit task in the past: $TradeDate 09:10"
}

$taskName = "Hydra-Live-Submit-$TradeDate-0910"
$powershellExe = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
$submitArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$submitScript`" -TradeDate $TradeDate"
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath "\" `
    -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    $actions = @($existing.Actions)
    $triggers = @($existing.Triggers)
    $expectedUser = "$env:USERDOMAIN\$env:USERNAME"
    $sameAction = (
        $actions.Count -eq 1 -and
        $actions[0].Execute -ieq $powershellExe -and
        $actions[0].Arguments -ceq $submitArguments
    )
    $sameTrigger = (
        $triggers.Count -eq 1 -and
        $triggers[0].CimClass.CimClassName -eq "MSFT_TaskTimeTrigger" -and
        [string]::IsNullOrWhiteSpace(
            [string]$triggers[0].Repetition.Interval
        ) -and
        ([datetime]$triggers[0].StartBoundary).ToString("yyyyMMddHHmm") -eq
            $runAt.ToString("yyyyMMddHHmm")
    )
    $samePrincipal = $existing.Principal.UserId -ieq $expectedUser
    if (-not $sameAction -or -not $sameTrigger -or -not $samePrincipal) {
        throw "Existing submit task has different action, trigger, or principal: $taskName"
    }
    if ($existing.State -eq "Disabled") {
        throw "Existing submit task is disabled: $taskName"
    }
    @{
        status = "ALREADY_REGISTERED"
        task_name = $taskName
        trade_date = $TradeDate
        run_at = $runAt.ToString("o")
    } | ConvertTo-Json
    return
}

$action = New-ScheduledTaskAction -Execute $powershellExe `
    -Argument $submitArguments
$trigger = New-ScheduledTaskTrigger -Once -At $runAt
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries:$false
Register-ScheduledTask -TaskName $taskName -TaskPath "\" -Action $action `
    -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Hydra live one-time offline submit for $TradeDate at 09:10." |
    Out-Null

@{
    status = "REGISTERED"
    task_name = $taskName
    trade_date = $TradeDate
    run_at = $runAt.ToString("o")
} | ConvertTo-Json
