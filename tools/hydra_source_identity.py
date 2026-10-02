"""Hash the actual review worktree, including not-yet-committed source files."""

import hashlib
import json
from pathlib import Path


def source_identity(repo: Path) -> dict:
    files = {}
    for relative in (
        "v2.3/server/app",
        "v2.3/server/plugins",
        "v2.3/live_client",
        "tools",
    ):
        for path in sorted((repo / relative).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".ps1", ".yaml"}:
                files[str(path.relative_to(repo))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "source_tree_sha256": digest,
        "file_count": len(files),
        "scope": "server app/plugins, live_client and local audit tools (.py/.ps1/.yaml); includes uncommitted files",
    }
