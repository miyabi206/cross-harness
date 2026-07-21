from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import sys
import uuid

from .config import load_config
from .errors import ConfigError, HarnessError
from .files import atomic_write
from .paths import user_paths


SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:OPENAI|CODEX|ANTHROPIC)_API_KEY\s*[:=]\s*\S+", re.IGNORECASE),
]


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def create_task_file(
    role: str,
    kind: str,
    cwd: Path,
    goal: str,
    done_when: list[str],
    scope: list[str] | None = None,
    constraints: list[str] | None = None,
    checks: list[str] | None = None,
    references: list[str] | None = None,
    assumptions: list[str] | None = None,
    config_path: Path | None = None,
    home: Path | None = None,
) -> Path:
    paths = user_paths(home)
    config = load_config(config_path, paths.home)
    if role not in config["roles"]:
        raise ConfigError(f"unknown role: {role}")
    role_config = config["roles"][role]
    if kind not in config["delegate_kinds"] or kind not in role_config["delegate_kinds"]:
        raise ConfigError(f"delegation kind {kind!r} is not allowed for {role}")
    if not cwd.is_dir():
        raise HarnessError(f"working directory not found: {cwd}")
    if not goal.strip():
        raise HarnessError("task goal must not be empty")
    if not 1 <= len(done_when) <= 3 or any(not item.strip() for item in done_when):
        raise HarnessError("task requires one to three non-empty completion conditions")

    normalized_checks = [item.strip() for item in checks or [] if item.strip()]
    if kind in {"test", "implementation", "debug"} and not normalized_checks:
        print(
            f"warning: no checks declared for {kind} task; checks must be executable command lines, "
            "not prose, or they cannot match executions and are treated as not_run",
            file=sys.stderr,
        )

    sections: list[tuple[str, list[str]]] = [
        ("Goal", [goal.strip()]),
        ("Done when", [item.strip() for item in done_when]),
        ("Scope", [item.strip() for item in scope or [] if item.strip()]),
        ("Constraints", [item.strip() for item in constraints or [] if item.strip()]),
        ("Checks", normalized_checks),
        ("References", [item.strip() for item in references or [] if item.strip()]),
        ("Assumptions", [item.strip() for item in assumptions or [] if item.strip()]),
    ]
    lines = [f"<!-- cross-harness role={role} kind={kind} cwd={cwd.resolve()} -->"]
    for heading, values in sections:
        if not values:
            continue
        lines.extend(["", f"# {heading}"])
        if heading == "Goal":
            lines.append(values[0])
        else:
            lines.extend(f"- {value}" for value in values)
    content = "\n".join(lines) + "\n"
    if contains_secret(content):
        raise HarnessError("task content appears to contain credential material")
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    path = Path(config["runtime_root"]) / "inbox" / f"{stamp}-{uuid.uuid4().hex[:8]}.md"
    atomic_write(path, content)
    return path
