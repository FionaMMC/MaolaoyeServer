[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^\d{8}$")]
    [string]$TradeDate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$installRoot = "C:\hydra-live"
$envFile = Join-Path $installRoot "config\hydra-live.env"
$runner = Join-Path $installRoot "bin\Run-HydraLive.ps1"
$logFile = Join-Path $installRoot "logs\hydra-live-submit-$TradeDate.log"

function Import-HydraPrivateEnvironment {
    foreach ($rawLine in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        if ($line -notmatch "^([A-Za-z_][A-Za-z0-9_]*)=(.*)$") { throw "Invalid private env entry" }
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($Matches[1], $value, "Process")
    }
}

function Send-WeComNotification([string]$Message) {
    if ([string]::IsNullOrWhiteSpace($env:HYDRA_LIVE_WECHAT_WEBHOOK)) { return }
    try {
        $body = @{ msgtype = "text"; text = @{ content = $Message } } |
            ConvertTo-Json -Compress -Depth 4
        Invoke-RestMethod -Method Post -Uri $env:HYDRA_LIVE_WECHAT_WEBHOOK `
            -ContentType "application/json; charset=utf-8" -Body $body | Out-Null
    } catch { Write-Warning "Enterprise WeChat notification failed: $($_.Exception.Message)" }
}

Import-HydraPrivateEnvironment
if ([string]::IsNullOrWhiteSpace($env:HYDRA_LIVE_PYTHON)) {
    throw "HYDRA_LIVE_PYTHON is required; fallback Python is forbidden"
}
$pythonExe = $env:HYDRA_LIVE_PYTHON
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "HYDRA_LIVE_PYTHON does not exist"
}

$phase = "submit"
try {
    # Morning submit is deliberately offline from the server. The Python
    # client re-hashes the frozen batch and requires last night's PASS receipt.
    $submitLines = @(& $runner -Command submit -Date $TradeDate -PythonExe $pythonExe 2>&1)
    $submitOutput = $submitLines | Out-String
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) submit succeeded`n$submitOutput"
    Send-WeComNotification "[Hydra live] $TradeDate submit completed; review the 14:55 cancel request and 16:05 final settlement notifications."
} catch {
    $message = "$phase failed: $($_.Exception.Message)"
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) $message"
    Send-WeComNotification "[Hydra live] $TradeDate automatic submit was not completed. $message"
    throw
}
