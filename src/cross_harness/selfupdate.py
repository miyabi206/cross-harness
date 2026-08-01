from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import subprocess

from .config import load_config
from .files import sha256
from .installer import COPY_TREES, MANIFEST_PATH, _repo_commit, install
from .paths import UserPaths, user_paths
from . import runner
from .trust import verify_codex_hook_receipt


@dataclass(frozen=True)
class SelfUpdateResult:
    state: str
    repo: Path | None = None
    drift: tuple[str, ...] = ()
    recorded_commit: str | None = None
    current_commit: str | None = None
    warnings: tuple[str, ...] = ()


def _manifest_path(paths: UserPaths) -> Path:
    return paths.home / MANIFEST_PATH


def _load_manifest(paths: UserPaths) -> dict | None:
    path = _manifest_path(paths)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"install manifest is not an object: {path}")
    return value


def _snapshot(root: Path) -> dict[str, tuple[str, str]]:
    """Snapshot COPY_TREES content while ignoring Python bytecode caches."""
    if not root.is_dir():
        return {}
    snapshot: dict[str, tuple[str, str]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.name.endswith(".pyc"):
            continue
        if path.is_symlink():
            snapshot[str(relative)] = ("symlink", os.readlink(path))
        elif path.is_file():
            snapshot[str(relative)] = ("file", sha256(path))
    return snapshot


def _tree_drift(source: Path, installed: Path) -> list[str]:
    source_snapshot = _snapshot(source)
    installed_snapshot = _snapshot(installed)
    drift: list[str] = []
    for relative in sorted(set(source_snapshot) | set(installed_snapshot)):
        if source_snapshot.get(relative) != installed_snapshot.get(relative):
            drift.append(relative)
    return drift


def _installed_hook_drift(manifest: dict) -> list[str]:
    """Return managed Git hooks that were changed after installation."""
    drift: list[str] = []
    records = manifest.get("records", [])
    if not isinstance(records, list):
        return drift
    for record in records:
        if not isinstance(record, dict) or record.get("management") != "git_hook":
            continue
        installed_hash = record.get("installed_hash")
        if not isinstance(installed_hash, str):
            continue
        path_value = record.get("path")
        if not isinstance(path_value, str) or not path_value:
            continue
        path = Path(path_value)
        try:
            changed = path.is_symlink() or not path.is_file() or sha256(path) != installed_hash
        except OSError:
            changed = True
        if changed:
            drift.append(str(path))
    return drift


def detect_drift(repo: Path, installed_root: Path) -> list[str]:
    """Compare the repository and installed runtime by COPY_TREES contents."""
    repo = repo.resolve()
    installed_root = installed_root.resolve()
    drift: list[str] = []
    for name in COPY_TREES:
        drift.extend(f"{name}/{relative}" for relative in _tree_drift(repo / name, installed_root / name))
    return drift


def _git_output(repo: Path, arguments: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    if not isinstance(result.stdout, str):
        return None
    value = result.stdout.strip()
    return value or None


def _branch_name(value: str | None) -> str | None:
    if not value:
        return None
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return value or None


def _default_branch(repo: Path) -> str:
    """Resolve the repository default branch, retaining a safe legacy fallback."""
    origin_head = _branch_name(
        _git_output(repo, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
    )
    if origin_head:
        return origin_head
    configured = _branch_name(_git_output(repo, ["config", "--get", "init.defaultBranch"]))
    return configured or "master"


def _on_default_branch(repo: Path) -> bool:
    current = _branch_name(_git_output(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"]))
    return current is not None and current == _default_branch(repo)


def _active_delegation(paths: UserPaths, repo: Path | None, warnings: list[str]) -> bool:
    """Check the per-repository runner lock, treating unknown status as active."""
    if os.environ.get("CROSS_HARNESS_ACTIVE") == "1":
        return True
    try:
        runtime_root = Path(load_config(home=paths.home)["runtime_root"]).resolve()
    except Exception as exc:
        warnings.append(f"delegation status unavailable: {exc}")
        return True
    try:
        if repo is None:
            raise RuntimeError("repository is unknown")
        lock_path = runner._root_lock_path(runtime_root, repo)
        if lock_path in runner._HELD_ROOT_LOCKS:
            return True
        descriptor = runner._try_lock(lock_path)
        if descriptor is None:
            return True
        runner._release_lock(descriptor)
        return False
    except Exception as exc:
        warnings.append(f"delegation status unavailable: {exc}")
        return True


def _status(home: Path | None = None, repo: Path | None = None) -> SelfUpdateResult:
    paths = user_paths(home)
    warnings: list[str] = []
    manifest = _load_manifest(paths)
    if manifest is None:
        return SelfUpdateResult("unknown", warnings=("install manifest not found",))

    selected_repo = repo
    if selected_repo is None:
        value = manifest.get("repo")
        if isinstance(value, str) and value:
            selected_repo = Path(value)
    if selected_repo is None:
        return SelfUpdateResult("unknown", warnings=("installed repository is unknown",))
    selected_repo = selected_repo.expanduser().resolve()
    if not selected_repo.is_dir():
        return SelfUpdateResult(
            "unknown", repo=selected_repo, warnings=(f"installed repository is unavailable: {selected_repo}",)
        )

    drift = detect_drift(selected_repo, paths.install_root)
    drift.extend(_installed_hook_drift(manifest))
    recorded_commit = manifest.get("repo_commit") if isinstance(manifest.get("repo_commit"), str) else None
    current_commit = _repo_commit(selected_repo)
    return SelfUpdateResult(
        "drift" if drift else "up-to-date",
        repo=selected_repo,
        drift=tuple(drift),
        recorded_commit=recorded_commit,
        current_commit=current_commit,
        warnings=tuple(warnings),
    )


def self_update(
    home: Path | None = None,
    repo: Path | None = None,
    *,
    check: bool = False,
    dry_run: bool = False,
    from_hook: bool = False,
) -> SelfUpdateResult:
    """Check or refresh an installation; all failures are warning-only."""
    paths = user_paths(home)
    try:
        result = _status(paths.home, repo)
    except Exception as exc:
        if from_hook:
            return SelfUpdateResult("skipped")
        return SelfUpdateResult("unknown", warnings=(f"self-update status unavailable: {exc}",))

    if from_hook and (result.repo is None or not _on_default_branch(result.repo)):
        return SelfUpdateResult("skipped", repo=result.repo)

    warnings = list(result.warnings)
    if result.state != "drift":
        return SelfUpdateResult(
            result.state,
            result.repo,
            result.drift,
            result.recorded_commit,
            result.current_commit,
            tuple(warnings),
        )
    if check:
        return SelfUpdateResult(
            "drift",
            result.repo,
            result.drift,
            result.recorded_commit,
            result.current_commit,
            tuple(warnings),
        )
    if _active_delegation(paths, result.repo, warnings):
        warnings.append("delegated execution is in progress; self-update skipped")
        return SelfUpdateResult(
            "skipped",
            result.repo,
            result.drift,
            result.recorded_commit,
            result.current_commit,
            tuple(warnings),
        )

    try:
        install(paths.home, result.repo, dry_run=dry_run)
    except Exception as exc:
        warnings.append(f"self-update install failed: {exc}")
        return SelfUpdateResult(
            "failed" if not dry_run else "dry-run-failed",
            result.repo,
            result.drift,
            result.recorded_commit,
            result.current_commit,
            tuple(warnings),
        )

    if dry_run:
        return SelfUpdateResult(
            "dry-run",
            result.repo,
            result.drift,
            result.recorded_commit,
            result.current_commit,
            tuple(warnings),
        )
    try:
        trusted, detail = verify_codex_hook_receipt(paths.home)
        if not trusted:
            warnings.append(f"Codex hook trust review required: {detail}")
    except Exception as exc:
        warnings.append(f"Codex hook trust status unavailable: {exc}")
    return SelfUpdateResult(
        "updated",
        result.repo,
        result.drift,
        result.recorded_commit,
        _repo_commit(result.repo) if result.repo else result.current_commit,
        tuple(warnings),
    )


def render(result: SelfUpdateResult, *, check: bool = False, dry_run: bool = False) -> str:
    commit = ""
    if result.current_commit or result.recorded_commit:
        current = result.current_commit or "unknown"
        recorded = result.recorded_commit or "unknown"
        commit = f"repo commit: {current} (installed: {recorded})\n"
    if check:
        if result.state == "up-to-date":
            return commit + "self-update: up-to-date\n"
        if result.state == "drift":
            return commit + "self-update: drift detected\n" + "".join(f"- {path}\n" for path in result.drift)
        return commit + f"self-update: {result.state}\n"
    if dry_run:
        if result.state in {"dry-run", "dry-run-failed"}:
            return f"self-update: {'would update' if result.state == 'dry-run' else 'update failed'}\n"
        if result.state == "drift":
            return "self-update: update available\n"
        return f"self-update: {result.state}\n"
    if result.state == "updated":
        return "self-update: updated\n"
    if result.state in {"failed", "skipped"}:
        return f"self-update: {result.state}\n"
    return ""
