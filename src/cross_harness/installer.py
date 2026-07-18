from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import shlex
import shutil
import tomllib

from .config import load_config
from .errors import HarnessError
from .files import append_marker, atomic_write, dump_json, load_json, marker_block, remove_marker, sha256
from .inventory import create_backup
from .paths import UserPaths, source_root, user_paths


MANIFEST_PATH = ".local/state/cross-harness/install-manifest.json"
COPY_TREES = ("bin", "src", "config", "schema", "schemas", "assets")


def _manifest_path(paths: UserPaths) -> Path:
    return paths.home / MANIFEST_PATH


def _record(path: Path, paths: UserPaths, backup_root: Path) -> dict:
    record = {"path": str(path), "existed": path.exists() or path.is_symlink()}
    if path.is_file() and not path.is_symlink():
        relative = path.relative_to(paths.home)
        backup = backup_root / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        record["backup"] = str(backup)
        record["before_hash"] = sha256(path)
    elif path.is_symlink():
        record["symlink"] = os.readlink(path)
    elif path.is_dir():
        relative = path.relative_to(paths.home)
        backup = backup_root / relative
        shutil.copytree(path, backup, symlinks=True)
        record["backup"] = str(backup)
    return record


def _finish_record(record: dict, path: Path) -> None:
    if path.is_file() and not path.is_symlink():
        record["installed_hash"] = sha256(path)
    elif path.is_symlink():
        record["installed_symlink"] = os.readlink(path)


def _write_text(
    path: Path,
    text: str,
    paths: UserPaths,
    backup: Path,
    records: list[dict],
    mode: int = 0o600,
    management: str = "owned",
) -> None:
    record = _record(path, paths, backup)
    record["management"] = management
    atomic_write(path, text, mode)
    _finish_record(record, path)
    records.append(record)


def _template(text: str, executable: Path) -> str:
    return text.replace("{{CROSS_HARNESS_BIN}}", str(executable))


def _materialize_templates(path: Path, executable: Path) -> None:
    candidates = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rendered = _template(text, executable)
        if rendered != text:
            atomic_write(candidate, rendered, candidate.stat().st_mode & 0o777)


CLAUDE_AGENT_ROLES = {
    "cross-harness-explorer.md": "explorer",
    "cross-harness-implementer.md": "implementer",
    "cross-harness-tester.md": "tester",
    "cross-harness-reviewer.md": "reviewer",
    "cross-harness-debugger.md": "debugger",
    "cross-harness-security_reviewer.md": "security_reviewer",
}


def _frontmatter_scalar(value: str) -> str:
    """Return a single-line YAML scalar while preserving simple existing output."""
    if re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+", value):
        return value
    return json.dumps(value)


def _render_claude_agent_role(text: str, role: dict) -> str:
    if not text.startswith("---\n"):
        raise HarnessError("Claude agent template is missing YAML frontmatter")
    closing = text.find("\n---\n", 4)
    if closing < 0:
        raise HarnessError("Claude agent template has unterminated YAML frontmatter")
    frontmatter = text[4:closing]
    for key in ("model", "effort"):
        value = role.get(key)
        if not isinstance(value, str) or not value:
            raise HarnessError(f"Claude agent role has invalid {key}")
        rendered, count = re.subn(
            rf"(?m)^{key}:.*$",
            f"{key}: {_frontmatter_scalar(value)}",
            frontmatter,
        )
        if count != 1:
            raise HarnessError(f"Claude agent template is missing {key} frontmatter")
        frontmatter = rendered
    return "---\n" + frontmatter + text[closing:]


def synchronize_claude_agent_roles(paths: UserPaths, config: dict) -> list[str]:
    """Apply Claude role settings to installed agents and return skipped-role warnings."""
    roles = config.get("roles")
    if not isinstance(roles, dict):
        raise HarnessError("configuration roles are unavailable for Claude agent synchronization")
    warnings: list[str] = []
    for filename, role_name in CLAUDE_AGENT_ROLES.items():
        role = roles.get(role_name)
        if not isinstance(role, dict):
            raise HarnessError(f"configuration role {role_name!r} is unavailable for Claude agent synchronization")
        harness = role.get("harness")
        if harness != "claude":
            warnings.append(
                f"Skipped Claude agent synchronization for role {role_name!r}: "
                f"harness is {harness!r}, not 'claude'"
            )
            continue
        path = paths.claude / "agents" / filename
        text = path.read_text(encoding="utf-8")
        rendered = _render_claude_agent_role(text, role)
        if rendered != text:
            atomic_write(path, rendered, path.stat().st_mode & 0o777)
    return warnings


def _merge_markdown(
    path: Path,
    sources: list[Path],
    paths: UserPaths,
    backup: Path,
    records: list[dict],
    executable: Path,
) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    content = "\n\n".join(_template(source.read_text(encoding="utf-8"), executable).rstrip() for source in sources)
    _write_text(path, append_marker(existing, content), paths, backup, records, management="marker")


def _merge_claude_settings(path: Path, executable: Path) -> str:
    data = load_json(path, {})
    if not isinstance(data, dict):
        raise HarnessError(f"expected JSON object: {path}")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise HarnessError(f"hooks must be an object: {path}")
    command = shlex.quote(str(executable))
    additions = {
        "SessionStart": [{"matcher": "startup|resume|compact", "hooks": [{"type": "command", "command": f"{command} hook claude-session-start", "timeout": 20}]}],
        "Stop": [{"hooks": [{"type": "command", "command": f"{command} hook claude-stop", "timeout": 20}]}],
        "PreToolUse": [{"matcher": "Edit|Write|Bash", "hooks": [{"type": "command", "command": f"{command} hook claude-pre-tool-use", "timeout": 20}]}],
    }
    for event, entries in additions.items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            raise HarnessError(f"hooks.{event} must be an array: {path}")
        for entry in entries:
            if entry not in existing:
                existing.append(entry)
    permissions = data.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise HarnessError(f"permissions must be an object: {path}")
    allowed = permissions.setdefault("allow", [])
    if not isinstance(allowed, list):
        raise HarnessError(f"permissions.allow must be an array: {path}")
    for command in (str(executable), "cross-harness"):
        for action in ("task", "delegate", "retry"):
            rule = f"Bash({command} {action}:*)"
            if rule not in allowed:
                allowed.append(rule)
    return dump_json(data)


def _merge_codex_hooks(path: Path, executable: Path) -> str:
    data = load_json(path, {})
    if not isinstance(data, dict):
        raise HarnessError(f"expected JSON object: {path}")
    hooks = data.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    entry = {
        "matcher": "^Bash$",
        "hooks": [{
            "type": "command",
            "command": f"{shlex.quote(str(executable))} hook codex-pre-tool-use",
            "timeout": 20,
            "statusMessage": "cross-harness: checking recursion guard",
        }],
    }
    if entry not in pre:
        pre.append(entry)
    return dump_json(data)


def _merge_codex_config(path: Path) -> str | None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        parsed = tomllib.loads(existing) if existing.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise HarnessError(f"invalid existing Codex config: {exc}") from exc
    method = parsed.get("forced_login_method")
    if method not in {None, "chatgpt"}:
        raise HarnessError("existing forced_login_method is not 'chatgpt'; refusing to change authentication")
    if method == "chatgpt":
        return None
    managed = marker_block('forced_login_method = "chatgpt"')
    return managed + ("\n\n" + existing.lstrip() if existing.strip() else "\n")


def _remove_codex_config(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    result: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == "# >>> cross-harness managed >>>":
            inside = True
            continue
        if stripped == "# <<< cross-harness managed <<<":
            inside = False
            continue
        if inside and re.fullmatch(r'forced_login_method\s*=\s*"chatgpt"', stripped):
            continue
        result.append(line)
    while result and not result[0].strip():
        result.pop(0)
    text = "\n".join(result).rstrip()
    atomic_write(path, text + "\n" if text else "")


def install(home: Path | None = None, repo: Path | None = None, dry_run: bool = False) -> list[str]:
    paths = user_paths(home)
    repo = (repo or source_root()).resolve()
    manifest_path = _manifest_path(paths)
    if manifest_path.exists():
        raise HarnessError("cross-harness is already installed; uninstall it before reinstalling")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = repo / ".local/backups" / timestamp
    actions = [
        f"backup settings to {backup_root}",
        f"copy runtime to {paths.install_root}",
        f"install executable {paths.executable}",
        f"install personal config {paths.config}",
        "merge Claude and Codex user assets",
    ]
    if dry_run:
        return actions

    backup_root.mkdir(parents=True, exist_ok=False)
    create_backup(backup_root, paths)
    records: list[dict] = []

    runtime_record = _record(paths.install_root, paths, backup_root)
    runtime_record["management"] = "owned"
    paths.install_root.parent.mkdir(parents=True, exist_ok=True)
    if paths.install_root.exists():
        shutil.rmtree(paths.install_root)
    paths.install_root.mkdir()
    for name in COPY_TREES:
        source = repo / name
        if source.is_dir():
            shutil.copytree(source, paths.install_root / name)
    os.chmod(paths.install_root / "bin/cross-harness", 0o755)
    records.append(runtime_record)

    executable_record = _record(paths.executable, paths, backup_root)
    executable_record["management"] = "owned"
    paths.executable.parent.mkdir(parents=True, exist_ok=True)
    if paths.executable.exists() or paths.executable.is_symlink():
        paths.executable.unlink()
    paths.executable.symlink_to(paths.install_root / "bin/cross-harness")
    _finish_record(executable_record, paths.executable)
    records.append(executable_record)

    if not paths.config.exists():
        _write_text(paths.config, (repo / "config/default.toml").read_text(encoding="utf-8"), paths, backup_root, records, management="owned")
    config = load_config(paths.config, paths.home)

    shared = repo / "assets/shared/safety.md"
    _merge_markdown(paths.claude / "CLAUDE.md", [repo / "assets/claude/CLAUDE.md", shared], paths, backup_root, records, paths.executable)
    _merge_markdown(paths.codex / "AGENTS.md", [repo / "assets/codex/AGENTS.md", shared], paths, backup_root, records, paths.executable)

    settings = paths.claude / "settings.json"
    _write_text(settings, _merge_claude_settings(settings, paths.executable), paths, backup_root, records, management="claude_settings")
    codex_hooks = paths.codex / "hooks.json"
    _write_text(codex_hooks, _merge_codex_hooks(codex_hooks, paths.executable), paths, backup_root, records, management="codex_hooks")
    codex_config = paths.codex / "config.toml"
    merged_config = _merge_codex_config(codex_config)
    if merged_config is not None:
        _write_text(codex_config, merged_config, paths, backup_root, records, management="codex_config")

    copies = (
        (repo / "assets/claude/skills/cross-harness-orchestrator", paths.claude / "skills/cross-harness-orchestrator"),
        (repo / "assets/claude/agents/explorer.md", paths.claude / "agents/cross-harness-explorer.md"),
        (repo / "assets/claude/agents/implementer.md", paths.claude / "agents/cross-harness-implementer.md"),
        (repo / "assets/claude/agents/tester.md", paths.claude / "agents/cross-harness-tester.md"),
        (repo / "assets/claude/agents/reviewer.md", paths.claude / "agents/cross-harness-reviewer.md"),
        (repo / "assets/claude/agents/debugger.md", paths.claude / "agents/cross-harness-debugger.md"),
        (repo / "assets/claude/agents/security_reviewer.md", paths.claude / "agents/cross-harness-security_reviewer.md"),
    )
    for source, destination in copies:
        record = _record(destination, paths, backup_root)
        record["management"] = "owned"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
            _finish_record(record, destination)
        _materialize_templates(destination, paths.executable)
        _finish_record(record, destination)
        records.append(record)
    synchronize_claude_agent_roles(paths, config)
    for source in sorted((repo / "assets/codex/agents").glob("*.toml")):
        destination = paths.codex / "agents" / f"cross-harness-{source.name}"
        record = _record(destination, paths, backup_root)
        record["management"] = "owned"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        _finish_record(record, destination)
        records.append(record)

    manifest = {
        "version": 1,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo),
        "backup_root": str(backup_root),
        "records": records,
    }
    atomic_write(manifest_path, dump_json(manifest))
    return actions


def _original_json(record: dict) -> dict:
    if not record.get("existed") or "backup" not in record:
        return {}
    value = load_json(Path(record["backup"]), {})
    return value if isinstance(value, dict) else {}


def _managed_hook_entry(entry: object, executable: Path) -> bool:
    if not isinstance(entry, dict):
        return False
    handlers = entry.get("hooks", [])
    if not isinstance(handlers, list):
        return False
    prefix = f"{shlex.quote(str(executable))} hook "
    return any(isinstance(handler, dict) and str(handler.get("command", "")).startswith(prefix) for handler in handlers)


def _remove_claude_settings(path: Path, executable: Path, original: dict) -> None:
    data = load_json(path, {})
    if not isinstance(data, dict):
        raise HarnessError(f"expected JSON object during uninstall: {path}")
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            entries = hooks[event]
            if isinstance(entries, list):
                hooks[event] = [entry for entry in entries if not _managed_hook_entry(entry, executable)]
                original_hooks = original.get("hooks", {}) if isinstance(original.get("hooks"), dict) else {}
                if not hooks[event] and event not in original_hooks:
                    hooks.pop(event)
        if not hooks and "hooks" not in original:
            data.pop("hooks", None)
    permissions = data.get("permissions")
    if isinstance(permissions, dict) and isinstance(permissions.get("allow"), list):
        managed = {
            f"Bash({command} {action}:*)"
            for command in (str(executable), "cross-harness")
            for action in ("task", "delegate", "retry")
        }
        permissions["allow"] = [rule for rule in permissions["allow"] if rule not in managed]
        original_permissions = original.get("permissions", {}) if isinstance(original.get("permissions"), dict) else {}
        if not permissions["allow"] and "allow" not in original_permissions:
            permissions.pop("allow")
        if not permissions and "permissions" not in original:
            data.pop("permissions", None)
    atomic_write(path, dump_json(data))


def _remove_codex_hooks(path: Path, executable: Path, original: dict) -> None:
    data = load_json(path, {})
    if not isinstance(data, dict):
        raise HarnessError(f"expected JSON object during uninstall: {path}")
    hooks = data.get("hooks")
    if isinstance(hooks, dict) and isinstance(hooks.get("PreToolUse"), list):
        hooks["PreToolUse"] = [entry for entry in hooks["PreToolUse"] if not _managed_hook_entry(entry, executable)]
        original_hooks = original.get("hooks", {}) if isinstance(original.get("hooks"), dict) else {}
        if not hooks["PreToolUse"] and "PreToolUse" not in original_hooks:
            hooks.pop("PreToolUse")
        if not hooks and "hooks" not in original:
            data.pop("hooks", None)
    atomic_write(path, dump_json(data))


def _restore_record(record: dict) -> str:
    path = Path(record["path"])
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)
    if record.get("existed"):
        path.parent.mkdir(parents=True, exist_ok=True)
        if "backup" in record:
            backup = Path(record["backup"])
            if backup.is_dir():
                shutil.copytree(backup, path, symlinks=True)
            else:
                shutil.copy2(backup, path)
        elif "symlink" in record:
            path.symlink_to(record["symlink"])
    return str(path)


def uninstall(
    home: Path | None = None,
    force: bool = False,
    preserve_user_changes: bool = False,
    purge_runtime: bool = False,
) -> list[str]:
    paths = user_paths(home)
    manifest_path = _manifest_path(paths)
    if not manifest_path.exists():
        raise HarnessError("cross-harness install manifest not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("records", [])
    runtime_root: Path | None = None
    if purge_runtime:
        config = load_config(home=paths.home)
        runtime_root = Path(config["runtime_root"]).resolve()
        safe_root = (paths.home / ".local/state/cross-harness").resolve()
        if runtime_root != safe_root:
            raise HarnessError(f"refusing to purge non-default runtime root: {runtime_root}")
    if not force and not preserve_user_changes:
        drift = []
        for record in records:
            path = Path(record["path"])
            if "installed_hash" in record and path.is_file() and sha256(path) != record["installed_hash"]:
                drift.append(str(path))
            if "installed_symlink" in record and path.is_symlink() and os.readlink(path) != record["installed_symlink"]:
                drift.append(str(path))
        if drift:
            raise HarnessError("installed files changed; refusing to overwrite:\n- " + "\n- ".join(drift))

    restored: list[str] = []
    if preserve_user_changes:
        for record in reversed(records):
            path = Path(record["path"])
            management = record.get("management", "owned")
            if (
                management == "codex_config"
                or (management == "marker" and path == paths.codex / "config.toml")
            ) and path.is_file():
                _remove_codex_config(path)
                if not record.get("existed") and not path.read_text(encoding="utf-8").strip():
                    path.unlink()
                restored.append(str(path))
            elif management == "marker" and path.is_file():
                remaining = remove_marker(path.read_text(encoding="utf-8"))
                if not record.get("existed") and not remaining.strip():
                    path.unlink()
                else:
                    atomic_write(path, remaining)
                restored.append(str(path))
            elif management == "claude_settings" and path.is_file():
                _remove_claude_settings(path, paths.executable, _original_json(record))
                if not record.get("existed") and load_json(path, {}) == {}:
                    path.unlink()
                restored.append(str(path))
            elif management == "codex_hooks" and path.is_file():
                _remove_codex_hooks(path, paths.executable, _original_json(record))
                if not record.get("existed") and load_json(path, {}) == {}:
                    path.unlink()
                restored.append(str(path))
            else:
                restored.append(_restore_record(record))
    else:
        restored.extend(_restore_record(record) for record in reversed(records))
    manifest_path.unlink()
    if purge_runtime:
        assert runtime_root is not None
        if runtime_root.exists():
            runtime_backup = Path(manifest["backup_root"]) / "runtime-state-at-uninstall"
            if runtime_backup.exists():
                raise HarnessError(f"runtime backup already exists: {runtime_backup}")
            shutil.copytree(runtime_root, runtime_backup, symlinks=True)
            shutil.rmtree(runtime_root)
    return restored
