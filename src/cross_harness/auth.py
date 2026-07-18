from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import shutil
import subprocess
import tomllib

from .errors import AuthError
from .files import atomic_write, dump_json


API_KEY_NAMES = {"OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"}
ENV_ALLOWLIST = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "TERM",
    "COLORTERM", "SSH_AUTH_SOCK", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "CROSS_HARNESS_ACTIVE", "CROSS_HARNESS_EXECUTOR", "CROSS_HARNESS_WRITE",
    "CROSS_HARNESS_PARENT", "CROSS_HARNESS_RUN_DIR",
}


def detected_api_keys(environment: dict[str, str] | None = None) -> list[str]:
    environment = dict(os.environ) if environment is None else environment
    detected = []
    for name, value in environment.items():
        upper = name.upper()
        if value and (upper in API_KEY_NAMES or upper.endswith("_API_KEY")):
            detected.append(name)
    return sorted(detected)


def sanitized_environment(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in os.environ.items():
        if name in ENV_ALLOWLIST or name.startswith("LC_"):
            result[name] = value
    result["HOME"] = str(home)
    result.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    if extra:
        result.update(extra)
    return result


def resolve_codex(environment: dict[str, str] | None = None) -> Path:
    for candidate in (Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    resolved = shutil.which("codex", path=(environment or os.environ).get("PATH"))
    if not resolved:
        raise AuthError("independent Codex CLI not found on PATH")
    return Path(resolved).resolve()


def verify_codex_chatgpt(
    runtime_root: Path,
    home: Path,
    cache_hours: int,
    environment: dict[str, str] | None = None,
    force: bool = False,
) -> tuple[Path, bool]:
    keys = detected_api_keys(environment)
    if keys:
        raise AuthError("API-key environment detected; refusing delegation: " + ", ".join(keys))
    clean = sanitized_environment(home)
    codex = resolve_codex(clean)
    cache = runtime_root / "auth" / f"{datetime.now().astimezone().date().isoformat()}.json"
    if not force and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            checked = datetime.fromisoformat(data["checked_at"])
            fresh = datetime.now(timezone.utc) - checked <= timedelta(hours=cache_hours)
            if fresh and data.get("method") == "chatgpt" and data.get("codex") == str(codex):
                return codex, True
        except (KeyError, ValueError, json.JSONDecodeError):
            pass
    try:
        result = subprocess.run(
            [str(codex), "login", "status"],
            capture_output=True,
            text=True,
            timeout=20,
            env=clean,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthError(f"could not verify Codex authentication: {exc}") from exc
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or "logged in using chatgpt" not in output.lower():
        raise AuthError("Codex ChatGPT authentication was not proven; run `codex login`")
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "method": "chatgpt",
        "codex": str(codex),
    }
    atomic_write(cache, dump_json(payload))
    return codex, False


def verify_codex_config_ownership(home: Path, repo_root: Path, cwd: Path) -> None:
    """Reject auth/provider overrides without reading any credential material."""
    candidates = [home / ".codex/config.toml"]
    current = repo_root
    candidates.append(current / ".codex/config.toml")
    try:
        relative = cwd.resolve().relative_to(repo_root.resolve())
    except ValueError:
        relative = Path()
    for part in relative.parts:
        current = current / part
        candidates.append(current / ".codex/config.toml")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise AuthError(f"invalid Codex config at {path}: {exc}") from exc
        method = data.get("forced_login_method")
        provider = data.get("model_provider")
        if method not in {None, "chatgpt"}:
            raise AuthError(f"non-ChatGPT forced_login_method in {path}")
        if provider not in {None, "openai"}:
            raise AuthError(f"non-OpenAI model_provider in {path}")
        if data.get("openai_base_url"):
            raise AuthError(f"OpenAI base URL override is not allowed in {path}")
