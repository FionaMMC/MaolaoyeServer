"""Portable checks; these do not replace Windows PowerShell 5.1 execution."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest


WINDOWS = Path(__file__).resolve().parents[1] / "windows"
SCRIPTS = (
    "Install-HydraLiveClient.ps1", "Register-HydraOmsTasks.ps1",
    "Inspect-V713SharedOms.ps1", "Run-V713OmsAcceptance.ps1",
)


def embedded_python(filename, variable):
    source = (WINDOWS / filename).read_text(encoding="utf-8")
    match = re.search(r"\$" + variable + r"\s*=\s*@'\r?\n(.*?)\r?\n'@", source, re.S)
    assert match, variable
    ast.parse(match[1])
    return match[1]


def powershell_tokens(source):
    """Tokenize comments/quotes/here strings and check delimiter balance only.

    This intentionally makes no native-parser claim. Deployment executes the
    actual PowerShell parser before activation and again in the acceptance tool.
    """
    tokens, i = [], 0
    while i < len(source):
        if source[i].isspace():
            i += 1
        elif source.startswith("<#", i):
            end = source.find("#>", i + 2)
            assert end >= 0, "unterminated block comment"
            i = end + 2
        elif source[i] == "#":
            end = source.find("\n", i)
            i = len(source) if end < 0 else end
        elif source[i:i + 2] in ("@'", '@"'):
            quote = source[i + 1]
            match = re.search(r"^" + re.escape(quote + "@") + r"\s*$", source[i + 2:], re.M)
            assert match, "unterminated here string"
            i += 2 + match.end()
        elif source[i] in "\"'":
            quote = source[i]
            i += 1
            while i < len(source):
                if source[i] == "`" and quote == '"':
                    i += 2
                elif source[i:i + 2] == quote * 2:
                    i += 2
                elif source[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            else:
                raise AssertionError("unterminated quoted string")
        else:
            if source[i] in "(){}[]":
                tokens.append(source[i])
            i += 1
    return tokens


@pytest.mark.parametrize("filename", SCRIPTS)
def test_windows_scripts_static_delimiters_and_embedded_python(filename):
    source = (WINDOWS / filename).read_text(encoding="utf-8")
    stack = []
    for token in powershell_tokens(source):
        if token in "({[":
            stack.append(token)
        else:
            assert stack and stack.pop() == {')': '(', '}': '{', ']': '['}[token]
    assert not stack
    for python in re.findall(r"=\s*@'\r?\n(.*?)\r?\n'@", source, re.S):
        ast.parse(python)


def fixture_runtime(tmp_path):
    account = "synthetic-private-account-123456789"
    fingerprint = hashlib.sha256(account.encode()).hexdigest()
    binding = {"account_alias": "synthetic-alias", "execution_domain": "live",
               "account_fingerprint": fingerprint, "client_id": "synthetic-client"}
    state = tmp_path / "private-state" / "state.db"
    state.parent.mkdir()
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE state (value TEXT)")
        db.execute("INSERT INTO state VALUES ('durable-state')")
    journal = state.with_name("oms-agent.db")
    with sqlite3.connect(journal) as db:
        db.executescript("CREATE TABLE metadata (key TEXT, value TEXT);"
                         "CREATE TABLE intents (state TEXT);"
                         "CREATE TABLE outbox (acked_at TEXT);")
        db.execute("INSERT INTO metadata VALUES ('binding', ?)", (json.dumps(binding),))
        db.execute("INSERT INTO intents VALUES ('UNKNOWN')")
        db.execute("INSERT INTO intents VALUES ('SUBMITTING')")
        db.execute("INSERT INTO outbox VALUES (NULL)")
    env = tmp_path / "private.env"
    values = {"HYDRA_LIVE_QMT_ACCOUNT_ID": account,
              "HYDRA_LIVE_EXPECTED_ACCOUNT_SHA256": fingerprint,
              "HYDRA_LIVE_ACCOUNT_ALIAS": binding["account_alias"],
              "HYDRA_LIVE_EXECUTION_DOMAIN": binding["execution_domain"],
              "HYDRA_LIVE_TASK_PREFIX": binding["client_id"],
              "HYDRA_LIVE_API_KEY": "private-api-secret-do-not-export",
              "HYDRA_LIVE_STATE_DB": str(state)}
    env.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    return state, journal, env, fingerprint, binding, account


def run_python(code, *args, extra_env=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", code, *map(str, args)], env=env,
                          capture_output=True, text=True, timeout=15)


def test_upgrade_backup_preserves_wal_db_spool_holds_and_binding(tmp_path):
    state, journal, env, fingerprint, binding, _ = fixture_runtime(tmp_path)
    spool = state.parent / "oms-spool"
    spool.mkdir()
    (spool / "pending.json").write_text('{"pending":true}')
    (spool / "uploaded").mkdir()
    (spool / "uploaded" / "ack.json").write_text('{"acknowledged":true}')
    for name in ("HOLD", "HOLD-v713", "HOLD.v713"):
        (state.parent / name).write_text("preserve evidence")
    backup = tmp_path / "backup"
    backup.mkdir()
    # Keep WAL open so raw file-copy backup would lose the committed newest row.
    writer = sqlite3.connect(state)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO state VALUES ('wal-committed')")
        writer.commit()
        result = run_python(embedded_python(SCRIPTS[0], "backupScript"), state, backup, fingerprint, env)
        assert result.returncode == 0, result.stderr
    finally:
        writer.close()
    with sqlite3.connect(backup / "hydra-live-state-before-upgrade.db") as db:
        assert db.execute("SELECT value FROM state").fetchall() == [("durable-state",), ("wal-committed",)]
    with sqlite3.connect(backup / "oms-agent-before-upgrade.db") as db:
        assert json.loads(db.execute("SELECT value FROM metadata").fetchone()[0]) == binding
        assert db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 2
    for path in (spool / "pending.json", spool / "uploaded" / "ack.json", *(state.parent / n for n in ("HOLD", "HOLD-v713", "HOLD.v713"))):
        assert (backup / "oms-runtime" / path.relative_to(state.parent)).read_bytes() == path.read_bytes()
    manifest = json.loads((backup / "runtime-backup.json").read_text())
    assert manifest["account_fingerprint"] == fingerprint
    assert all(item["quick_check"] == "ok" for item in manifest["databases"].values())
    for relative, digest in manifest["files"].items():
        assert hashlib.sha256((backup / relative).read_bytes()).hexdigest() == digest


def test_upgrade_backup_rejects_changed_client_binding(tmp_path):
    state, journal, env, fingerprint, binding, _ = fixture_runtime(tmp_path)
    binding["client_id"] = "some-other-client"
    with sqlite3.connect(journal) as db:
        db.execute("UPDATE metadata SET value=?", (json.dumps(binding),))
    before = journal.read_bytes()
    backup = tmp_path / "backup"
    backup.mkdir()
    result = run_python(embedded_python(SCRIPTS[0], "backupScript"), state, backup, fingerprint, env)
    assert result.returncode != 0
    assert "Journal account binding mismatch" in result.stderr
    assert journal.read_bytes() == before
    assert not (backup / "runtime-backup.json").exists()


def test_default_inspection_is_read_only_and_exports_no_private_values(tmp_path):
    state, journal, env, fingerprint, _, account = fixture_runtime(tmp_path)
    # Discovery is allowed, importing or connecting the provider is forbidden.
    stub = tmp_path / "xtquant.py"
    stub.write_text("raise AssertionError('provider must not be imported')")
    before = {path: path.read_bytes() for path in (state, journal, env)}
    output, fixtures = tmp_path / "local.json", tmp_path / "quotes.json"
    result = run_python(embedded_python(SCRIPTS[2], "inspection"), env, output, fixtures, "0", "-",
                        extra_env={"PYTHONPATH": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert not result.stdout and not result.stderr
    payload = json.loads(output.read_text())
    assert payload["account_fingerprint"] == fingerprint
    assert payload["configured_fingerprint_matches"]
    assert payload["journal"]["binding_matches"]
    assert payload["journal"]["integrity_ok"]
    assert payload["journal"]["unknown_count"] == 1
    assert payload["journal"]["submitting_count"] == 1
    assert payload["journal"]["pending_outbox_count"] == 1
    assert payload["blockers"] == ["UNRESOLVED_BROKER_INTENTS"]
    assert not payload["qmt_contacted"] and not payload["server_contacted"]
    for private in (account, "private-api-secret-do-not-export", "synthetic-alias", "synthetic-client"):
        assert private not in output.read_text()
    assert not fixtures.exists()
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not list(tmp_path.rglob("__pycache__"))


def test_inspection_missing_journal_does_not_create_state(tmp_path):
    state, journal, env, _, _, _ = fixture_runtime(tmp_path)
    journal.unlink()
    result = run_python(embedded_python(SCRIPTS[2], "inspection"), env, tmp_path / "local.json", tmp_path / "quote.json", "0", "-")
    assert result.returncode == 0, result.stderr
    assert not journal.exists()
    payload = json.loads((tmp_path / "local.json").read_text())
    assert not payload["journal"]["present"]
    assert payload["blockers"] == []


def test_task_staging_and_install_gates_do_not_enable_legacy_on_failure():
    register = (WINDOWS / SCRIPTS[1]).read_text()
    assert register.index("Get-TimeZone") < register.index("Register-ScheduledTask")
    assert "[switch]$StageOnly" in register
    assert "if (-not $StageOnly)" in register
    assert "Enable-ScheduledTask" not in register.split("catch {", 1)[1]
    assert "Unregister-ScheduledTask" not in register
    installer = (WINDOWS / SCRIPTS[0]).read_text()
    assert installer.index("-m live_client.offline_acceptance") < installer.index("-m live_client.oms_offline_acceptance")
    assert "Enable-ScheduledTask" not in installer and "Disable-ScheduledTask" not in installer
    assert installer.index("$accountLock.Lock(0, 1)") < installer.index("$backupScript =")
    assert installer.index("$accountLock.Unlock(0, 1)") > installer.index('status = "INSTALLED"')
    for name in SCRIPTS[2:]:
        source = (WINDOWS / name).read_text()
        assert not re.search(r"\b(?:Register|Enable|Disable|Unregister)-ScheduledTask\b", source)
        assert "-Command doctor" not in source and "-m live_client.oms_agent" not in source
