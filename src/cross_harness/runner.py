from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import uuid

from .auth import sanitized_environment, verify_codex_chatgpt, verify_codex_config_ownership
from .config import load_config, project_config
from .errors import AuthError, ConfigError, DirtyWorktreeError, HarnessError
from .files import atomic_write, dump_json, sha256
from .paths import source_root, user_paths
from .summarize import failure_signature, load_final, parse_events, render_summary
from .taskfile import contains_secret


RATE_LIMIT = re.compile(r"rate.?limit|usage.?limit|quota|too many requests", re.IGNORECASE)
AUTH_FAILURE = re.compile(r"unauthorized|authentication|not logged in|login required", re.IGNORECASE)


def _git(cwd: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def _git_root(cwd: Path) -> Path:
    result = _git(cwd, ["rev-parse", "--show-toplevel"])
    if result.returncode:
        raise HarnessError("delegation requires a Git repository")
    return Path(result.stdout.strip()).resolve()


def _dirty(cwd: Path) -> list[str]:
    result = _git(cwd, ["status", "--short", "--untracked-files=normal"])
    if result.returncode:
        raise HarnessError("could not inspect Git worktree")
    return [line for line in result.stdout.splitlines() if line]


def _diff_details(cwd: Path) -> tuple[str, list[dict], list[str]]:
    stat_result = _git(cwd, ["diff", "HEAD", "--stat", "--", "."])
    if stat_result.returncode:
        stat_result = _git(cwd, ["diff", "--stat", "--", "."])
    numstat = _git(cwd, ["diff", "HEAD", "--numstat", "--no-renames", "--", "."])
    if numstat.returncode:
        numstat = _git(cwd, ["diff", "--numstat", "--no-renames", "--", "."])
    details: list[dict] = []
    changed: list[str] = []
    for line in numstat.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, file_name = parts
        file_path = cwd / file_name
        details.append({
            "file": file_name,
            "added": added,
            "deleted": deleted,
            "untracked": False,
            "fingerprint": sha256(file_path) if file_path.is_file() else None,
        })
        changed.append(file_name)
    untracked = _git(cwd, ["ls-files", "--others", "--exclude-standard", "-z"])
    for raw in untracked.stdout.split("\0"):
        if not raw:
            continue
        path = cwd / raw
        size = path.stat().st_size if path.is_file() else 0
        details.append({
            "file": raw,
            "bytes": size,
            "untracked": True,
            "fingerprint": sha256(path) if path.is_file() else None,
        })
        changed.append(raw)
    return stat_result.stdout, details, changed


def _write_baseline(run_dir: Path, cwd: Path) -> None:
    stat, details, changed = _diff_details(cwd)
    atomic_write(run_dir / "baseline.json", dump_json({
        "cwd": str(cwd),
        "diff_stat": stat,
        "diff_summary": details,
        "changed_files": changed,
    }))


def _execution_delta(run_dir: Path, current: list[dict]) -> tuple[list[dict], set[str]]:
    baseline_path = run_dir / "baseline.json"
    if not baseline_path.exists():
        return current, set()
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return current, set()
    before = {
        item.get("file"): item
        for item in baseline.get("diff_summary", [])
        if isinstance(item, dict) and isinstance(item.get("file"), str)
    }
    after = {
        item.get("file"): item
        for item in current
        if isinstance(item, dict) and isinstance(item.get("file"), str)
    }
    delta = [item for name, item in after.items() if before.get(name) != item]
    for name, item in before.items():
        if name not in after:
            delta.append({
                "file": name,
                "untracked": item.get("untracked", False),
                "removed_preexisting_change": True,
            })
    return delta, set(before)


def _create_isolated_worktree(root: Path, run_dir: Path) -> Path:
    worktree = run_dir / "worktree"
    result = _git(root, ["worktree", "add", "--detach", str(worktree), "HEAD"], timeout=60)
    if result.returncode:
        raise HarnessError(f"could not create isolated worktree: {result.stderr.strip()}")
    atomic_write(run_dir / "ISOLATED_WORKTREE", str(worktree) + "\n")
    return worktree


def _new_run_dir(runtime_root: Path) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    run_dir = runtime_root / "runs" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, mode=0o700)
    return run_dir


def _tee(pipe, target: Path) -> None:
    with target.open("wb") as handle:
        while True:
            block = pipe.read(65536)
            if not block:
                break
            handle.write(block)


def _invoke_safe(command: list[str], task: str, env: dict[str, str], cwd: Path, run_dir: Path, timeout: int) -> int:
    """Separate wrapper keeps exception binding correct on Python 3.11+."""
    try:
        return _invoke_inner(command, task, env, cwd, run_dir, timeout)
    except KeyboardInterrupt:
        atomic_write(run_dir / "INTERRUPTED", "interrupt\n")
        return 130


def _invoke_inner(command: list[str], task: str, env: dict[str, str], cwd: Path, run_dir: Path, timeout: int) -> int:
    events = run_dir / "events.jsonl"
    stderr = run_dir / "stderr.log"
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    threads = [
        threading.Thread(target=_tee, args=(process.stdout, events), daemon=True),
        threading.Thread(target=_tee, args=(process.stderr, stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        assert process.stdin is not None
        process.stdin.write(task.encode("utf-8"))
        process.stdin.close()
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            atomic_write(run_dir / "INTERRUPTED", "timeout\n")
            return 124
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
        raise
    finally:
        for thread in threads:
            thread.join(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _command(codex: Path, role: dict, cwd: Path, run_dir: Path, resume: str | None = None) -> list[str]:
    schema = source_root() / "schemas/delegation-result.schema.json"
    if not schema.exists():
        schema = user_paths().install_root / "schemas/delegation-result.schema.json"
    base = [str(codex), "exec"]
    if resume:
        return [
            *base, "resume", resume,
            "--json", "-m", role["model"],
            "-c", f'model_reasoning_effort="{role["effort"]}"',
            "-c", 'model_provider="openai"',
            "-c", 'forced_login_method="chatgpt"',
            "--output-schema", str(schema),
            "-o", str(run_dir / "final.json"), "-",
        ]
    sandbox = "workspace-write" if role["write"] else "read-only"
    return [
        *base, "--json", "--sandbox", sandbox, "-C", str(cwd),
        "-m", role["model"],
        "-c", f'model_reasoning_effort="{role["effort"]}"',
        "-c", 'model_provider="openai"',
        "-c", 'forced_login_method="chatgpt"',
        "-c", 'shell_environment_policy.inherit="core"',
        "--output-schema", str(schema),
        "-o", str(run_dir / "final.json"), "-",
    ]


def delegate(
    role_name: str,
    kind: str,
    task_file: Path,
    cwd: Path,
    config_path: Path | None = None,
    home: Path | None = None,
    confirm_high_risk: bool = False,
) -> dict:
    if os.environ.get("CROSS_HARNESS_ACTIVE") == "1":
        raise HarnessError("nested cross-harness delegation from an active executor is blocked")
    paths = user_paths(home)
    config = load_config(config_path, paths.home)
    if role_name not in config["roles"]:
        raise ConfigError(f"unknown role: {role_name}")
    role = dict(config["roles"][role_name])
    if role["harness"] != "codex":
        raise ConfigError(f"role {role_name} is assigned to Claude, not Codex")
    if kind not in config["delegate_kinds"] or kind not in role["delegate_kinds"]:
        raise ConfigError(f"delegation kind {kind!r} is not allowed for {role_name}")
    if role_name == "security_reviewer" and not confirm_high_risk:
        raise HarnessError("security review requires --confirm-high-risk after explicit human confirmation")
    if not task_file.is_file():
        raise HarnessError(f"task file not found: {task_file}")
    if task_file.name.lower() in {"auth.json", ".env", "credentials.json"}:
        raise HarnessError("credential or environment files cannot be used as task files")
    task = task_file.read_text(encoding="utf-8")
    if not task.strip():
        raise HarnessError("task file is empty")
    if contains_secret(task):
        raise HarnessError("task file appears to contain credential material; refusing delegation")
    root = _git_root(cwd)
    runtime_root = Path(config["runtime_root"])
    run_dir = _new_run_dir(runtime_root)
    shutil.copy2(task_file, run_dir / "task.md")
    project = project_config(config, root)
    policy = project.get("dirty_worktree_policy", config["dirty_worktree_policy"])
    dirty = _dirty(root)
    execution_root = root
    if role["write"] and dirty:
        if policy == "stop":
            reason = "write delegation blocked by pre-existing changes:\n- " + "\n- ".join(dirty[:20])
            finalize_blocked_run(run_dir, role_name, role, kind, root, reason, "dirty_worktree")
            raise DirtyWorktreeError(f"{reason}\nrun state: {run_dir}")
        execution_root = _create_isolated_worktree(root, run_dir)
    _write_baseline(run_dir, execution_root)
    try:
        verify_codex_config_ownership(paths.home, root, execution_root)
        codex, cached = verify_codex_chatgpt(runtime_root, paths.home, config["auth_cache_hours"])
    except AuthError as exc:
        return finalize_blocked_run(run_dir, role_name, role, kind, execution_root, str(exc), "authentication")
    environment = sanitized_environment(paths.home, {
        "CROSS_HARNESS_ACTIVE": "1",
        "CROSS_HARNESS_PARENT": "claude",
        "CROSS_HARNESS_RUN_DIR": str(run_dir),
    })
    command = _command(codex, role, execution_root, run_dir)
    atomic_write(run_dir / "command.json", dump_json({"argv": command, "auth_cached": cached, "cwd": str(execution_root)}))
    exit_code = _invoke_safe(command, task, environment, execution_root, run_dir, role["timeout_seconds"])
    return finalize_run(run_dir, role_name, role, kind, execution_root, exit_code, attempt=1)


def finalize_blocked_run(
    run_dir: Path,
    role_name: str,
    role: dict,
    kind: str,
    cwd: Path,
    reason: str,
    category: str,
    attempts: int = 0,
    thread_id: str | None = None,
    signatures: list[str] | None = None,
) -> dict:
    final = {
        "status": "blocked",
        "work_completed": "Codex was not started.",
        "changed_files": [],
        "tests": [],
        "error": reason,
        "next_decision": "Resolve the blocking condition and start a new delegation.",
    }
    atomic_write(run_dir / "events.jsonl", "")
    atomic_write(run_dir / "stderr.log", reason + "\n")
    atomic_write(run_dir / "final.json", dump_json(final))
    atomic_write(run_dir / "diff-stat.txt", "")
    summary = {
        "status": "blocked",
        "run_dir": str(run_dir),
        "exit_code": None,
        "role": role_name,
        "kind": kind,
        "model": role["model"],
        "effort": role["effort"],
        "attempt": attempts,
        "thread_id": thread_id,
        "changed_files": [],
        "tests": [],
        "work_completed": final["work_completed"],
        "error": reason,
        "next_decision": final["next_decision"],
        "usage": {},
        "failure_signature": None,
        "event_log": str(run_dir / "events.jsonl"),
        "stderr_log": str(run_dir / "stderr.log"),
        "final_message": str(run_dir / "final.json"),
        "cwd": str(cwd),
    }
    state = {
        "role": role_name,
        "kind": kind,
        "cwd": str(cwd),
        "thread_id": thread_id,
        "attempts": attempts,
        "signatures": signatures or [],
        "escalated": False,
        "status": "blocked",
        "blocked_category": category,
        "blocked_reason": reason,
        "model": role["model"],
        "effort": role["effort"],
    }
    atomic_write(run_dir / "summary.json", dump_json(summary))
    atomic_write(run_dir / "state.json", dump_json(state))
    atomic_write(run_dir / "BLOCKED", f"{category}: {reason}\n")
    atomic_write(run_dir / "summary.txt", render_summary(summary, role["output_limit_chars"]))
    return summary


def finalize_run(run_dir: Path, role_name: str, role: dict, kind: str, cwd: Path, exit_code: int, attempt: int) -> dict:
    parsed = parse_events(run_dir / "events.jsonl")
    final = load_final(run_dir / "final.json") or {}
    stderr = (run_dir / "stderr.log").read_text(encoding="utf-8", errors="replace") if (run_dir / "stderr.log").exists() else ""
    event_failed = bool(parsed.get("errors"))
    status = final.get("status") if final.get("status") in {"success", "failed", "blocked", "partial"} else ("success" if exit_code == 0 and not event_failed else "failed")
    if exit_code != 0 and status == "success":
        status = "failed"
    if event_failed and status == "success":
        status = "failed"
    combined_error = str(final.get("error") or "\n".join(parsed.get("errors", [])[-3:]) or stderr[-2000:] or "")
    blocked_category: str | None = None
    if RATE_LIMIT.search(combined_error):
        status = "blocked"
        blocked_category = "rate_limit"
        combined_error = "rate limit detected; no fallback or automatic waiting was attempted"
    elif AUTH_FAILURE.search(combined_error):
        status = "blocked"
        blocked_category = "authentication"
        combined_error = "authentication failure detected; no billing fallback was attempted"
    elif status == "blocked":
        blocked_category = "executor_reported"
    diff_stat, current_diff_summary, _ = _diff_details(cwd)
    diff_summary, baseline_names = _execution_delta(run_dir, current_diff_summary)
    detected_changed = [item["file"] for item in diff_summary]
    if not diff_summary:
        diff_stat = ""
    atomic_write(run_dir / "diff-stat.txt", diff_stat)
    reported_changed = final.get("changed_files") if isinstance(final.get("changed_files"), list) else []
    filtered_reported = [name for name in reported_changed if name not in baseline_names or name in detected_changed]
    changed = list(dict.fromkeys([*detected_changed, *filtered_reported]))
    signature = failure_signature(exit_code, parsed, stderr)
    summary = {
        "status": status,
        "run_dir": str(run_dir),
        "exit_code": exit_code,
        "role": role_name,
        "kind": kind,
        "model": role["model"],
        "effort": role["effort"],
        "attempt": attempt,
        "thread_id": parsed.get("thread_id"),
        "changed_files": changed,
        "diff_summary": diff_summary,
        "tests": final.get("tests", []) if isinstance(final.get("tests"), list) else [],
        "work_completed": str(final.get("work_completed", "")),
        "error": combined_error[:4000] or None,
        "next_decision": final.get("next_decision"),
        "usage": parsed.get("usage", {}),
        "failure_signature": signature,
        "event_log": str(run_dir / "events.jsonl"),
        "stderr_log": str(run_dir / "stderr.log"),
        "final_message": str(run_dir / "final.json"),
        "diff_stat_file": str(run_dir / "diff-stat.txt"),
        "baseline_file": str(run_dir / "baseline.json"),
        "cwd": str(cwd),
    }
    state = {
        "role": role_name,
        "kind": kind,
        "cwd": str(cwd),
        "thread_id": parsed.get("thread_id"),
        "attempts": attempt,
        "signatures": [signature] if signature else [],
        "escalated": False,
        "status": status,
        "model": role["model"],
        "effort": role["effort"],
    }
    if blocked_category:
        state["blocked_category"] = blocked_category
        state["blocked_reason"] = combined_error
    raw_paths = (run_dir / "events.jsonl", run_dir / "stderr.log", run_dir / "final.json")
    summary["raw_artifact_bytes"] = sum(path.stat().st_size for path in raw_paths if path.exists())
    summary["summary_bytes"] = 0
    summary["compression_percent"] = 0.0
    summary_text = ""
    for _ in range(4):
        summary_text = render_summary(summary, role["output_limit_chars"])
        byte_count = len(summary_text.encode("utf-8"))
        percent = (
            (1 - byte_count / summary["raw_artifact_bytes"]) * 100
            if summary["raw_artifact_bytes"]
            else 0.0
        )
        if byte_count == summary["summary_bytes"] and abs(percent - summary["compression_percent"]) < 0.01:
            break
        summary["summary_bytes"] = byte_count
        summary["compression_percent"] = percent
    atomic_write(run_dir / "summary.json", dump_json(summary))
    atomic_write(run_dir / "state.json", dump_json(state))
    if blocked_category:
        atomic_write(run_dir / "BLOCKED", f"{blocked_category}: {combined_error}\n")
    atomic_write(run_dir / "summary.txt", summary_text)
    return summary


def _escalated_role(role: dict, config: dict) -> dict:
    updated = dict(role)
    chain = config["fallback"]["codex"]
    if role["model"] in chain:
        index = chain.index(role["model"])
        if index > 0:
            updated["model"] = chain[index - 1]
    efforts = ["minimal", "low", "medium", "high", "xhigh"]
    if role["effort"] in efforts and efforts.index(role["effort"]) < len(efforts) - 1:
        updated["effort"] = efforts[efforts.index(role["effort"]) + 1]
    return updated


def retry(run_dir: Path, task_file: Path, config_path: Path | None = None, home: Path | None = None) -> dict:
    if os.environ.get("CROSS_HARNESS_ACTIVE") == "1":
        raise HarnessError("nested cross-harness retry from an active executor is blocked")
    paths = user_paths(home)
    config = load_config(config_path, paths.home)
    state_path = run_dir / "state.json"
    if not state_path.exists():
        raise HarnessError(f"run state not found: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    role = dict(config["roles"][state["role"]])
    if state["status"] == "blocked":
        raise HarnessError("blocked runs cannot be retried automatically")
    if state["attempts"] > role["retries"]:
        raise HarnessError("normal retry budget exhausted")
    if not task_file.is_file() or not task_file.read_text(encoding="utf-8").strip():
        raise HarnessError("retry delta task file is missing or empty")
    retry_root = _new_run_dir(Path(config["runtime_root"]))
    shutil.copy2(task_file, retry_root / "task.md")
    _write_baseline(retry_root, Path(state["cwd"]))
    try:
        verify_codex_config_ownership(paths.home, _git_root(Path(state["cwd"])), Path(state["cwd"]))
        codex, cached = verify_codex_chatgpt(Path(config["runtime_root"]), paths.home, config["auth_cache_hours"])
    except AuthError as exc:
        return finalize_blocked_run(
            retry_root,
            state["role"],
            role,
            state["kind"],
            Path(state["cwd"]),
            str(exc),
            "authentication",
            attempts=state["attempts"],
            thread_id=state["thread_id"],
            signatures=state.get("signatures", []),
        )
    environment = sanitized_environment(paths.home, {
        "CROSS_HARNESS_ACTIVE": "1",
        "CROSS_HARNESS_PARENT": "claude",
        "CROSS_HARNESS_RUN_DIR": str(retry_root),
    })
    command = _command(codex, role, Path(state["cwd"]), retry_root, state["thread_id"])
    atomic_write(retry_root / "command.json", dump_json({"argv": command, "auth_cached": cached, "resume": state["thread_id"]}))
    exit_code = _invoke_safe(command, task_file.read_text(encoding="utf-8"), environment, Path(state["cwd"]), retry_root, role["timeout_seconds"])
    summary = finalize_run(retry_root, state["role"], role, state["kind"], Path(state["cwd"]), exit_code, state["attempts"] + 1)
    new_state = json.loads((retry_root / "state.json").read_text(encoding="utf-8"))
    new_state["signatures"] = [*state.get("signatures", []), *new_state.get("signatures", [])]
    identical = summary.get("failure_signature") and new_state["signatures"].count(summary["failure_signature"]) >= 2
    if summary["status"] == "failed" and identical and not state.get("escalated"):
        new_state["escalated"] = True
        atomic_write(retry_root / "state.json", dump_json(new_state))
        escalation = _escalated_role(role, config)
        escalation_root = _new_run_dir(Path(config["runtime_root"]))
        shutil.copy2(task_file, escalation_root / "task.md")
        _write_baseline(escalation_root, Path(state["cwd"]))
        command = _command(codex, escalation, Path(state["cwd"]), escalation_root, summary.get("thread_id") or state["thread_id"])
        atomic_write(escalation_root / "command.json", dump_json({"argv": command, "escalation": True, "previous_run": str(retry_root)}))
        code = _invoke_safe(command, task_file.read_text(encoding="utf-8"), environment | {"CROSS_HARNESS_RUN_DIR": str(escalation_root)}, Path(state["cwd"]), escalation_root, escalation["timeout_seconds"])
        escalated_summary = finalize_run(escalation_root, state["role"], escalation, state["kind"], Path(state["cwd"]), code, new_state["attempts"] + 1)
        escalated_state = json.loads((escalation_root / "state.json").read_text(encoding="utf-8"))
        escalated_state["escalated"] = True
        escalated_state["signatures"] = [*new_state["signatures"], *escalated_state.get("signatures", [])]
        atomic_write(escalation_root / "state.json", dump_json(escalated_state))
        return escalated_summary
    atomic_write(retry_root / "state.json", dump_json(new_state))
    return summary
