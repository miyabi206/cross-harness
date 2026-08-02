from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tomllib
import unicodedata
import uuid

from .config import defaulted_config_paths, load_config
from .errors import ConfigError, HarnessError
from .files import append_marker, atomic_write, dump_json, extract_marker, load_json, marker_block, remove_marker, sha256
from .inventory import create_backup
from .paths import UserPaths, source_root, user_paths


MANIFEST_PATH = ".local/state/cross-harness/install-manifest.json"
COPY_TREES = ("bin", "src", "config", "schema", "schemas", "assets")
RUNTIME_TEMP_PREFIX = ".current.tmp-"
_CODEX_CONFIG_READ_LIMIT = 1024 * 1024
_ANSI_ESCAPE = re.compile(
    r"\x1B(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x1B\x07]*(?:\x07|\x1B\\)|[\x00-\x7F])"
)


def _safe_install_text(value: object) -> str:
    """Remove terminal controls before inserting a value into an error message."""
    text = _ANSI_ESCAPE.sub("", str(value or ""))
    return "".join(char for char in text if unicodedata.category(char) != "Cc")


def _runtime_temp_path(install_root: Path) -> Path:
    parent = install_root.parent
    while True:
        candidate = parent / f"{RUNTIME_TEMP_PREFIX}{uuid.uuid4().hex}"
        if not os.path.lexists(candidate):
            return candidate


def _cleanup_runtime_temporary_dirs(install_root: Path) -> None:
    parent = install_root.parent
    if not parent.is_dir():
        return
    for candidate in parent.iterdir():
        if not candidate.name.startswith(RUNTIME_TEMP_PREFIX):
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)


def _manifest_path(paths: UserPaths) -> Path:
    return paths.home / MANIFEST_PATH


def _is_install_root_repo(repo: Path, paths: UserPaths) -> bool:
    install_root = paths.install_root.resolve()
    return repo == install_root or install_root in repo.parents


def _manifest_repo_hint(paths: UserPaths) -> Path | None:
    """Return a usable previously recorded repository without trusting the manifest."""
    try:
        manifest = json.loads(_manifest_path(paths).read_text(encoding="utf-8"))
        value = manifest.get("repo") if isinstance(manifest, dict) else None
        if not isinstance(value, str) or not value:
            return None
        repo = Path(value).expanduser().resolve()
        if _is_install_root_repo(repo, paths):
            return None
        return repo if repo.is_dir() else None
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _reject_install_root_repo(repo: Path, paths: UserPaths) -> None:
    """Prevent an installed wrapper from replacing its runtime with itself."""
    if not _is_install_root_repo(repo, paths):
        return
    message = (
        "cannot use the installation destination as a repository: "
        f"{_safe_install_text(repo)}. "
        "Specify the repository with --repo."
    )
    hint = _manifest_repo_hint(paths)
    if hint is not None:
        safe_hint = _safe_install_text(hint)
        if safe_hint and Path(safe_hint).is_dir():
            message += f" Try: cross-harness install --repo {shlex.quote(safe_hint)}"
    raise HarnessError(message)


def _record(path: Path, paths: UserPaths, backup_root: Path) -> dict:
    record = {"path": str(path), "existed": path.exists() or path.is_symlink()}
    if path.is_file() and not path.is_symlink():
        relative = path.relative_to(paths.home)
        backup = backup_root / relative
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        record["backup"] = str(backup)
        record["before_hash"] = sha256(path)
    elif path.is_symlink():
        record["symlink"] = os.readlink(path)
    elif path.is_dir():
        relative = path.relative_to(paths.home)
        backup = backup_root / relative
        if not backup.exists():
            shutil.copytree(path, backup, symlinks=True)
        record["backup"] = str(backup)
    return record


def _record_external(path: Path, backup_root: Path) -> dict:
    """Record a path outside the user's home, such as a repository hook."""
    record = {"path": str(path), "existed": path.exists() or path.is_symlink()}
    if path.is_file() and not path.is_symlink():
        backup = backup_root / "git-hooks" / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        record["backup"] = str(backup)
        record["before_hash"] = sha256(path)
    elif path.is_symlink():
        record["symlink"] = os.readlink(path)
    elif path.is_dir():
        backup = backup_root / "git-hooks" / path.name
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


def _template(
    text: str,
    executable: Path,
    context_threshold_percent: int,
    max_parallel: int,
    implementer_effort: str,
) -> str:
    return (
        text.replace("{{CROSS_HARNESS_BIN}}", str(executable))
        .replace("{{CONTEXT_THRESHOLD_PERCENT}}", str(context_threshold_percent))
        .replace("{{MAX_PARALLEL}}", str(max_parallel))
        .replace("{{IMPLEMENTER_EFFORT}}", implementer_effort)
    )


def _materialize_templates(
    path: Path,
    executable: Path,
    context_threshold_percent: int,
    max_parallel: int,
    implementer_effort: str,
) -> None:
    candidates = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rendered = _template(text, executable, context_threshold_percent, max_parallel, implementer_effort)
        if rendered != text:
            atomic_write(candidate, rendered, candidate.stat().st_mode & 0o777)


CLAUDE_AGENT_ROLES = {
    "cross-harness-explorer.md": "explorer",
    "cross-harness-implementer.md": "implementer",
    "cross-harness-implementer_complex.md": "implementer_complex",
    "cross-harness-tester.md": "tester",
    "cross-harness-reviewer.md": "reviewer",
    "cross-harness-debugger.md": "debugger",
    "cross-harness-security_reviewer.md": "security_reviewer",
}

CODEX_AGENT_ROLES = {
    "cross-harness-explorer.toml": "explorer",
    "cross-harness-implementer.toml": "implementer",
    "cross-harness-implementer_complex.toml": "implementer_complex",
    "cross-harness-tester.toml": "tester",
    "cross-harness-reviewer.toml": "reviewer",
    "cross-harness-debugger.toml": "debugger",
    "cross-harness-security_reviewer.toml": "security_reviewer",
}


def _frontmatter_scalar(value: str) -> str:
    """Return a single-line YAML scalar while preserving simple existing output."""
    if re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+", value):
        return value
    return json.dumps(value)


def _render_claude_agent_role(text: str, role: dict, role_name: str = "unknown") -> str:
    if not text.startswith("---\n"):
        raise HarnessError("Claude agent template is missing YAML frontmatter")
    closing = text.find("\n---\n", 4)
    if closing < 0:
        raise HarnessError("Claude agent template has unterminated YAML frontmatter")
    frontmatter = text[4:closing]
    for key in ("model", "effort"):
        value = role.get(key)
        if not isinstance(value, str) or not value:
            raise HarnessError(f"Claude agent role {role_name!r} has invalid {key}")
        rendered, count = re.subn(
            rf"(?m)^{key}:.*$",
            f"{key}: {_frontmatter_scalar(value)}",
            frontmatter,
        )
        frontmatter = rendered if count else frontmatter.rstrip("\n") + f"\n{key}: {_frontmatter_scalar(value)}"
    return "---\n" + frontmatter + text[closing:]


def _remove_claude_agent_role_keys(text: str) -> str:
    """Remove managed role keys only from an agent's YAML frontmatter."""
    if not text.startswith("---\n"):
        return text
    closing = text.find("\n---\n", 4)
    if closing < 0:
        return text
    frontmatter = re.sub(r"(?m)^(?:model|effort):.*\n?", "", text[4:closing])
    return "---\n" + frontmatter.rstrip("\n") + text[closing:]


def synchronize_claude_agent_roles(paths: UserPaths, config: dict) -> list[str]:
    """Apply Claude role settings to installed agents and return skipped-role warnings."""
    roles = config.get("roles")
    if not isinstance(roles, dict):
        raise HarnessError("configuration roles are unavailable for Claude agent synchronization")
    warnings: list[str] = []
    skipped: list[str] = []
    for filename, role_name in CLAUDE_AGENT_ROLES.items():
        role = roles.get(role_name)
        if not isinstance(role, dict):
            raise HarnessError(f"configuration role {role_name!r} is unavailable for Claude agent synchronization")
        harness = role.get("harness")
        path = paths.claude / "agents" / filename
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            warnings.append(f"Missing Claude agent definition for role {role_name!r}: {path}")
            continue
        if harness != "claude":
            rendered = _remove_claude_agent_role_keys(text)
            if rendered != text:
                atomic_write(path, rendered, path.stat().st_mode & 0o777)
            skipped.append(role_name)
            continue
        rendered = _render_claude_agent_role(text, role, role_name)
        if rendered != text:
            atomic_write(path, rendered, path.stat().st_mode & 0o777)
    if skipped:
        warnings.append(
            f"Skipped Claude agent synchronization for roles {', '.join(repr(role) for role in skipped)}: "
            "harness is not 'claude'"
        )
    return warnings


def _codex_agent_header_offset(text: str) -> int:
    """Return the start of the first TOML table, or the end of the file."""
    offset = 0
    multiline: str | None = None
    for line in text.splitlines(keepends=True):
        if multiline is None and line.lstrip().startswith("["):
            return offset
        position = 0
        while position < len(line):
            if multiline is not None:
                closing = line.find(multiline, position)
                if closing < 0:
                    break
                if multiline == '"""':
                    backslashes = 0
                    index = closing - 1
                    while index >= 0 and line[index] == "\\":
                        backslashes += 1
                        index -= 1
                    if backslashes % 2:
                        position = closing + len(multiline)
                        continue
                position = closing + len(multiline)
                multiline = None
                continue
            basic = line.find('"""', position)
            literal = line.find("'''", position)
            candidates = [candidate for candidate in (basic, literal) if candidate >= 0]
            if not candidates:
                break
            opening = min(candidates)
            multiline = '"""' if opening == basic else "'''"
            position = opening + 3
        offset += len(line)
    if multiline is not None:
        raise HarnessError("Codex agent template has unterminated multiline string")
    return len(text)


def _codex_agent_role_prefix(text: str) -> tuple[str, str]:
    offset = _codex_agent_header_offset(text)
    return text[:offset], text[offset:]


def _render_codex_agent_role(text: str, role: dict, role_name: str = "unknown") -> str:
    prefix, suffix = _codex_agent_role_prefix(text)
    for key, config_key in (("model", "model"), ("model_reasoning_effort", "effort")):
        value = role.get(config_key)
        if not isinstance(value, str) or not value:
            raise HarnessError(f"Codex agent role {role_name!r} has invalid {config_key}")
        rendered, count = re.subn(
            rf"(?m)^{key}\s*=.*$",
            f"{key} = {json.dumps(value)}",
            prefix,
        )
        if count > 1:
            raise HarnessError(f"Codex agent template has duplicate {key} keys")
        if count:
            prefix = rendered
        else:
            base = prefix.rstrip("\n")
            separator = "\n" if base else ""
            prefix = base + separator + f"{key} = {json.dumps(value)}\n"
    return prefix + suffix


def _remove_codex_agent_role_keys(text: str) -> str:
    """Remove managed role keys from a Codex agent TOML file."""
    prefix, suffix = _codex_agent_role_prefix(text)
    return re.sub(r"(?m)^(?:model|model_reasoning_effort)\s*=.*\n?", "", prefix) + suffix


def synchronize_codex_agent_roles(paths: UserPaths, config: dict) -> list[str]:
    """Apply Codex role settings to installed agents and return skipped-role warnings."""
    roles = config.get("roles")
    if not isinstance(roles, dict):
        raise HarnessError("configuration roles are unavailable for Codex agent synchronization")
    warnings: list[str] = []
    skipped: list[str] = []
    for filename, role_name in CODEX_AGENT_ROLES.items():
        role = roles.get(role_name)
        if not isinstance(role, dict):
            raise HarnessError(f"configuration role {role_name!r} is unavailable for Codex agent synchronization")
        harness = role.get("harness")
        path = paths.codex / "agents" / filename
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            warnings.append(f"Missing Codex agent definition for role {role_name!r}: {path}")
            continue
        if harness != "codex":
            rendered = _remove_codex_agent_role_keys(text)
            if rendered != text:
                atomic_write(path, rendered, path.stat().st_mode & 0o777)
            skipped.append(role_name)
            continue
        rendered = _render_codex_agent_role(text, role, role_name)
        if rendered != text:
            atomic_write(path, rendered, path.stat().st_mode & 0o777)
    if skipped:
        warnings.append(
            f"Skipped Codex agent synchronization for roles {', '.join(repr(role) for role in skipped)}: "
            "harness is not 'codex'"
        )
    return warnings


def _merge_markdown(
    path: Path,
    sources: list[Path],
    paths: UserPaths,
    backup: Path,
    records: list[dict],
    executable: Path,
    context_threshold_percent: int,
    max_parallel: int,
    implementer_effort: str,
) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    content = "\n\n".join(
        _template(
            source.read_text(encoding="utf-8"), executable, context_threshold_percent, max_parallel, implementer_effort
        ).rstrip()
        for source in sources
    )
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


def _codex_config_marker_is_root_scoped(existing: str) -> bool:
    """Return whether the managed block begins before every TOML table."""
    multiline: str | None = None
    for line in existing.splitlines():
        if line == "# >>> cross-harness managed >>>":
            return multiline is None
        if multiline is None and line.lstrip().startswith("["):
            return False
        position = 0
        while position < len(line):
            if multiline is not None:
                closing = line.find(multiline, position)
                if closing < 0:
                    break
                if multiline == '\"\"\"':
                    backslashes = 0
                    index = closing - 1
                    while index >= 0 and line[index] == "\\":
                        backslashes += 1
                        index -= 1
                    if backslashes % 2:
                        position = closing + len(multiline)
                        continue
                position = closing + len(multiline)
                multiline = None
                continue
            basic = line.find('\"\"\"', position)
            literal = line.find("'''", position)
            candidates = [candidate for candidate in (basic, literal) if candidate >= 0]
            if not candidates:
                break
            opening = min(candidates)
            multiline = '\"\"\"' if opening == basic else "'''"
            position = opening + len(multiline)
    return False


def _read_codex_config(path: Path) -> str | None:
    """Read a manageable Codex config without following links or unbounded reads."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            if os.fstat(handle.fileno()).st_size > _CODEX_CONFIG_READ_LIMIT:
                return None
            payload = handle.read(_CODEX_CONFIG_READ_LIMIT)
            if os.fstat(handle.fileno()).st_size > _CODEX_CONFIG_READ_LIMIT:
                return None
        return payload.decode("utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _merge_codex_config(path: Path) -> str | None:
    existing = "" if not path.exists() else _read_codex_config(path)
    if existing is None:
        raise HarnessError(
            f"cannot manage Codex config: {path}; it must be a non-symlink regular UTF-8 file "
            f"no larger than {_CODEX_CONFIG_READ_LIMIT // (1024 * 1024)} MiB."
        )
    try:
        parsed = tomllib.loads(existing) if existing.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise HarnessError(f"invalid existing Codex config: {exc}") from exc
    method = parsed.get("forced_login_method")
    if method not in {None, "chatgpt"}:
        raise HarnessError("existing forced_login_method is not 'chatgpt'; refusing to change authentication")
    if "# >>> cross-harness managed >>>" in existing or "# <<< cross-harness managed <<<" in existing:
        expected = marker_block('forced_login_method = "chatgpt"')
        if extract_marker(existing) == expected and _codex_config_marker_is_root_scoped(existing):
            return None
        raise HarnessError(
            f"cannot manage Codex config: {path}; an existing managed marker is invalid or not in the root scope. "
            "Move or remove the marker block manually before installing."
        )
    if method == "chatgpt":
        return None
    managed = marker_block('forced_login_method = "chatgpt"')
    return managed + ("\n\n" + existing.lstrip() if existing.strip() else "\n")


def _remove_codex_config(path: Path) -> None:
    """Remove Codex-owned marker content without normalizing user bytes."""
    existing = path.read_bytes()
    start_pattern = re.compile(
        rb"(?m)^[ \t]*" + re.escape(b"# >>> cross-harness managed >>>") + rb"[ \t]*(?:\r\n|\n|\r|$)"
    )
    end_pattern = re.compile(
        rb"(?m)^[ \t]*" + re.escape(b"# <<< cross-harness managed <<<") + rb"[ \t]*(?:\r\n|\n|\r|$)"
    )
    start = start_pattern.search(existing)
    if start is None:
        return
    end = end_pattern.search(existing, start.end())
    if end is None:
        return

    # The installer inserts one blank-line separator after the block.  Remove
    # exactly that one separator while retaining all user content verbatim.
    remove_end = end.end()
    if existing[remove_end : remove_end + 2] == b"\r\n":
        remove_end += 2
    elif existing[remove_end : remove_end + 1] in (b"\n", b"\r"):
        remove_end += 1
    # Older releases placed user settings inside this marker block.  Retain
    # those settings, removing only the marker lines and our owned setting.
    managed_setting = re.compile(
        rb'(?m)^[ \t]*forced_login_method[ \t]*=[ \t]*"chatgpt"[ \t]*(?:\r\n|\n|\r|$)'
    )
    middle = managed_setting.sub(b"", existing[start.end() : end.start()])
    atomic_write(path, existing[: start.start()] + middle + existing[remove_end:])


def _installed_drift(
    records: list[dict],
    paths: UserPaths,
    preserve_personal_config: bool = False,
    preserve_codex_config_user_sections: bool = False,
) -> list[str]:
    """Return installed paths that no longer match their manifest records."""
    drift: list[str] = []
    for record in records:
        path = Path(record["path"])
        if record.get("management") == "git_hook":
            continue
        if preserve_personal_config and _is_personal_config_record(record, paths):
            continue
        if record.get("management") == "codex_config" and preserve_codex_config_user_sections:
            expected = marker_block('forced_login_method = "chatgpt"')
            existing = _read_codex_config(path)
            matches_marker = existing is not None and (
                extract_marker(existing) == expected
                and _codex_config_marker_is_root_scoped(existing)
            )
            if not matches_marker:
                drift.append(str(path))
            continue
        if record.get("management") == "codex_config" and "installed_hash" in record:
            try:
                matches_hash = not path.is_symlink() and path.is_file() and sha256(path) == record["installed_hash"]
            except OSError:
                matches_hash = False
            if not matches_hash:
                drift.append(str(path))
            continue
        if "installed_hash" in record:
            try:
                matches_hash = not path.is_symlink() and path.is_file() and sha256(path) == record["installed_hash"]
            except OSError:
                matches_hash = False
            if not matches_hash:
                drift.append(str(path))
        if "installed_symlink" in record:
            try:
                matches_symlink = path.is_symlink() and os.readlink(path) == record["installed_symlink"]
            except OSError:
                matches_symlink = False
            if not matches_symlink:
                drift.append(str(path))
    return drift


def _backup_root(repo: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = repo / ".local/backups" / timestamp
    suffix = 1
    while root.exists():
        root = repo / ".local/backups" / f"{timestamp}-{suffix}"
        suffix += 1
    return root


def _git_hooks_path(repo: Path) -> Path | None:
    """Return the repository's hooks directory without requiring a Git repo."""
    git_marker = repo / ".git"
    if not git_marker.is_dir() and not git_marker.is_file():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "config", "--local", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        value = result.stdout.strip()
        if value:
            path = Path(value).expanduser()
            return (repo / path).resolve() if not path.is_absolute() else path.resolve()
    if git_marker.is_dir():
        return git_marker / "hooks"
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    path = Path(value)
    common_dir = (repo / path).resolve() if not path.is_absolute() else path.resolve()
    return common_dir / "hooks"


def _repo_commit(repo: Path) -> str | None:
    """Return HEAD for human-facing install reporting, when Git can provide it."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    commit = result.stdout.strip()
    return commit or None


def _install(
    home: Path | None = None,
    repo: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    failure_state: dict[str, object] | None = None,
) -> list[str]:
    paths = user_paths(home)
    repo = (repo or source_root()).resolve()
    _reject_install_root_repo(repo, paths)
    if not dry_run:
        _cleanup_runtime_temporary_dirs(paths.install_root)
    git_hooks = _git_hooks_path(repo)
    repo_commit = _repo_commit(repo)
    manifest_path = _manifest_path(paths)
    previous_manifest: dict | None = None
    previous_records: dict[str, dict] = {}
    if manifest_path.exists():
        previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records = previous_manifest.get("records", [])
        if not isinstance(records, list):
            raise HarnessError(f"invalid install manifest records: {manifest_path}")
        previous_records = {str(record["path"]): record for record in records if isinstance(record, dict) and "path" in record}
        if not force:
            drift = _installed_drift(
                records,
                paths,
                preserve_personal_config=True,
                preserve_codex_config_user_sections=True,
            )
            if drift:
                raise HarnessError("installed files changed; refusing to overwrite:\n- " + "\n- ".join(drift))
    backup_root = _backup_root(repo)
    if failure_state is not None:
        failure_state["backup_root"] = backup_root
    actions = [
        f"backup settings to {backup_root}",
        f"{'update' if previous_manifest else 'copy'} runtime to {paths.install_root}",
        f"{'update' if previous_manifest else 'install'} executable {paths.executable}",
        f"{'preserve' if previous_manifest else 'install'} personal config {paths.config}",
        f"{'refresh' if previous_manifest else 'merge'} Claude and Codex user assets",
    ]
    config: dict | None = None
    if paths.config.exists() or paths.config.is_symlink():
        try:
            config = load_config(paths.config, paths.home)
        except ConfigError as exc:
            raise ConfigError(
                f"existing personal configuration is invalid: {paths.config}. "
                f"No files were changed. Fix the configuration and run install again.\n{exc}"
            ) from exc
        defaulted = defaulted_config_paths(paths.config, paths.home)
        if defaulted:
            actions.append(f"defaulted settings: {len(defaulted)}")
            actions.extend(f"default: {path}" for path in defaulted)
    if dry_run:
        return actions

    backup_root.mkdir(parents=True, exist_ok=False)
    create_backup(backup_root, paths)
    if failure_state is not None:
        failure_state["changes_started"] = True
    records: list[dict] = []

    def begin_record(path: Path, management: str) -> dict:
        previous = previous_records.get(str(path))
        if previous is not None:
            record = previous.copy()
        else:
            record = _record(path, paths, backup_root)
        record["management"] = management
        record.pop("installed_hash", None)
        record.pop("installed_symlink", None)
        return record

    runtime_record = begin_record(paths.install_root, "owned")
    paths.install_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = _runtime_temp_path(paths.install_root)
    staging_root.mkdir()
    for name in COPY_TREES:
        source = repo / name
        if source.is_dir():
            shutil.copytree(source, staging_root / name)
    os.chmod(staging_root / "bin/cross-harness", 0o755)
    previous_root = _runtime_temp_path(paths.install_root)
    if paths.install_root.exists() or paths.install_root.is_symlink():
        os.rename(paths.install_root, previous_root)
    os.rename(staging_root, paths.install_root)
    if previous_root.exists():
        shutil.rmtree(previous_root)
    records.append(runtime_record)

    executable_record = begin_record(paths.executable, "owned")
    paths.executable.parent.mkdir(parents=True, exist_ok=True)
    if paths.executable.exists() or paths.executable.is_symlink():
        paths.executable.unlink()
    paths.executable.symlink_to(paths.install_root / "bin/cross-harness")
    _finish_record(executable_record, paths.executable)
    records.append(executable_record)

    if config is None:
        config_record = begin_record(paths.config, "personal_config")
        atomic_write(paths.config, (repo / "config/default.toml").read_text(encoding="utf-8"))
        _finish_record(config_record, paths.config)
        records.append(config_record)
        config = load_config(paths.config, paths.home)
    else:
        config_record = begin_record(paths.config, "personal_config")
        _finish_record(config_record, paths.config)
        records.append(config_record)

    shared = repo / "assets/shared/safety.md"
    if previous_manifest:
        for path in (paths.claude / "CLAUDE.md", paths.codex / "AGENTS.md"):
            if path.is_file():
                atomic_write(path, remove_marker(path.read_text(encoding="utf-8")))
    _merge_markdown(
        paths.claude / "CLAUDE.md", [repo / "assets/claude/CLAUDE.md", shared], paths,
        backup_root, records, paths.executable, config["context_threshold_percent"], config["max_parallel"],
        config["roles"]["implementer"]["effort"],
    )
    _merge_markdown(
        paths.codex / "AGENTS.md", [repo / "assets/codex/AGENTS.md", shared], paths,
        backup_root, records, paths.executable, config["context_threshold_percent"], config["max_parallel"],
        config["roles"]["implementer"]["effort"],
    )

    settings = paths.claude / "settings.json"
    _write_text(settings, _merge_claude_settings(settings, paths.executable), paths, backup_root, records, management="claude_settings")
    codex_hooks = paths.codex / "hooks.json"
    _write_text(codex_hooks, _merge_codex_hooks(codex_hooks, paths.executable), paths, backup_root, records, management="codex_hooks")
    codex_config = paths.codex / "config.toml"
    merged_config = _merge_codex_config(codex_config)
    if merged_config is not None:
        _write_text(codex_config, merged_config, paths, backup_root, records, management="codex_config")
    elif str(codex_config) in previous_records:
        config_record = begin_record(codex_config, "codex_config")
        _finish_record(config_record, codex_config)
        records.append(config_record)

    copies = (
        (repo / "assets/claude/skills/cross-harness-orchestrator", paths.claude / "skills/cross-harness-orchestrator"),
        (repo / "assets/claude/agents/explorer.md", paths.claude / "agents/cross-harness-explorer.md"),
        (repo / "assets/claude/agents/implementer.md", paths.claude / "agents/cross-harness-implementer.md"),
        (repo / "assets/claude/agents/implementer_complex.md", paths.claude / "agents/cross-harness-implementer_complex.md"),
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
        _materialize_templates(
            destination,
            paths.executable,
            config["context_threshold_percent"],
            config["max_parallel"],
            config["roles"]["implementer"]["effort"],
        )
        _finish_record(record, destination)
        records.append(record)
    synchronize_claude_agent_roles(paths, config)
    claude_agent_paths = {
        str(paths.claude / "agents" / filename)
        for filename in CLAUDE_AGENT_ROLES
    }
    for record in records:
        if record["path"] in claude_agent_paths:
            _finish_record(record, Path(record["path"]))
    for source in sorted((repo / "assets/codex/agents").glob("*.toml")):
        destination = paths.codex / "agents" / f"cross-harness-{source.name}"
        record = _record(destination, paths, backup_root)
        record["management"] = "owned"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        _finish_record(record, destination)
        records.append(record)
    synchronize_codex_agent_roles(paths, config)
    codex_agent_paths = {
        str(paths.codex / "agents" / filename)
        for filename in CODEX_AGENT_ROLES
    }
    for record in records:
        if record["path"] in codex_agent_paths:
            _finish_record(record, Path(record["path"]))

    if git_hooks is not None:
        for source in sorted((repo / "assets/git").glob("*")):
            if not source.is_file():
                continue
            destination = git_hooks / source.name
            record = _record_external(destination, backup_root)
            record["management"] = "git_hook"
            record.pop("installed_hash", None)
            record.pop("installed_symlink", None)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            rendered = source.read_text(encoding="utf-8").replace(
                "{{CROSS_HARNESS_BIN}}", shlex.quote(str(paths.executable))
            )
            atomic_write(destination, rendered, 0o755)
            _finish_record(record, destination)
            records.append(record)

    # A reinstall keeps the original pre-install backups.  The operations
    # above necessarily make fresh snapshots while applying the update; use
    # the prior records for paths that were already managed so uninstall still
    # restores the user's pre-install state rather than the previous runtime.
    if previous_records:
        current_paths = {record["path"] for record in records}
        for previous in previous_records.values():
            if previous["path"] not in current_paths:
                _restore_record(previous)
        for index, record in enumerate(records):
            previous = previous_records.get(record["path"])
            if previous is None:
                continue
            refreshed = previous.copy()
            refreshed["management"] = record.get("management", "owned")
            refreshed.pop("installed_hash", None)
            refreshed.pop("installed_symlink", None)
            _finish_record(refreshed, Path(refreshed["path"]))
            records[index] = refreshed

    manifest = {
        "version": 2,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo),
        "repo_commit": repo_commit,
        "backup_root": str(backup_root),
        "records": records,
    }
    atomic_write(manifest_path, dump_json(manifest))
    return actions


def install(
    home: Path | None = None,
    repo: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> list[str]:
    """Install or update managed assets, preserving a usable failure report."""
    was_update = _manifest_path(user_paths(home)).exists()
    failure_state: dict[str, object] = {"changes_started": False}
    try:
        return _install(home, repo, dry_run, force, failure_state)
    except Exception as exc:
        if was_update and failure_state["changes_started"]:
            backup_root = failure_state["backup_root"]
            raise HarnessError(
                "install update failed after managed files may have changed; "
                f"current settings backup: {backup_root}. The previous install manifest was retained. "
                f"Cause: {exc}"
            ) from exc
        raise


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


def _preserve_personal_config(record: dict, paths: UserPaths, backup_root: Path) -> str:
    """Back up, but never restore or remove, user-owned personal configuration."""
    path = Path(record["path"])
    if path.is_file():
        backup = backup_root / path.relative_to(paths.home)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        return f"preserved personal config {path} (backup: {backup})"
    return f"preserved personal config {path} (no readable file to back up)"


def _is_personal_config_record(record: dict, paths: UserPaths) -> bool:
    """Recognize both current records and manifests written before personal config ownership."""
    return record.get("management") == "personal_config" or Path(record["path"]) == paths.config


def _is_codex_config_record(record: dict, paths: UserPaths) -> bool:
    """Recognize current and pre-codex_config manifests for config.toml."""
    return (
        record.get("management") == "codex_config"
        or (record.get("management") == "marker" and Path(record["path"]) == paths.codex / "config.toml")
    )


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
    # Never let a legacy fallback reach _restore_record for config.toml: it
    # recursively deletes directories.  This must also apply to --force and
    # --preserve-user-changes, which bypass the ordinary drift preflight.
    codex_nonfiles = [
        str(Path(record["path"]))
        for record in records
        if isinstance(record, dict)
        and "path" in record
        and _is_codex_config_record(record, paths)
        and (not Path(record["path"]).is_file() or Path(record["path"]).is_symlink())
    ]
    if codex_nonfiles:
        raise HarnessError("installed files changed; refusing to overwrite:\n- " + "\n- ".join(codex_nonfiles))
    if not force and not preserve_user_changes:
        drift = _installed_drift(records, paths)
        if drift:
            raise HarnessError("installed files changed; refusing to overwrite:\n- " + "\n- ".join(drift))

    restored: list[str] = []
    if preserve_user_changes:
        for record in reversed(records):
            path = Path(record["path"])
            management = record.get("management", "owned")
            if _is_personal_config_record(record, paths):
                restored.append(_preserve_personal_config(record, paths, Path(manifest["backup_root"])))
            elif _is_codex_config_record(record, paths):
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
            elif management == "git_hook" and (
                path.is_symlink()
                or not path.is_file()
                or (
                    isinstance(record.get("installed_hash"), str)
                    and sha256(path) != record["installed_hash"]
                )
            ):
                restored.append(f"preserved user changes {path}")
            else:
                restored.append(_restore_record(record))
    else:
        for record in reversed(records):
            if _is_personal_config_record(record, paths):
                restored.append(_preserve_personal_config(record, paths, Path(manifest["backup_root"])))
            elif _is_codex_config_record(record, paths):
                path = Path(record["path"])
                _remove_codex_config(path)
                if not record.get("existed") and not path.read_text(encoding="utf-8").strip():
                    path.unlink()
                restored.append(str(path))
            else:
                restored.append(_restore_record(record))
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
