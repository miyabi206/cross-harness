from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json

from .config import load_config
from .errors import HarnessError
from .files import atomic_write
from .paths import source_root, user_paths


TASK_LABEL = "cross-harness: watch delegations"
_STATE_FILE = "project-state.json"


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _project_directory(cwd: Path) -> Path:
    if not cwd.is_dir():
        raise HarnessError(f"project directory does not exist: {cwd}")
    return cwd.resolve()


def _tasks_path(cwd: Path) -> Path:
    return cwd / ".vscode/tasks.json"


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"invalid JSON in {path}: {exc}") from exc


def _jsonc_guidance(path: Path, error: json.JSONDecodeError, executable: Path, operation: str) -> HarnessError:
    task = _task_template(executable)
    if operation == "setup":
        manual_action = 'Add this task manually to the "tasks" array:'
    else:
        manual_action = f"Remove the task with label {TASK_LABEL!r} manually:"
    return HarnessError(
        f"invalid JSON in {path}: {error}\n"
        "VS Code tasks.json supports comments (JSONC), but cross-harness project "
        f"{operation} cannot rewrite a commented file without losing comments. The file was not modified.\n"
        f"{manual_action}\n{_json_text(task).rstrip()}"
    )


def _tasks_document(path: Path, executable: Path, operation: str) -> dict:
    try:
        document = _read_json(path)
    except HarnessError as exc:
        cause = exc.__cause__
        if isinstance(cause, json.JSONDecodeError):
            raise _jsonc_guidance(path, cause, executable, operation) from exc
        raise
    if not isinstance(document, dict):
        raise HarnessError(f"invalid VS Code tasks document in {path}: expected a JSON object")
    tasks = document.get("tasks", [])
    if not isinstance(tasks, list):
        raise HarnessError(f"invalid VS Code tasks document in {path}: 'tasks' must be an array")
    return document


def _task_template(executable: Path) -> dict:
    template_path = source_root() / "assets/shared/vscode-tasks.json"
    try:
        rendered_text = template_path.read_text(encoding="utf-8").replace("{{CROSS_HARNESS_BIN}}", str(executable))
        template = json.loads(rendered_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"invalid project task template: {template_path}: {exc}") from exc
    if not isinstance(template, dict) or not isinstance(template.get("tasks"), list) or len(template["tasks"]) != 1:
        raise HarnessError(f"invalid project task template: {template_path}")
    task = template["tasks"][0]
    if not isinstance(task, dict):
        raise HarnessError(f"invalid project task template: {template_path}")
    return task


def _state_path(runtime_root: Path) -> Path:
    return runtime_root / _STATE_FILE


def _created_tasks(runtime_root: Path) -> set[str]:
    path = _state_path(runtime_root)
    if not path.exists():
        return set()
    state = _read_json(path)
    entries = state.get("created_tasks") if isinstance(state, dict) else None
    if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
        raise HarnessError(f"invalid project state in {path}")
    return set(entries)


def _write_created_tasks(runtime_root: Path, entries: set[str]) -> None:
    path = _state_path(runtime_root)
    if entries:
        atomic_write(path, _json_text({"created_tasks": sorted(entries)}), 0o600)
    else:
        path.unlink(missing_ok=True)


def _backup(path: Path, content: str, runtime_root: Path) -> Path:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = runtime_root / "project-backups" / f"{stamp}-{digest}-tasks.json"
    atomic_write(destination, content, 0o600)
    return destination


def _replace_task(document: dict, task: dict) -> bool:
    tasks = document.get("tasks", [])
    replacement = [task if isinstance(item, dict) and item.get("label") == TASK_LABEL else item for item in tasks]
    if not any(isinstance(item, dict) and item.get("label") == TASK_LABEL for item in tasks):
        replacement.append(task)
    if replacement == tasks:
        return False
    document["tasks"] = replacement
    return True


def setup(cwd: Path, config_path: Path | None = None, home: Path | None = None, dry_run: bool = False) -> list[str]:
    """Install or update the folder-open watch task for one VS Code project."""
    root = _project_directory(cwd)
    paths = user_paths(home)
    config = load_config(config_path, paths.home)
    runtime_root = Path(config["runtime_root"])
    path = _tasks_path(root)
    task = _task_template(paths.executable)

    if path.exists():
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise HarnessError(f"invalid JSON in {path}: {exc}") from exc
        document = _tasks_document(path, paths.executable, "setup")
        changed = _replace_task(document, task)
        if not changed:
            return []
        action = f"{'would update' if dry_run else 'updated'} {path}"
        if dry_run:
            return [action]
        _backup(path, original, runtime_root)
        atomic_write(path, _json_text(document), path.stat().st_mode & 0o777)
        return [action]

    document = {"version": "2.0.0", "tasks": [task]}
    action = f"{'would create' if dry_run else 'created'} {path}"
    if dry_run:
        return [action]
    created = _created_tasks(runtime_root)
    created.add(str(path))
    atomic_write(path, _json_text(document), 0o644)
    _write_created_tasks(runtime_root, created)
    return [action]


def remove(cwd: Path, config_path: Path | None = None, home: Path | None = None, dry_run: bool = False) -> list[str]:
    """Remove the folder-open watch task without disturbing other tasks."""
    root = _project_directory(cwd)
    paths = user_paths(home)
    config = load_config(config_path, paths.home)
    runtime_root = Path(config["runtime_root"])
    path = _tasks_path(root)
    if not path.exists():
        return []

    document = _tasks_document(path, paths.executable, "remove")
    tasks = document.get("tasks", [])
    remaining = [item for item in tasks if not (isinstance(item, dict) and item.get("label") == TASK_LABEL)]
    if remaining == tasks:
        return []

    created = _created_tasks(runtime_root)
    created_here = str(path) in created
    delete_file = not remaining and created_here
    actions = [f"{'would remove' if dry_run else 'removed'} {path}"]
    vscode = path.parent
    delete_directory = delete_file and len(list(vscode.iterdir())) == 1
    if delete_directory:
        actions.append(f"{'would remove' if dry_run else 'removed'} {vscode}")
    if dry_run:
        return actions

    created.discard(str(path))
    if delete_file:
        path.unlink()
        if delete_directory:
            vscode.rmdir()
    else:
        document["tasks"] = remaining
        atomic_write(path, _json_text(document), path.stat().st_mode & 0o777)
    _write_created_tasks(runtime_root, created)
    return actions
