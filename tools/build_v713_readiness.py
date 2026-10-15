"""Build source-only review artifacts. Market/account files are never included."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {".py", ".ps1", ".sh", ".html", ".css", ".js", ".toml", ".yaml", ".md"}
SCOPES = ["v2.3/server/app", "v2.3/server/scripts", "v2.3/server/plugins", "v2.3/server/tests",
          "v2.3/server/strategies.yaml", "v2.3/server/.env.example",
          "v2.3/server/pyproject.toml", "v2.3/live_client", "reports/hydra_policy_20261002/policy_replay.py",
          "docs/THREE_MODE_TRADING_ARCHITECTURE_SPEC_20260907.md",
          "docs/V713_SHARED_OMS_HANDOFF_20261015.md", "tools/build_v713_readiness.py",
          "docs/V713_SHARED_OMS_WINDOWS_ACCEPTANCE_20261015.md",
          "tools/rehearse_v713_shared_oms.py", "tools/verify_v713_schema_copy.py"]


def build(output: Path, *, allow_dirty: bool = False) -> dict:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT))
    if dirty and not allow_dirty:
        raise ValueError("tracked source is dirty; commit reviewed code or use --allow-dirty for isolated rehearsal")
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z",
                                     "--", *SCOPES], cwd=ROOT).decode().split("\0")
    payload = {}
    for name in sorted(set(names)):
        if not name:
            continue
        path = ROOT/name
        if not path.is_file() or path.is_symlink():
            continue
        relative = Path(name)
        if any(part in {"data", "private", "bundles", "__pycache__", ".venv", "venv", ".pytest_cache"}
               for part in relative.parts):
            continue
        source_json = path.suffix == ".json" and (name.startswith("v2.3/server/app/web/")
                                                   or name.startswith("v2.3/server/tests/"))
        if path.suffix not in SUFFIXES and path.name != ".env.example" and not source_json:
            continue
        payload[name] = path.read_bytes()
    entries = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
               for name, data in payload.items()]
    source_hash = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {"format": 1, "source_commit": commit, "source_dirty": dirty,
                "source_sha256": source_hash, "files": entries, "file_count": len(entries),
                "contains_market_or_account_data": False, "live_activation": False}
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    output.mkdir(parents=True, exist_ok=True)
    archive = output/"v713-readiness-source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in {**payload, "RELEASE_MANIFEST.json": encoded}.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            entry.mode = 0o644
            entry.mtime = 0
            tar.addfile(entry, io.BytesIO(data))
    windows = output/"v713-shared-oms-windows.zip"
    with zipfile.ZipFile(windows, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, data in payload.items():
            if name.startswith("v2.3/live_client/") or name.startswith("docs/V713_SHARED_"):
                zipped.writestr(name, data)
        zipped.writestr("RELEASE_MANIFEST.json", encoded)
    receipt = {**{key: value for key, value in manifest.items() if key != "files"},
               "archives": [{"name": path.name, "bytes": path.stat().st_size,
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                            for path in (archive, windows)]}
    (output/"RELEASE_MANIFEST.json").write_bytes(encoded)
    (output/"build_receipt.json").write_text(json.dumps(receipt, indent=2)+"\n")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(build(args.output_dir, allow_dirty=args.allow_dirty), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
