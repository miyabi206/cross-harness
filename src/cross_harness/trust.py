from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json

from .errors import HarnessError
from .files import atomic_write, dump_json
from .paths import UserPaths, user_paths


def _managed_definition(paths: UserPaths) -> dict:
    hooks_path = paths.codex / "hooks.json"
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"cannot read Codex hooks: {exc}") from exc
    expected = f"{paths.executable} hook codex-pre-tool-use"
    entries = data.get("hooks", {}).get("PreToolUse", []) if isinstance(data, dict) else []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("matcher") != "^Bash$":
            continue
        handlers = entry.get("hooks", [])
        if any(isinstance(handler, dict) and handler.get("command") == expected for handler in handlers):
            return entry
    raise HarnessError("installed cross-harness Codex hook definition not found")


def _digest(definition: dict) -> str:
    encoded = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _receipt_path(paths: UserPaths) -> Path:
    return paths.home / ".local/state/cross-harness/trust/codex-hook.json"


def confirm_codex_hook(home: Path | None = None, *, confirmed_after_review: bool = False) -> Path:
    if not confirmed_after_review:
        raise HarnessError("open /hooks, review the exact command, then pass --confirmed-after-review")
    paths = user_paths(home)
    definition = _managed_definition(paths)
    receipt = {
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "hooks_path": str(paths.codex / "hooks.json"),
        "definition_sha256": _digest(definition),
        "command": f"{paths.executable} hook codex-pre-tool-use",
    }
    path = _receipt_path(paths)
    atomic_write(path, dump_json(receipt), 0o600)
    return path


def verify_codex_hook_receipt(home: Path | None = None) -> tuple[bool, str]:
    paths = user_paths(home)
    try:
        definition = _managed_definition(paths)
    except HarnessError as exc:
        return False, str(exc)
    receipt_path = _receipt_path(paths)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, "manual verification required: open /hooks, trust the hook, then record confirmation"
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"invalid hook review receipt: {exc}"
    digest = _digest(definition)
    if not isinstance(receipt, dict) or receipt.get("definition_sha256") != digest:
        return False, "hook definition changed after manual review; review it again in /hooks"
    return True, f"manual review receipt matches current definition ({digest[:12]})"
