[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$InstallRoot = "C:\hydra-live",
    [string]$EnvFile,
    [string]$PythonExe,
    [switch]$ConnectQmt,
    [ValidatePattern('^[0-9]{6}\.(SH|SZ|BJ)$')][string[]]$Symbols = @()
)

# Read-only inspection. Output artifacts are the only writes by default. No agent
# commands, state constructors, server requests, task changes, or trading methods.
# -ConnectQmt opts into account/market queries and the shared account lock file.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($Symbols.Count -gt 0 -and -not $ConnectQmt) { throw "-Symbols requires -ConnectQmt" }
if ($Symbols.Count -gt 20) { throw "At most 20 quote fixture symbols are allowed" }
if (-not $EnvFile) { $EnvFile = Join-Path $InstallRoot "config\hydra-live.env" }
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { throw "Private config file is missing" }
if (-not $PythonExe) {
    $matchesPython = @(Get-Content -LiteralPath $EnvFile -Encoding UTF8 | Where-Object {
        $_.Trim() -match '^HYDRA_LIVE_PYTHON='
    })
    if ($matchesPython.Count -ne 1) { throw "Specify -PythonExe or configure one HYDRA_LIVE_PYTHON entry" }
    $PythonExe = ($matchesPython[0].Trim() -split '=', 2)[1].Trim().Trim('"').Trim("'")
}
if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) { throw "Configured Python executable is unavailable" }
$activePointer = Join-Path $InstallRoot "config\active-release.txt"
$commit = (Get-Content -LiteralPath $activePointer -Raw -Encoding UTF8).Trim()
if ($commit -cnotmatch '^[0-9a-f]{40}$') { throw "Invalid active release pointer" }
$releaseRoot = Join-Path (Join-Path $InstallRoot "releases") $commit
$packageRoot = Join-Path $releaseRoot "live_client"
$manifestPath = Join-Path $releaseRoot "release.json"
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $packageRoot -PathType Container)) { throw "Active package is missing" }
$hashLines = @(Get-ChildItem -LiteralPath $packageRoot -Recurse -File | Sort-Object FullName | ForEach-Object {
    $relative = $_.FullName.Substring($packageRoot.Length).TrimStart([char[]]@('\', '/')).Replace('\', '/')
    "$relative=$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant())"
})
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $packageHash = [BitConverter]::ToString($sha.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes([string]::Join("`n", $hashLines))
    )).Replace("-", "").ToLowerInvariant()
}
finally { $sha.Dispose() }
$packageMatches = $manifest.source_commit -ceq $commit -and $manifest.package_sha256 -ceq $packageHash
if (-not $packageMatches) { throw "Active package hash differs from its release manifest; refusing to import it" }
$runtimeHashes = @()
foreach ($name in @("Run-HydraLive.ps1", "Run-HydraOms.ps1", "Inspect-V713SharedOms.ps1", "Run-V713OmsAcceptance.ps1", "hydra_oms_notify.py")) {
    $source = Join-Path $packageRoot "windows\$name"
    # A pre-upgrade inspection can target a release that predates these new tools.
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
    $subdirectory = if ($name -eq "Run-HydraLive.ps1") { "bin" } else { "scripts" }
    $installed = Join-Path (Join-Path $InstallRoot $subdirectory) $name
    $expectedHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
    $actualHash = if (Test-Path -LiteralPath $installed -PathType Leaf) {
        (Get-FileHash -LiteralPath $installed -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { $null }
    $runtimeHashes += @{ name = $name; sha256 = $actualHash; matches_active_package = ($actualHash -ceq $expectedHash) }
}
# Reading process version metadata does not connect to QMT. Missing/ inaccessible
# vendor version metadata is reported as unknown, never inferred from xtquant.
$qmtVersions = @(Get-Process -Name XtMiniQmt, XtQmt, MiniQmt, QMT -ErrorAction SilentlyContinue | ForEach-Object {
    $qmtProcess = $_
    $fileVersion = $null
    $productVersion = $null
    try {
        $info = $qmtProcess.MainModule.FileVersionInfo
        $fileVersion = $info.FileVersion
        $productVersion = $info.ProductVersion
    } catch { }
    @{ process = $qmtProcess.ProcessName; file_version = $fileVersion; product_version = $productVersion }
})

# Export only action executable/script paths and a known OMS command. Raw task
# arguments/XML may contain private env values or credentials and are omitted.
$tasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
    $_.TaskName -like "Hydra-Live-*" -or $_.TaskName -like "Hydra-Oms-*"
} | ForEach-Object {
    $task = $_
    $actions = @($task.Actions | ForEach-Object {
        $action = $_
        $arguments = if ($action.PSObject.Properties["Arguments"]) { [string]$action.Arguments } else { "" }
        $executable = if ($action.PSObject.Properties["Execute"]) { [string]$action.Execute } else { $null }
        $scriptPath = $null
        $operation = $null
        if ($arguments -match '(?i)-File\s+(?:"([^"]+)"|([^\s]+))') {
            $scriptPath = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
        }
        if ($arguments -match '(?i)-Command\s+(pre|buy|sell|cancel|eod|intraday|upload-spool|status)(?:\s|$)') {
            $operation = $Matches[1]
        }
        @{ executable = $executable; script_path = $scriptPath; oms_command = $operation }
    })
    @{ name = $task.TaskName; path = $task.TaskPath; state = [string]$task.State; actions = $actions }
})
$zone = (Get-TimeZone).Id
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$reportPath = Join-Path $OutputDirectory "shared-oms-inspection.json"
$pythonPath = Join-Path $OutputDirectory "local-runtime.json"
$fixturePath = Join-Path $OutputDirectory "qmt-quote-fixtures.json"
$tasksPath = Join-Path $OutputDirectory "tasks.json"
foreach ($path in @($reportPath, $pythonPath, $fixturePath, $tasksPath)) {
    if (Test-Path -LiteralPath $path) { throw "Use a new inspection directory; evidence will not be overwritten" }
}
$inspection = @'
import contextlib
import dataclasses
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from datetime import datetime, timezone

def read_env(path):
    values = {}
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match or match[1] in values:
            raise ValueError("private configuration syntax")
        value = match[2].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[match[1]] = value
    return values

def inspect_journal(path, expected):
    result = {"present": path.is_file(), "binding_present": False, "binding_matches": None,
              "integrity_ok": None, "unknown_count": None, "submitting_count": None,
              "pending_outbox_count": None}
    if not path.is_file():
        return result
    # Never instantiate OmsJournal/LiveStateStore: their constructors write/migrate.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        result["integrity_ok"] = connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "metadata" in tables:
            binding = connection.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
            if binding:
                result["binding_present"] = True
                result["binding_sha256"] = hashlib.sha256(binding[0].encode()).hexdigest()
                result["binding_matches"] = json.loads(binding[0]) == expected
        if "intents" in tables:
            result["unknown_count"] = connection.execute("SELECT COUNT(*) FROM intents WHERE state='UNKNOWN'").fetchone()[0]
            result["submitting_count"] = connection.execute("SELECT COUNT(*) FROM intents WHERE state='SUBMITTING'").fetchone()[0]
        if "outbox" in tables:
            result["pending_outbox_count"] = connection.execute("SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL").fetchone()[0]
    finally:
        connection.close()
    return result

def json_market(value):
    # xtdata may use numpy scalar/arrays; retain numeric/raw quote fields, never
    # stringify arbitrary objects (which could reveal account/session internals).
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): json_market(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_market(item) for item in value]
    if hasattr(value, "tolist"):
        return json_market(value.tolist())
    raise TypeError("unsupported quote value")

def main():
    env_path, output, fixtures, connect, symbol_text = sys.argv[1:6]
    symbol_text = "" if symbol_text == "-" else symbol_text
    values = read_env(env_path)
    fingerprint = hashlib.sha256(values["HYDRA_LIVE_QMT_ACCOUNT_ID"].encode()).hexdigest()
    expected = {"account_alias": values["HYDRA_LIVE_ACCOUNT_ALIAS"],
                "execution_domain": values["HYDRA_LIVE_EXECUTION_DOMAIN"],
                "account_fingerprint": values["HYDRA_LIVE_EXPECTED_ACCOUNT_SHA256"],
                "client_id": values["HYDRA_LIVE_TASK_PREFIX"]}
    state_path = Path(values["HYDRA_LIVE_STATE_DB"])
    if not state_path.is_absolute():
        raise ValueError("state path must be absolute")
    journal = inspect_journal(state_path.with_name("oms-agent.db"), expected)
    result = {"captured_at_utc": datetime.now(timezone.utc).isoformat(),
              "python_version": sys.version.split()[0],
              "account_fingerprint": fingerprint,
              "configured_fingerprint_matches": fingerprint == expected["account_fingerprint"],
              "journal": journal, "qmt_contacted": False, "server_contacted": False,
              "trading_methods_called": False, "blockers": []}
    try:
        result["xtquant_version"] = importlib.metadata.version("xtquant")
    except importlib.metadata.PackageNotFoundError:
        result["xtquant_version"] = None
    result["xtquant_installed"] = importlib.util.find_spec("xtquant") is not None
    if not result["configured_fingerprint_matches"]:
        result["blockers"].append("ACCOUNT_FINGERPRINT_MISMATCH")
    if journal["present"]:
        if not journal["integrity_ok"]:
            result["blockers"].append("JOURNAL_INTEGRITY")
        if not journal["binding_matches"]:
            result["blockers"].append("JOURNAL_BINDING_UNVERIFIED")
        if journal["unknown_count"] or journal["submitting_count"]:
            result["blockers"].append("UNRESOLVED_BROKER_INTENTS")
    holds = [path for path in state_path.parent.glob("HOLD*")
             if path.name == "HOLD" or path.name.startswith(("HOLD-", "HOLD."))]
    result["hold_count"] = len(holds)
    result["spool_pending_count"] = sum(1 for path in (state_path.parent / "oms-spool").glob("*.json") if path.is_file())
    if holds:
        result["blockers"].append("HOLD_PRESENT")
    if connect == "1":
        if result["blockers"]:
            raise ValueError("local evidence blocks QMT query")
        symbols = symbol_text.split(",") if symbol_text else []
        if any(not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", symbol) for symbol in symbols) or len(symbols) > 20:
            raise ValueError("invalid fixture symbols")
        os.environ.update(values)
        # Prevent provider diagnostics (including native stdout/stderr) from
        # leaking account ids into the exported report or terminal.
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            saved = [os.dup(1), os.dup(2)]
            try:
                os.dup2(sink.fileno(), 1)
                os.dup2(sink.fileno(), 2)
                from live_client.config import LiveClientConfig
                from live_client.execution_queue import account_submission_lock
                from live_client.gateway import XtQMTGateway
                cfg = dataclasses.replace(LiveClientConfig.from_env(), trading_enabled=False)
                if cfg.mode != "live":
                    raise ValueError("QMT inspection requires live mode")
                with account_submission_lock(cfg.userdata_dir, cfg.expected_account_sha256):
                    gateway = XtQMTGateway(cfg)
                    try:
                        result["qmt_contacted"] = True
                        gateway.connect()
                        snapshot = gateway.account_snapshot()
                        actual = hashlib.sha256(snapshot.account_id.encode()).hexdigest()
                        if actual != fingerprint:
                            raise ValueError("connected account mismatch")
                        result["qmt_query"] = {"connected": True, "account_fingerprint": actual,
                                               "position_symbol_count": len(snapshot.positions),
                                               "day_order_count": len(gateway.day_orders()),
                                               "frozen_cash_observed": snapshot.frozen_cash is not None}
                        if snapshot.frozen_cash is None:
                            result["blockers"].append("FROZEN_CASH_UNOBSERVED")
                        if symbols:
                            fixture = {"captured_at_utc": datetime.now(timezone.utc).isoformat(),
                                       "symbols": symbols, "ticks": json_market(gateway.xtdata.get_full_tick(symbols)),
                                       "instruments": {symbol: json_market(gateway.xtdata.get_instrument_detail(symbol)) for symbol in symbols}}
                            Path(fixtures).write_text(json.dumps(fixture, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                    finally:
                        gateway.close()
            finally:
                for target, old in zip((1, 2), saved):
                    os.dup2(old, target)
                    os.close(old)
    Path(output).write_text(json.dumps(result, indent=2), encoding="utf-8")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Error type is useful for diagnosis; error text/tracebacks can contain
        # private values, broker payloads, or the account number.
        failure = {"inspection_failed": True, "error_type": type(exc).__name__}
        Path(sys.argv[2]).write_text(json.dumps(failure), encoding="utf-8")
        print(json.dumps(failure))
        sys.exit(1)
'@
$previousPythonPath = $env:PYTHONPATH
$previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
$previousPythonUtf8 = $env:PYTHONUTF8
$previousPythonIoEncoding = $env:PYTHONIOENCODING
$previousErrorActionPreference = $ErrorActionPreference
try {
    $env:PYTHONPATH = $releaseRoot
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $connectFlag = if ($ConnectQmt) { "1" } else { "0" }
    $symbolArgument = if ($Symbols.Count -gt 0) { $Symbols -join "," } else { "-" }
    $ErrorActionPreference = "Continue"
    $nativeOutput = $inspection | & $PythonExe - $EnvFile $pythonPath $fixturePath $connectFlag $symbolArgument 2>&1
    $inspectionExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorActionPreference
    if ($inspectionExitCode -ne 0) { throw "Read-only inspection failed; provider diagnostics suppressed to protect private values" }
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
    $env:PYTHONUTF8 = $previousPythonUtf8
    $env:PYTHONIOENCODING = $previousPythonIoEncoding
    $ErrorActionPreference = $previousErrorActionPreference
}
$local = Get-Content -LiteralPath $pythonPath -Raw -Encoding UTF8 | ConvertFrom-Json
$blockers = @($local.blockers)
if ($zone -ne "China Standard Time") { $blockers += "WINDOWS_TIMEZONE" }
if (@($runtimeHashes | Where-Object { -not $_.matches_active_package }).Count -gt 0) {
    $blockers += "INSTALLED_RUNTIME_HASH_MISMATCH"
}
$legacyEnabled = @($tasks | Where-Object { $_.name -like "Hydra-Live-*" -and $_.state -ne "Disabled" })
$omsEnabled = @($tasks | Where-Object { $_.name -like "Hydra-Oms-*" -and $_.state -ne "Disabled" })
if ($legacyEnabled.Count -gt 0 -and $omsEnabled.Count -gt 0) { $blockers += "DUAL_TASK_SCHEDULE" }
$report = @{
    status = "INSPECTED"
    captured_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    windows_timezone = $zone
    active_release = $commit
    active_pointer_sha256 = (Get-FileHash -LiteralPath $activePointer -Algorithm SHA256).Hash.ToLowerInvariant()
    release_manifest_sha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    package_sha256 = $packageHash
    package_matches_manifest = $packageMatches
    installed_runtime_hashes = $runtimeHashes
    qmt_process_versions = $qmtVersions
    tasks = $tasks
    local = $local
    blockers = $blockers
    tasks_modified = $false
    configuration_modified = $false
    journal_modified = $false
}
$report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $reportPath -Encoding UTF8
ConvertTo-Json -InputObject $tasks -Depth 6 | Set-Content -LiteralPath $tasksPath -Encoding UTF8
@{ status = "INSPECTED"; report = $reportPath; blocker_count = $blockers.Count; qmt_contacted = [bool]$ConnectQmt } | ConvertTo-Json
