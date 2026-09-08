"""G24: a diagnostic collector must not require whole-repository SHA equality."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from live_client.windows import hydra_live_market_backup as backup


def fake_git(monkeypatch, *, actual="a" * 40, dirty=""):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=dirty if "status" in command else actual)

    monkeypatch.setattr(backup.subprocess, "run", run)
    return commands


def test_different_repo_head_is_reported_not_blocked(monkeypatch):
    commands = fake_git(monkeypatch)
    result = backup._code_provenance(Path("code"), "b" * 40)
    assert result["actual_commit"] == "a" * 40
    assert result["configured_commit"] == "b" * 40
    assert result["matches_configured_commit"] is False
    assert commands[1][-2:] == ["client/market_push.py", "config.py"]


@pytest.mark.parametrize("configured", ["", "no-longer-a-version-gate", "b" * 40])
def test_configured_version_does_not_authorize_or_block_backup(monkeypatch, configured):
    fake_git(monkeypatch)
    result = backup._code_provenance(Path("code"), configured)
    assert result["collector_sources_clean"] is True
    assert result["event"] == "MARKET_BACKUP_CODE_PROVENANCE"


def test_uncommitted_collector_changes_still_require_review(monkeypatch):
    fake_git(monkeypatch, dirty=" M client/market_push.py\n")
    with pytest.raises(RuntimeError, match="source files have uncommitted changes"):
        backup._code_provenance(Path("code"), "a" * 40)


def test_backup_still_runs_collector_and_emits_provenance_without_a_pin(
    tmp_path, monkeypatch, capsys,
):
    fake_git(monkeypatch)
    client = tmp_path / "client"
    client.mkdir()
    (client / "market_push.py").write_text("# fake collector", encoding="utf-8")
    monkeypatch.setenv("HYDRA_LIVE_CODE_DIR", str(tmp_path))
    monkeypatch.delenv("HYDRA_LIVE_CODE_COMMIT", raising=False)
    monkeypatch.setenv("HYDRA_LIVE_QMT_USERDATA_DIR", "test-userdata")
    monkeypatch.setenv("HYDRA_LIVE_DATA_BACKUP_API_KEY", "TEST_ONLY")
    monkeypatch.setenv("HYDRA_LIVE_WECHAT_WEBHOOK", "")
    monkeypatch.setenv("QMT_PIPELINE_PUSH_MODE", "")
    monkeypatch.setenv("QMT_PIPELINE_WECOM_WEBHOOK", "")
    monkeypatch.setitem(backup.sys.modules, "config", SimpleNamespace())
    monkeypatch.setattr(backup.sys, "path", list(backup.sys.path))
    monkeypatch.setattr(backup.sys, "argv", list(backup.sys.argv))
    calls = []
    monkeypatch.setattr(backup.runpy, "run_path", lambda *args, **kwargs: calls.append((args, kwargs)))
    backup.main()
    result = json.loads(capsys.readouterr().out)
    assert result["actual_commit"] == "a" * 40
    assert result["configured_commit"] is None
    assert backup.sys.argv == [str(client / "market_push.py"), "--live-backup"]
    assert calls == [((str(client / "market_push.py"),), {"run_name": "__main__"})]
    # Provenance alone must not pretend that data have been uploaded.
    assert "status" not in result
