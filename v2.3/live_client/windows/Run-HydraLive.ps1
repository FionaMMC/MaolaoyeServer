[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("doctor", "query", "preflight", "submit", "settle")]
    [string]$Command,

    [ValidatePattern("^\d{8}$")]
    [string]$Date,

    [string]$InstallRoot = "C:\hydra-live",
    [string]$EnvFile,
    [string]$PythonExe = "python",
    [string]$MockState
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Import-HydraEnvironment {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Hydra private env file not found: $Path"
    }
    $seen = @{}
    foreach ($rawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        if ($line -notmatch "^([A-Za-z_][A-Za-z0-9_]*)=(.*)$") {
            throw "Invalid env entry; expected NAME=value (secret not printed)"
        }
        $name = $Matches[1]
        if ($seen.ContainsKey($name)) {
            throw "Duplicate env name is not allowed: $name"
        }
        $seen[$name] = $true
        $value = $Matches[2].Trim()
        if (
            $value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

if ($Command -ne "doctor" -and -not $Date) {
    throw "-$Command requires an explicit -Date YYYYMMDD"
}
if ($MockState -and $Command -notin @("preflight", "submit", "settle")) {
    throw "-MockState is only valid for preflight, submit, or settle"
}
if (-not $EnvFile) {
    $EnvFile = Join-Path $InstallRoot "config\hydra-live.env"
}

$activePointer = Join-Path $InstallRoot "config\active-release.txt"
if (-not (Test-Path -LiteralPath $activePointer -PathType Leaf)) {
    throw "Hydra active release pointer not found: $activePointer"
}
$releaseId = (Get-Content -LiteralPath $activePointer -Raw -Encoding UTF8).Trim()
if ($releaseId -notmatch "^[0-9a-f]{40}$") {
    throw "Hydra active release pointer is invalid"
}
$releaseRoot = Join-Path (Join-Path $InstallRoot "releases") $releaseId
$packageRoot = Join-Path $releaseRoot "live_client"
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "cli.py") -PathType Leaf)) {
    throw "Hydra active release is incomplete: $releaseId"
}
if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) {
    throw "Python executable not found: $PythonExe"
}

Import-HydraEnvironment -Path $EnvFile

$runtimeRoot = Join-Path $InstallRoot "runtime"
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
$lockSuffix = if ($Date) { "$Command-$Date" } else { $Command }
$lockPath = Join-Path $runtimeRoot "$lockSuffix.lock"
$lockStream = $null
$previousPythonPath = $env:PYTHONPATH
$previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
try {
    try {
        $lockStream = [IO.File]::Open(
            $lockPath,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    }
    catch [IO.IOException] {
        throw "Another Hydra '$Command' process already holds $lockPath"
    }

    $env:PYTHONPATH = $releaseRoot
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $arguments = @("-m", "live_client.cli", $Command)
    if ($Date) {
        $arguments += @("--date", $Date)
    }
    if ($MockState) {
        $arguments += @("--mock-state", $MockState)
    }
    & $PythonExe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Hydra '$Command' failed with exit code $LASTEXITCODE"
    }
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}
