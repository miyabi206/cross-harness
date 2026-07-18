from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import shutil
import subprocess

from .config import load_config
from .errors import HarnessError
from .files import atomic_write
from .paths import user_paths


def _supervisor_alive(run_dir: Path) -> bool:
    pid_path = run_dir / "supervisor.pid"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def cleanup(config_path: Path | None = None, home: Path | None = None, now: datetime | None = None) -> dict:
    paths = user_paths(home)
    config = load_config(config_path, paths.home)
    root = Path(config["runtime_root"]).resolve()
    runs = (root / "runs").resolve()
    now = now or datetime.now(timezone.utc)
    removed: list[str] = []
    orphaned: list[str] = []
    if not runs.exists():
        runs.mkdir(parents=True, exist_ok=True)
    try:
        runs.relative_to(root)
    except ValueError as exc:
        raise HarnessError("unsafe runtime path; refusing cleanup") from exc
    cutoff = now - timedelta(days=config["retention_days"])
    orphan_cutoff = now - timedelta(hours=1)
    for path in runs.iterdir():
        if not path.is_dir():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            marker = path / "ISOLATED_WORKTREE"
            if marker.is_file():
                worktree = Path(marker.read_text(encoding="utf-8").strip()).resolve()
                try:
                    worktree.relative_to(path.resolve())
                except ValueError:
                    raise HarnessError(f"unsafe isolated worktree marker in {path}")
                if worktree.exists():
                    subprocess.run(
                        ["git", "-C", str(worktree), "worktree", "remove", "--force", str(worktree)],
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
            shutil.rmtree(path)
            removed.append(str(path))
            continue
        complete = (path / "summary.json").exists() or (path / "BLOCKED").exists() or (path / "INTERRUPTED").exists()
        if not complete and modified < orphan_cutoff and not _supervisor_alive(path):
            atomic_write(path / "ORPHANED", f"marked {now.isoformat()}\n")
            orphaned.append(str(path))
    inbox = root / "inbox"
    if inbox.is_dir():
        for path in inbox.iterdir():
            if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                path.unlink()
                removed.append(str(path))
    return {"removed": removed, "orphaned": orphaned}
