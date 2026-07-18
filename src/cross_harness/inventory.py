from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess

from .files import sha256
from .auth import sanitized_environment
from .paths import UserPaths, user_paths


SENSITIVE_NAMES = {
    "auth.json",
    "credentials.json",
    ".credentials.json",
    "secrets.json",
}
SENSITIVE_PARTS = {"logs", "transcripts", "history", "session-env", "projects", "todos"}


def _run_version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "not available"
    first = (result.stdout or result.stderr).strip().splitlines()
    return first[0][:200] if first else f"exit {result.returncode}"


def _auth_status(command: list[str], provider: str, home: Path | None = None) -> str:
    clean_env = sanitized_environment(home or Path.home())
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, env=clean_env, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    text = f"{result.stdout}\n{result.stderr}".lower()
    compact = text.replace(" ", "")
    if provider == "codex" and result.returncode == 0 and "chatgpt" in text and "logged in" in text:
        return "authenticated with ChatGPT"
    if provider == "claude" and result.returncode == 0 and '"loggedin":true' in compact:
        return "authenticated"
    if "not logged" in text or '"loggedin":false' in compact:
        return "not authenticated"
    return f"unknown (exit {result.returncode})"


def inventory(paths: UserPaths | None = None) -> str:
    paths = paths or user_paths()
    candidates = list(setting_files(paths))
    lines = [
        "# Local harness inventory",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## CLI",
        "",
        f"- Codex: `{_run_version(['codex', '--version'])}`",
        f"- Claude Code: `{_run_version(['claude', '--version'])}`",
        f"- Python: `{_run_version(['python3', '--version'])}`",
        f"- Git: `{_run_version(['git', '--version'])}`",
        "",
        "## Authentication (status only)",
        "",
        f"- Codex: {_auth_status(['codex', 'login', 'status'], 'codex', paths.home)}",
        f"- Claude Code: {_auth_status(['claude', 'auth', 'status'], 'claude', paths.home)}",
        f"- Codex credential file exists: {(paths.codex / 'auth.json').exists()} (content not read)",
        "",
        "## Existing settings",
        "",
    ]
    if not candidates:
        lines.append("- None")
    for path in candidates:
        relative = path.relative_to(paths.home)
        lines.append(f"- `~/{relative}`: {path.stat().st_size} bytes, sha256 `{sha256(path)}`")
    lines.extend(["", "Credential and environment files are intentionally excluded.", ""])
    return "\n".join(lines)


def setting_files(paths: UserPaths):
    roots = [
        paths.claude / "CLAUDE.md",
        paths.claude / "settings.json",
        paths.claude / "settings.local.json",
        paths.codex / "AGENTS.md",
        paths.codex / "config.toml",
        paths.codex / "hooks.json",
    ]
    for root in roots:
        if root.is_file() and safe_setting_file(root, paths.home):
            yield root
    for folder in (paths.claude / "agents", paths.claude / "skills", paths.codex / "agents"):
        if folder.is_dir():
            for path in sorted(folder.rglob("*")):
                if path.is_file() and safe_setting_file(path, paths.home):
                    yield path


def safe_setting_file(path: Path, home: Path) -> bool:
    try:
        relative = path.resolve().relative_to(home.resolve())
    except ValueError:
        return False
    lowered = {part.lower() for part in relative.parts}
    if path.name.lower() in SENSITIVE_NAMES or lowered & SENSITIVE_PARTS:
        return False
    if path.name == ".env" or path.suffix.lower() in {".key", ".pem", ".p12"}:
        return False
    return True


def create_backup(destination: Path, paths: UserPaths | None = None) -> list[Path]:
    paths = paths or user_paths()
    copied: list[Path] = []
    for source in setting_files(paths):
        relative = source.relative_to(paths.home)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target)
    manifest = destination / "MANIFEST.txt"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("\n".join(str(path.relative_to(destination)) for path in copied) + "\n", encoding="utf-8")
    return copied
