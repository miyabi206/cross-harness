from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tomllib

from .errors import ConfigError
from .paths import expand_user, source_root, user_paths


REQUIRED_ROLES = {
    "orchestrator",
    "explorer",
    "implementer",
    "implementer_complex",
    "tester",
    "reviewer",
    "debugger",
    "security_reviewer",
}
TOP_KEYS = {
    "version",
    "parent_harness",
    "runtime_root",
    "retention_days",
    "auth_cache_hours",
    "context_threshold_percent",
    "max_parallel",
    "dirty_worktree_policy",
    "delegate_kinds",
    "fallback",
    "roles",
    "projects",
    "mode",
}
ROLE_KEYS = {
    "harness",
    "model",
    "effort",
    "max_parallel",
    "retries",
    "timeout_seconds",
    "write",
    "output_limit_chars",
    "delegate_kinds",
}
PROJECT_KEYS = {"checks", "delegate_kinds", "dirty_worktree_policy", "mode"}
CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DELEGATE_KINDS = {"exploration", "implementation", "test", "debug", "review", "security_review"}
ROLE_DELEGATE_KINDS = DELEGATE_KINDS


def delegation_kind_error(
    kind: str, role_name: str, delegate_kinds: list[str], role_delegate_kinds: list[str]
) -> str:
    """Describe a kind rejected by the global or role-specific policy."""
    allowed = sorted(set(delegate_kinds) & set(role_delegate_kinds))
    suffix = f"; allowed kinds: {', '.join(allowed)}" if allowed else "; no delegation kinds are allowed"
    return f"delegation kind {kind!r} is not allowed for {role_name}{suffix}"


def load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def default_config() -> dict:
    return load_toml(source_root() / "config/default.toml")


def _merge_tables(defaults: dict, overrides: dict) -> dict:
    merged = deepcopy(defaults)
    for key, value in overrides.items():
        default_value = merged.get(key)
        if isinstance(default_value, dict) and isinstance(value, dict):
            merged[key] = _merge_tables(default_value, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def merge_defaults(overrides: dict) -> dict:
    """Apply personal settings over the repository defaults without writing them back."""
    return _merge_tables(default_config(), overrides)


def defaulted_paths(overrides: dict) -> list[str]:
    """Return leaf setting paths supplied by defaults rather than personal settings."""
    paths: list[str] = []

    def collect(defaults: dict, values: dict, prefix: str = "") -> None:
        for key, default_value in defaults.items():
            path = f"{prefix}.{key}" if prefix else key
            if key not in values:
                if isinstance(default_value, dict):
                    collect(default_value, {}, path)
                else:
                    paths.append(path)
            elif isinstance(default_value, dict) and isinstance(values[key], dict):
                collect(default_value, values[key], path)

    collect(default_config(), overrides)
    return paths


def defaulted_config_paths(path: Path | None = None, home: Path | None = None) -> list[str]:
    """Return defaults applied to a personal configuration file, if one exists."""
    home_path = user_paths(home).home
    selected = path or user_paths(home_path).config
    return defaulted_paths(load_toml(selected)) if selected.exists() else []


def _unknown(actual: set[str], allowed: set[str], location: str, errors: list[str]) -> None:
    for key in sorted(actual - allowed):
        errors.append(f"{location}: unknown key {key!r}")


def _integer(config: dict, key: str, low: int, high: int, errors: list[str]) -> None:
    value = config.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        errors.append(f"{key}: expected integer in range {low}..{high}")


def validate(config: dict) -> list[str]:
    errors: list[str] = []
    _unknown(set(config), TOP_KEYS, "root", errors)
    for key in TOP_KEYS - {"projects", "mode"}:
        if key not in config:
            errors.append(f"root: missing key {key!r}")

    if config.get("version") != 1:
        errors.append("version: expected 1")
    if config.get("parent_harness") not in {"claude", "codex"}:
        errors.append("parent_harness: expected 'claude' or 'codex'")
    if not isinstance(config.get("runtime_root"), str) or not config.get("runtime_root"):
        errors.append("runtime_root: expected non-empty string")
    _integer(config, "retention_days", 1, 365, errors)
    _integer(config, "auth_cache_hours", 1, 24, errors)
    _integer(config, "context_threshold_percent", 40, 85, errors)
    _integer(config, "max_parallel", 1, 5, errors)
    if config.get("dirty_worktree_policy") not in {"stop", "isolate", "allow", "allow_delegated"}:
        errors.append("dirty_worktree_policy: expected 'stop', 'isolate', 'allow', or 'allow_delegated'")
    if "mode" in config and config["mode"] not in {"on", "off"}:
        errors.append("mode: expected 'on' or 'off'")
    if not _string_list(config.get("delegate_kinds")):
        errors.append("delegate_kinds: expected a unique string array")
    elif unknown_kinds := set(config["delegate_kinds"]) - DELEGATE_KINDS:
        errors.append("delegate_kinds: unsupported values " + ", ".join(sorted(unknown_kinds)))

    fallback = config.get("fallback")
    if not isinstance(fallback, dict):
        errors.append("fallback: expected table")
    else:
        _unknown(set(fallback), {"codex", "claude"}, "fallback", errors)
        for harness in ("codex", "claude"):
            if not _string_list(fallback.get(harness)) or not fallback.get(harness):
                errors.append(f"fallback.{harness}: expected non-empty unique string array")

    roles = config.get("roles")
    if not isinstance(roles, dict):
        errors.append("roles: expected table")
    else:
        _unknown(set(roles), REQUIRED_ROLES, "roles", errors)
        missing = REQUIRED_ROLES - set(roles)
        for role in sorted(missing):
            errors.append(f"roles: missing role {role!r}")
        for name, role in roles.items():
            location = f"roles.{name}"
            if not isinstance(role, dict):
                errors.append(f"{location}: expected table")
                continue
            _unknown(set(role), ROLE_KEYS, location, errors)
            for key in ROLE_KEYS:
                if key not in role:
                    errors.append(f"{location}: missing key {key!r}")
            harness = role.get("harness")
            if harness not in {"claude", "codex"}:
                errors.append(f"{location}.harness: expected 'claude' or 'codex'")
            if not isinstance(role.get("model"), str) or not role.get("model"):
                errors.append(f"{location}.model: expected non-empty string")
            if not isinstance(role.get("effort"), str) or not role.get("effort"):
                errors.append(f"{location}.effort: expected non-empty string")
            for key, low, high in (
                ("max_parallel", 1, 5),
                ("retries", 0, 2),
                ("timeout_seconds", 30, 14400),
                ("output_limit_chars", 1000, 50000),
            ):
                value = role.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                    errors.append(f"{location}.{key}: expected integer in range {low}..{high}")
            if not isinstance(role.get("write"), bool):
                errors.append(f"{location}.write: expected boolean")
            if not _string_list(role.get("delegate_kinds"), allow_empty=True):
                errors.append(f"{location}.delegate_kinds: expected unique string array")
            elif unknown_kinds := set(role["delegate_kinds"]) - ROLE_DELEGATE_KINDS:
                errors.append(f"{location}.delegate_kinds: unsupported values " + ", ".join(sorted(unknown_kinds)))

    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        errors.append("projects: expected table")
    else:
        for path, project in projects.items():
            location = f"projects.{path}"
            if not isinstance(path, str) or not Path(path).expanduser().is_absolute():
                errors.append(f"{location}: project key must be an absolute path")
            if not isinstance(project, dict):
                errors.append(f"{location}: expected table")
                continue
            _unknown(set(project), PROJECT_KEYS, location, errors)
            if "checks" in project and not _string_list(project["checks"], allow_empty=True):
                errors.append(f"{location}.checks: expected unique string array")
            if "delegate_kinds" in project and not _string_list(project["delegate_kinds"], allow_empty=True):
                errors.append(f"{location}.delegate_kinds: expected unique string array")
            elif "delegate_kinds" in project and (unknown_kinds := set(project["delegate_kinds"]) - DELEGATE_KINDS):
                errors.append(f"{location}.delegate_kinds: unsupported values " + ", ".join(sorted(unknown_kinds)))
            if "dirty_worktree_policy" in project and project["dirty_worktree_policy"] not in {"stop", "isolate", "allow", "allow_delegated"}:
                errors.append(
                    f"{location}.dirty_worktree_policy: expected 'stop', 'isolate', 'allow', or 'allow_delegated'"
                )
            if "mode" in project and project["mode"] not in {"on", "off"}:
                errors.append(f"{location}.mode: expected 'on' or 'off'")
    return errors


def warnings(config: dict) -> list[str]:
    """Return compatibility warnings that do not make a configuration invalid."""
    messages: list[str] = []
    roles = config.get("roles")
    if not isinstance(roles, dict):
        return messages
    for name, role in roles.items():
        if not isinstance(role, dict):
            continue
        harness = role.get("harness")
        model = role.get("model")
        efforts = CLAUDE_EFFORTS if harness == "claude" else CODEX_EFFORTS if harness == "codex" else None
        effort = role.get("effort")
        if harness == "claude" and isinstance(model, str) and "haiku" in model.lower() and isinstance(effort, str) and effort:
            messages.append(f"roles.{name}.effort: has no effect for the haiku model")
        if efforts is not None and isinstance(effort, str) and effort and effort not in efforts:
            messages.append(
                f"roles.{name}.effort: {effort!r} is not a known {harness} effort value; passing through unchanged"
            )
    return messages


def _string_list(value: object, allow_empty: bool = False) -> bool:
    if not isinstance(value, list) or (not value and not allow_empty):
        return False
    return all(isinstance(item, str) and item for item in value) and len(value) == len(set(value))


def load_config(path: Path | None = None, home: Path | None = None) -> dict:
    home_path = user_paths(home).home
    selected = path or user_paths(home_path).config
    config = merge_defaults(load_toml(selected)) if selected.exists() else default_config()
    errors = validate(config)
    if errors:
        raise ConfigError("invalid configuration:\n- " + "\n- ".join(errors))
    resolved = deepcopy(config)
    resolved["runtime_root"] = str(expand_user(resolved["runtime_root"], home_path))
    return resolved


def project_config(config: dict, cwd: Path) -> dict:
    """Return the closest matching project override without allowing model ownership."""
    selected: dict = {}
    selected_length = -1
    cwd_resolved = cwd.resolve()
    for raw, values in config.get("projects", {}).items():
        candidate = Path(raw).expanduser().resolve()
        try:
            cwd_resolved.relative_to(candidate)
        except ValueError:
            continue
        if len(str(candidate)) > selected_length:
            selected = values
            selected_length = len(str(candidate))
    return selected


def effective_mode(config: dict, cwd: Path) -> str:
    """Return the cwd-specific enforcement mode, defaulting to fail-closed on."""
    mode = project_config(config, cwd).get("mode", config.get("mode", "on"))
    return mode if mode in {"on", "off"} else "on"
