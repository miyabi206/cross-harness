from __future__ import annotations

from pathlib import Path
import json
import subprocess

from .auth import detected_api_keys, resolve_codex, sanitized_environment, verify_codex_chatgpt
from .config import load_config
from .files import MARKER_START
from .paths import user_paths
from .trust import verify_codex_hook_receipt


def doctor(home: Path | None = None, config_path: Path | None = None) -> dict:
    paths = user_paths(home)
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        config = load_config(config_path, paths.home)
        add("configuration", True, "valid")
    except Exception as exc:
        add("configuration", False, str(exc))
        config = None
    try:
        codex = resolve_codex()
        add("independent Codex CLI", "/.vscode/extensions/" not in str(codex), str(codex))
    except Exception as exc:
        add("independent Codex CLI", False, str(exc))
        codex = None
    keys = detected_api_keys()
    add("API-key environment", not keys, "none" if not keys else ", ".join(keys))
    if config and codex and not keys:
        try:
            actual, cached = verify_codex_chatgpt(Path(config["runtime_root"]), paths.home, config["auth_cache_hours"], force=True)
            add("Codex ChatGPT auth", True, f"{actual} ({'cached' if cached else 'fresh'})")
        except Exception as exc:
            add("Codex ChatGPT auth", False, str(exc))
    try:
        result = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=15, env=sanitized_environment(paths.home), check=False)
        compact = f"{result.stdout}{result.stderr}".replace(" ", "").lower()
        add("Claude auth", result.returncode == 0 and '"loggedin":true' in compact, "authenticated" if result.returncode == 0 and '"loggedin":true' in compact else "not authenticated")
    except Exception as exc:
        add("Claude auth", False, str(exc))
    for label, path in (("Claude charter", paths.claude / "CLAUDE.md"), ("Codex charter", paths.codex / "AGENTS.md")):
        ok = path.exists() and MARKER_START in path.read_text(encoding="utf-8", errors="replace")
        add(label, ok, str(path))
    hook_ok, hook_detail = verify_codex_hook_receipt(paths.home)
    add("Codex hook trust", hook_ok, hook_detail)
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


def render(report: dict) -> str:
    lines = [f"overall: {'PASS' if report['ok'] else 'FAIL'}"]
    for check in report["checks"]:
        lines.append(f"[{'PASS' if check['ok'] else 'FAIL'}] {check['name']}: {check['detail']}")
    return "\n".join(lines) + "\n"
