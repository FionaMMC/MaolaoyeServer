# Hydra MiniQMT client upgrade and cutover

This runbook upgrades only the Windows Hydra live client. It does not stage a
target, create an order, contact the server, connect to MiniQMT or change a
Task Scheduler entry during installation.

## 1. Preconditions

- The source checkout is at the reviewed full Git commit and `live_client` is
  clean. Use `git pull --ff-only`; never deploy an uncommitted working tree.
- `C:\hydra-live\config\hydra-live.env` remains private and already contains the
  approved live account, account fingerprint, API key, HTTP acknowledgement,
  isolated state/log/userdata paths and risk settings.
- The MiniQMT Python executable is known. Pass its full path; do not assume the
  Windows service account has the same `PATH` as an interactive login.
- No Hydra query, preflight, submit or settle task is currently running.
- Record the next exchange trading date from the frozen Hydra calendar. Do not
  calculate it as “today plus one day.”

## 2. Install an immutable release

From the checked-out `v2.3` directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\live_client\windows\Install-HydraLiveClient.ps1 `
  -SourceRoot C:\src\MaolaoyeServer\v2.3 `
  -InstallRoot C:\hydra-live `
  -PythonExe C:\path\to\python.exe
```

Expected final JSON:

```json
{
  "status": "INSTALLED",
  "offline_acceptance": "PASS",
  "local_doctor": "PASS",
  "env_preserved": true,
  "state_preserved": true,
  "tasks_modified": false
}
```

The complete Git SHA is the directory name under `releases`. Before the local
schema doctor runs, the installer makes a consistent SQLite backup and copies
the private env, previous runner and active pointer under a timestamped private
`backups` directory. It then validates every Python file and exercises only
synthetic mock orders. It does not print the API key or real QMT account id.

## 3. Verify the installed client without network or QMT

```powershell
C:\hydra-live\bin\Run-HydraLive.ps1 `
  -Command doctor `
  -InstallRoot C:\hydra-live `
  -PythonExe C:\path\to\python.exe

$env:PYTHONPATH = "C:\hydra-live\releases\<full-git-sha>"
C:\path\to\python.exe -m live_client.offline_acceptance
```

Both reports must say `server_contacted=false` and `qmt_contacted=false` or
`real_qmt_contacted=false`. The acceptance scenarios must all be `PASS`:

- missing preflight receipt: submit stops before QMT is opened;
- server down plus repeated submit: the first invocation makes the expected mock
  broker calls and the repeat makes zero;
- crash after broker acceptance: the deterministic QMT remark recovers the order;
- lost response with no broker evidence: the second invocation is blocked and the
  broker call count stays one.

## 4. Cut over the existing scheduled tasks

Keep the existing task names and triggers. Change only their program/action to
the stable runner after the two checks above pass. Use `powershell.exe` as the
program and arguments in this form:

```text
-NoProfile -ExecutionPolicy Bypass -File "C:\hydra-live\bin\Run-HydraLive.ps1" -Command query -Date YYYYMMDD -PythonExe "C:\path\to\python.exe"
```

Use `submit`, `settle-close` or `retry` for the corresponding task. Replace
`YYYYMMDD` with the approved exchange trading date for that cycle. Do not put
secrets in the task arguments. Configure Task Scheduler to reject overlapping
runs; the runner also holds a per-command/per-date exclusive lock.

The mandatory boundary is:

1. T evening `query`: fetch, independently hash and freeze the server batch.
2. T evening `preflight`: while server availability may safely block, compare the
   server ledger with QMT, record/check capacity, and persist the hashed PASS
   receipt for this exact frozen batch.
3. T+1 09:10 `submit`: use the frozen SQLite batch and MiniQMT only. It must run
   even if the server is down; it never performs an integrity call to the server.
4. T+1 15:10 `settle-close`: read terminal QMT results, return them to the
   server, create reconciliation evidence and close the Hydra attempt.
5. T+1 16:00 `retry`: only a server-confirmed `RESIDUAL` can stage the next
   Hydra attempt. Never call the ordinary StrategyPipeline live trigger.

Do not combine `query`, `preflight` and `submit` in one scheduled action. Morning
submit requires the exact frozen batch and its successful preflight receipt. If
either is absent, it fails before opening QMT; once both exist, a later server
outage cannot block submission.

## 5. First cutover evidence

Before a formal Hydra rebalance, save these redacted artifacts:

- reviewed Git full SHA and `release.json` package hash;
- installer JSON and local doctor JSON;
- offline-acceptance JSON;
- Task Scheduler action and service-account identity (no secret values);
- SHA-256 and backup of the live SQLite before the first upgraded run;
- evening query/preflight output and morning submit output;
- proof that a second submit for the same date reports `attempted_now=0`.

Do not deliberately stop the production server during an order window. The
synthetic acceptance already tests the dependency boundary; observe a natural
outage or use an isolated mock environment for a network-failure drill.

## 6. Rollback

Rollback changes only the code pointer; never delete or replace the private
SQLite because it is the idempotency record.

1. Set `HYDRA_LIVE_TRADING_ENABLED=false` in the private env while investigating.
2. Confirm no client task is running.
3. Restore `active-release.txt` and `Run-HydraLive.ps1` from the timestamped
   installer backup, or point `active-release.txt` to the previous reviewed
   40-character release directory.
4. Run `doctor` and inspect the local submission states before re-enabling.
5. If any order is `SUBMITTING_UNKNOWN`, search QMT by its deterministic remark.
   Never “fix” the condition by deleting the row or rerunning blindly.
