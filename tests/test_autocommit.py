"""Root auto-commit contracts exercised against real repositories and hooks."""

from pathlib import Path
import json
import subprocess

import pytest

from cross_harness import runner
from cross_harness.config import load_config
from cross_harness.errors import HarnessError


THREAD = "00000000-0000-0000-0000-000000000001"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def execution(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Run Author")
    git(repo, "config", "user.email", "author@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    for name in ("README.md", "user.txt"):
        (repo / name).write_text("before\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config.toml"
    config.write_text("auto_commit = true\n")
    task = tmp_path / "task.md"
    task.write_text("# Goal\nImplement the change\n\n# Checks\n- fixture\n")
    monkeypatch.setattr(runner, "verify_codex_config_ownership", lambda *args: None)
    monkeypatch.setattr(runner, "verify_codex_chatgpt", lambda *args: (Path("/fixture/codex"), False))
    state = {"repo": repo, "home": home, "config": config, "task": task, "calls": [], "status": "success"}

    def invoke(command, prompt, env, cwd, run, timeout):
        state["calls"].append(run)
        state["branch_at_execution"] = runner._current_branch(cwd)
        state["subject_at_execution"] = (run / "commit-subject.txt").read_text().strip()
        if "change" in state:
            state["change"](cwd, run)
        else:
            (cwd / "delegated.txt").write_text("delegated\n")
        state["index_before_commit"] = (cwd / ".git/index").read_bytes() if (cwd / ".git").is_dir() else None
        events = [{"type": "thread.started", "thread_id": THREAD}]
        if state.get("check", "passed") != "not_run":
            events.append({"type": "item.completed", "item": {
                "type": "command_execution", "command": "fixture", "status": "completed",
                "exit_code": 1 if state.get("check") == "failed" else 0,
            }})
        (run / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": state["status"], "work_completed": "done", "changed_files": ["delegated.txt"],
            "tests": ["fixture"], "error": None, "next_decision": None,
            "discussion_points": ["A proposal"] if state["status"] == "discussion" else [],
        }))
        return 0

    monkeypatch.setattr(runner, "_invoke_safe", invoke)
    return state


def delegate(state, role="implementer", kind="implementation"):
    return runner.delegate(role, kind, state["task"], state["repo"], state["config"], state["home"])


def test_success_creates_one_commit_on_recorded_work_branch(execution):
    state = execution
    repo = state["repo"]
    parent = git(repo, "rev-parse", "HEAD")
    state["task"].write_text("# Goal\nIgnored goal\n# Commit message\nAdd the requested feature!\n# Checks\n- fixture\n")
    hook_log = repo.parent / "hook-log"
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text(f'#!/bin/sh\nprintf "called\\n" >> "{hook_log}"\n')
    hook.chmod(0o755)

    summary = delegate(state)

    commit = summary["commit"]
    assert summary["status"] == "success"
    assert commit["status"] == "committed"
    assert commit["sha"] == git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "HEAD^") == parent
    assert git(repo, "show", "--format=%s", "--no-patch") == "Add the requested feature!"
    assert git(repo, "show", "--format=%an <%ae>", "--no-patch") == "Run Author <author@example.com>"
    assert commit["paths"] == ["delegated.txt"]
    assert commit["excluded_paths"] == []
    assert commit["branch"].startswith("cross-harness/")
    assert commit["branch"].endswith("-add-the-requested-feature")
    assert state["branch_at_execution"] == commit["branch"]
    assert state["subject_at_execution"] == commit["subject"]
    run = Path(summary["run_dir"])
    assert (run / "WORK_BRANCH").read_text().strip() == commit["branch"]
    assert json.loads((run / "summary.json").read_text())["commit"] == commit
    assert f"commit_sha: {commit['sha']}" in (run / "summary.txt").read_text()
    assert git(repo, "status", "--porcelain") == ""
    assert hook_log.read_text() == "called\n"
    runtime = Path(load_config(state["config"], state["home"])["runtime_root"])
    assert "delegated.txt" not in runner._load_delegated_changes(runtime)[str(repo.resolve())]


@pytest.mark.parametrize("detached", [False, True])
def test_branch_preparation_preserves_worktree_and_index(execution, detached):
    state = execution
    repo = state["repo"]
    if detached:
        git(repo, "checkout", "--detach")
    state["config"].write_text('dirty_worktree_policy = "allow"\nwork_branch_prefix = "work/"\n')
    (repo / "user.txt").write_text("staged\n")
    git(repo, "add", "user.txt")
    (repo / "user.txt").write_text("unstaged\n")
    index = (repo / ".git/index").read_bytes()
    state["status"] = "failed"

    def change(cwd, run):
        assert (cwd / ".git/index").read_bytes() == index
        assert (cwd / "user.txt").read_text() == "unstaged\n"
        (cwd / "delegated.txt").write_text("delegated\n")

    state["change"] = change
    summary = delegate(state)
    assert summary["commit"]["status"] == "skipped"
    assert git(repo, "symbolic-ref", "--short", "HEAD").startswith("work/")
    assert (repo / ".git/index").read_bytes() == index
    assert git(repo, "show", ":user.txt") == "staged"


def test_existing_unprotected_branch_is_used(execution):
    state = execution
    git(state["repo"], "checkout", "-b", "feature")
    summary = delegate(state)
    assert summary["commit"]["branch"] == "feature"
    assert git(state["repo"], "branch", "--list", "cross-harness/*") == ""


@pytest.mark.parametrize("enabled", [False, True])
def test_project_settings_override_global_settings(execution, enabled):
    state = execution
    repo = state["repo"]
    git(repo, "branch", "-m", "release")
    state["config"].write_text(
        f'auto_commit = {str(not enabled).lower()}\nprotected_branches = []\n'
        f'[projects.{json.dumps(str(repo.resolve()))}]\n'
        f'auto_commit = {str(enabled).lower()}\nprotected_branches = ["release"]\n'
    )
    summary = delegate(state)
    assert summary["commit"]["status"] == ("committed" if enabled else "disabled")
    if enabled:
        assert state["branch_at_execution"].startswith("cross-harness/")
    else:
        assert state["branch_at_execution"] == "release"
    assert git(repo, "status", "--porcelain") == ("" if enabled else "?? delegated.txt")


def test_disabled_auto_commit_preserves_detached_head(execution):
    state = execution
    state["config"].write_text("auto_commit = false\n")
    git(state["repo"], "checkout", "--detach")
    head = git(state["repo"], "rev-parse", "HEAD")
    summary = delegate(state)
    assert summary["status"] == "success"
    assert summary["commit"]["status"] == "disabled"
    assert runner._current_branch(state["repo"]) is None
    assert git(state["repo"], "rev-parse", "HEAD") == head
    assert not (Path(summary["run_dir"]) / "WORK_BRANCH").exists()


@pytest.mark.parametrize("project_override", [False, True])
@pytest.mark.parametrize("resume", [False, True])
def test_disabled_auto_commit_allows_unfinished_operation(execution, project_override, resume):
    state = execution
    repo = state["repo"]
    state["config"].write_text(
        'auto_commit = true\n'
        f'[projects.{json.dumps(str(repo.resolve()))}]\nauto_commit = false\n'
        if project_override else 'auto_commit = false\n'
    )
    head = git(repo, "rev-parse", "HEAD")
    operation = repo / ".git/MERGE_HEAD"
    operation.write_text(head + "\n")
    if resume:
        state["status"] = "partial"
        first = delegate(state)
        state["status"] = "success"
        state["change"] = lambda cwd, run: (cwd / "second.txt").write_text("second\n")
        summary = runner.retry(Path(first["run_dir"]), state["task"], state["config"], state["home"])
    else:
        summary = delegate(state)
    assert summary["status"] == "success"
    assert summary["commit"]["status"] == "disabled"
    assert git(repo, "rev-parse", "HEAD") == head
    assert runner._current_branch(repo) == "main"
    assert operation.read_text() == head + "\n"
    assert (repo / "delegated.txt").read_text() == "delegated\n"
    assert not (Path(summary["run_dir"]) / "WORK_BRANCH").exists()


def test_repository_without_commits_runs_without_a_branch_or_commit(execution):
    state = execution
    repo = state["repo"].parent / "unborn"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    state["repo"] = repo

    def change(cwd, run):
        (cwd / "delegated.txt").write_text("delegated\n")
        git(cwd, "add", "delegated.txt")

    state["change"] = change
    summary = delegate(state)
    assert summary["status"] == "success"
    assert summary["commit"]["status"] == "skipped"
    assert summary["commit"]["reason"] == "repository has no commits"
    assert state["branch_at_execution"] == runner._current_branch(repo) == "main"
    head = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=repo, capture_output=True)
    assert head.returncode == 1
    assert git(repo, "branch", "--list") == ""
    assert git(repo, "status", "--porcelain") == "A  delegated.txt"
    assert (repo / ".git/index").read_bytes() == state["index_before_commit"]
    assert not (Path(summary["run_dir"]) / "WORK_BRANCH").exists()


def test_work_branch_lock_contention_is_bounded_and_separate_from_root_lock(execution, monkeypatch):
    state = execution
    runtime = Path(load_config(state["config"], state["home"])["runtime_root"])
    root_lock = runner._root_lock_path(runtime, state["repo"])
    branch_lock = root_lock.with_name(root_lock.name.replace("root-", "work-branch-", 1))
    descriptor = runner._try_lock(branch_lock)
    monkeypatch.setattr(runner, "_DELEGATED_CHANGES_LOCK_TIMEOUT_SECONDS", 0.01)
    try:
        with pytest.raises(HarnessError, match="timed out waiting for lock"):
            delegate(state)
    finally:
        runner._release_lock(descriptor)
    assert state["calls"] == []
    assert not runner._HELD_ROOT_LOCKS
    assert root_lock != branch_lock
    assert git(state["repo"], "symbolic-ref", "--short", "HEAD") == "main"


def test_work_branch_failure_blocks_before_executor_and_releases_locks(execution, monkeypatch):
    state = execution
    real_git = runner._git

    def fail_branch(cwd, args, timeout=30):
        if args[0] == "branch":
            return subprocess.CompletedProcess(args, 128, "", "cannot lock ref\n")
        return real_git(cwd, args, timeout)

    monkeypatch.setattr(runner, "_git", fail_branch)
    with pytest.raises(HarnessError, match="cannot lock ref"):
        delegate(state)
    assert state["calls"] == []
    assert not runner._HELD_ROOT_LOCKS
    runtime = Path(load_config(state["config"], state["home"])["runtime_root"])
    run = next((runtime / "runs").iterdir())
    assert json.loads((run / "state.json").read_text())["blocked_category"] == "work_branch"
    assert json.loads((run / "summary.json").read_text())["commit"]["reason"] == "status not success"
    lock = next((runtime / "locks").glob("work-branch-*.lock"))
    descriptor = runner._try_lock(lock)
    assert descriptor is not None
    runner._release_lock(descriptor)


def test_detached_start_block_records_skip_and_resolved_subject(execution, monkeypatch):
    state = execution
    monkeypatch.setattr(runner, "_reserve_parallel_capacity", lambda *args: "capacity full")
    run = runner.start_detached_delegate(
        "implementer", "implementation", state["task"], state["repo"], state["config"], state["home"],
    )
    summary = json.loads((run / "summary.json").read_text())
    assert summary["status"] == "blocked"
    assert summary["commit"]["status"] == "skipped"
    assert summary["commit"]["reason"] == "status not success"
    assert summary["commit"]["subject"] == "Implement the change"
    assert not (run / "WORK_BRANCH").exists()
    assert state["calls"] == []
    assert git(state["repo"], "symbolic-ref", "--short", "HEAD") == "main"


@pytest.mark.parametrize("status", ["failed", "partial", "blocked", "discussion"])
def test_unsuccessful_runs_never_commit(execution, status):
    state = execution
    state["status"] = status
    head = git(state["repo"], "rev-parse", "HEAD")
    summary = delegate(state)
    assert summary["status"] == status
    assert summary["commit"]["status"] == "skipped"
    assert summary["commit"]["reason"] == "status not success"
    assert git(state["repo"], "rev-parse", "HEAD") == head
    assert (state["repo"] / "delegated.txt").read_text() == "delegated\n"


@pytest.mark.parametrize("check,status", [("failed", "failed"), ("not_run", "partial")])
def test_commit_waits_for_check_demotions(execution, check, status):
    execution["check"] = check
    summary = delegate(execution)
    assert summary["status"] == status
    assert summary["commit"]["reason"] == "status not success"


def test_no_changes_skips_commit(execution):
    execution["change"] = lambda cwd, run: None
    summary = delegate(execution)
    assert summary["status"] == "success"
    assert summary["commit"]["reason"] == "no changes"


@pytest.mark.parametrize("change", ["edit", "delete", "rename", "literal path"])
def test_commit_includes_all_kinds_of_run_paths(execution, change):
    state = execution
    repo = state["repo"]

    def mutate(cwd, run):
        if change == "edit":
            (cwd / "README.md").write_text("changed\n")
        elif change == "delete":
            (cwd / "README.md").unlink()
        elif change == "rename":
            (cwd / "README.md").rename(cwd / "renamed.txt")
        else:
            (cwd / ":(glob)*.txt").write_text("literal\n")

    state["change"] = mutate
    summary = delegate(state)
    assert summary["status"] == "success"
    assert summary["commit"]["status"] == "committed"
    expected = {
        "edit": ["README.md"], "delete": ["README.md"],
        "rename": ["README.md", "renamed.txt"], "literal path": [":(glob)*.txt"],
    }
    assert summary["commit"]["paths"] == expected[change]
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-list", "--count", "HEAD") == "2"


def test_self_reversion_demotion_prevents_commit(execution, monkeypatch):
    monkeypatch.setattr(runner, "_self_reversions", lambda *args: [{"target": "README.md", "source": "git"}])
    summary = delegate(execution)
    assert summary["status"] == "partial"
    assert summary["commit"]["reason"] == "status not success"
    assert git(execution["repo"], "rev-list", "--count", "HEAD") == "1"


def test_isolated_run_commits_and_integrates(execution):
    state = execution
    state["config"].write_text('dirty_worktree_policy = "isolate"\n')
    summary = delegate(state)
    run = Path(summary["run_dir"])
    assert summary["status"] == "success"
    assert summary["commit"]["status"] == "committed"
    assert summary["integration"]["status"] == "integrated"
    assert (run / "INTEGRATED").read_text().strip() == git(state["repo"], "rev-parse", "HEAD")
    assert not (run / "ISOLATED_WORKTREE").exists()
    assert not (run / "worktree").exists()
    assert git(state["repo"], "symbolic-ref", "--short", "HEAD").startswith("cross-harness/")
    assert git(state["repo"], "status", "--porcelain") == ""
    assert (state["repo"] / "delegated.txt").read_text() == "delegated\n"


def test_read_only_role_has_no_commit_record_or_branch(execution, monkeypatch):
    state = execution
    state["change"] = lambda cwd, run: None
    monkeypatch.setattr(runner, "verify_claude_config_ownership", lambda *args: None)
    monkeypatch.setattr(runner, "verify_claude_subscription", lambda *args: (Path("/fixture/claude"), False))
    monkeypatch.setattr(runner, "_write_claude_final_from_events", lambda *args: None)
    summary = delegate(state, "reviewer", "review")
    assert summary["status"] == "success"
    assert "commit" not in summary
    assert not (Path(summary["run_dir"]) / "WORK_BRANCH").exists()
    assert git(state["repo"], "symbolic-ref", "--short", "HEAD") == "main"


def test_allow_preserves_staged_and_unstaged_user_changes_and_excludes_overlap(execution):
    state = execution
    repo = state["repo"]
    state["config"].write_text('dirty_worktree_policy = "allow"\n')
    (repo / "user.txt").write_text("staged\n")
    git(repo, "add", "user.txt")
    (repo / "user.txt").write_text("user working\n")

    def change(cwd, run):
        (cwd / "user.txt").write_text("user and delegated working\n")
        (cwd / "delegated.txt").write_text("delegated\n")

    state["change"] = change
    summary = delegate(state)
    assert summary["commit"]["paths"] == ["delegated.txt"]
    assert summary["commit"]["excluded_paths"] == ["user.txt"]
    assert git(repo, "diff", "--cached", "--name-only") == "user.txt"
    assert git(repo, "show", ":user.txt") == "staged"
    assert git(repo, "show", "HEAD:user.txt") == "before"
    assert (repo / "user.txt").read_text() == "user and delegated working\n"
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == "delegated.txt"


def test_allow_excludes_staged_user_change_cancelled_by_worktree(execution):
    state = execution
    repo = state["repo"]
    state["config"].write_text('dirty_worktree_policy = "allow"\n')
    (repo / "user.txt").write_text("staged user change\n")
    git(repo, "add", "user.txt")
    (repo / "user.txt").write_text("before\n")
    assert git(repo, "diff", "HEAD", "--", "user.txt") == ""

    def change(cwd, run):
        (cwd / "user.txt").write_text("delegated overlap\n")
        (cwd / "delegated.txt").write_text("delegated\n")

    state["change"] = change
    summary = delegate(state)
    assert summary["commit"]["paths"] == ["delegated.txt"]
    assert summary["commit"]["excluded_paths"] == ["user.txt"]
    assert git(repo, "show", ":user.txt") == "staged user change"
    assert git(repo, "show", "HEAD:user.txt") == "before"
    assert (repo / "user.txt").read_text() == "delegated overlap\n"


def test_only_overlap_skips_without_staging(execution):
    state = execution
    state["config"].write_text('dirty_worktree_policy = "allow"\n')
    (state["repo"] / "user.txt").write_text("user\n")
    git(state["repo"], "add", "user.txt")
    state["change"] = lambda cwd, run: (cwd / "user.txt").write_text("user and executor\n")
    summary = delegate(state)
    assert summary["commit"]["reason"] == "overlap with pre-existing changes"
    assert summary["commit"]["paths"] == []
    assert summary["commit"]["excluded_paths"] == ["user.txt"]
    assert (state["repo"] / ".git/index").read_bytes() == state["index_before_commit"]


def test_hook_failure_restores_index_and_preserves_worktree(execution):
    state = execution
    repo = state["repo"]
    state["config"].write_text('dirty_worktree_policy = "allow"\n')
    (repo / "user.txt").write_text("staged\n")
    git(repo, "add", "user.txt")
    (repo / "user.txt").write_text("unstaged\n")
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text('#!/bin/sh\nprintf "hook refused\\n" >&2\nexit 1\n')
    hook.chmod(0o755)
    head = git(repo, "rev-parse", "HEAD")
    summary = delegate(state)
    assert summary["status"] == "partial"
    assert summary["commit"]["status"] == "failed"
    assert "hook refused" in summary["commit"]["reason"]
    assert summary["error"].startswith("auto-commit failed")
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / ".git/index").read_bytes() == state["index_before_commit"]
    assert (repo / "user.txt").read_text() == "unstaged\n"
    assert (repo / "delegated.txt").read_text() == "delegated\n"
    assert git(repo, "diff", "--cached", "--name-only") == "user.txt"


def test_retry_commits_baseline_delegated_paths_and_then_can_retry_clean_commit(execution):
    state = execution
    state["status"] = "failed"
    first = delegate(state)
    previous = Path(first["run_dir"])
    state["status"] = "success"
    state["change"] = lambda cwd, run: (cwd / "second.txt").write_text("second\n")
    delta_task = state["task"].parent / "delta.md"
    delta_task.write_text("# Goal\nA different goal\n# Checks\n- fixture\n")
    second = runner.retry(previous, delta_task, state["config"], state["home"])
    assert second["commit"]["status"] == "committed"
    assert second["commit"]["paths"] == ["delegated.txt", "second.txt"]
    assert second["commit"]["subject"] == first["commit"]["subject"] == "Implement the change"
    assert git(state["repo"], "status", "--porcelain") == ""
    state["change"] = lambda cwd, run: (cwd / "third.txt").write_text("third\n")
    third = runner.retry(Path(second["run_dir"]), delta_task, state["config"], state["home"])
    assert third["commit"]["status"] == "committed"
    assert third["commit"]["paths"] == ["third.txt"]
    assert third["commit"]["sha"] != second["commit"]["sha"]
    assert git(state["repo"], "rev-parse", "HEAD^") == second["commit"]["sha"]
    assert git(state["repo"], "status", "--porcelain") == ""


def test_reply_inherits_subject_and_explicit_message_overrides_it(execution):
    state = execution
    state["status"] = "discussion"
    state["task"].write_text("# Goal\nOld goal\n# Commit message\nOriginal subject\n# Checks\n- fixture\n")
    first = delegate(state)
    reply_task = state["task"].parent / "reply.md"
    reply_task.write_text("Accept the proposal\n")
    second = runner.reply(Path(first["run_dir"]), reply_task, state["config"], state["home"])
    assert second["commit"]["subject"] == "Original subject"
    reply_task.write_text("# Commit message\nNew subject\n")
    state["status"] = "success"
    third = runner.reply(Path(second["run_dir"]), reply_task, state["config"], state["home"])
    assert third["status"] == "success"
    assert third["commit"]["subject"] == "New subject"
    assert third["commit"]["status"] == "committed"


def test_escalation_inherits_subject_and_commits_only_on_success(execution, monkeypatch):
    state = execution
    state["status"] = "failed"
    monkeypatch.setattr(runner, "failure_signature", lambda *args: "same failure")
    first = delegate(state)
    task = state["task"].parent / "delta.md"
    task.write_text("# Goal\nA different goal\n# Checks\n- fixture\n")
    invoke = runner._invoke_safe

    def escalation(command, prompt, env, cwd, run, timeout):
        state["status"] = "success" if len(state["calls"]) == 2 else "failed"
        return invoke(command, prompt, env, cwd, run, timeout)

    monkeypatch.setattr(runner, "_invoke_safe", escalation)
    summary = runner.retry(Path(first["run_dir"]), task, state["config"], state["home"])
    assert len(state["calls"]) == 3
    assert summary["commit"]["status"] == "committed"
    assert summary["commit"]["subject"] == "Implement the change"
    assert git(state["repo"], "rev-list", "--count", "HEAD") == "2"
    assert git(state["repo"], "status", "--porcelain") == ""


@pytest.mark.parametrize("subject", ["ALLCAPS " * 12, "機能追加"])
def test_branch_slug_is_bounded_ascii_with_run_name_fallback(execution, subject):
    state = execution
    state["task"].write_text(f"# Commit message\n{subject}\n# Checks\n- fixture\n")
    summary = delegate(state)
    slug = summary["commit"]["branch"].split("/", 1)[1].split("-", 2)[2]
    assert len(slug) <= 40
    assert all(char in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in slug)
    assert "--" not in slug
    if subject == "機能追加":
        assert slug == Path(summary["run_dir"]).name.lower()[:40]


@pytest.mark.parametrize("goal", ["A" * 100, None])
def test_subject_falls_back_to_truncated_goal_or_role_and_run(execution, goal):
    state = execution
    state["task"].write_text((f"# Goal\n{goal}\n" if goal else "") + "# Checks\n- fixture\n")
    summary = delegate(state)
    expected = goal[:72] if goal else f"cross-harness: implementer {Path(summary['run_dir']).name}"
    assert summary["commit"]["subject"] == expected


@pytest.mark.parametrize("branch", ["main", None])
def test_finalize_never_commits_if_executor_changed_to_unsafe_head(execution, branch):
    state = execution

    def change(cwd, run):
        git(cwd, "symbolic-ref", "HEAD", "refs/heads/main") if branch else git(cwd, "checkout", "--detach")
        (cwd / "delegated.txt").write_text("delegated\n")

    state["change"] = change
    summary = delegate(state)
    assert summary["commit"]["status"] == "skipped"
    assert summary["commit"]["reason"].startswith("protected branch")
    assert git(state["repo"], "rev-list", "--count", "HEAD") == "1"


def test_unavailable_diff_never_commits(execution, monkeypatch):
    state = execution

    def change(cwd, run):
        (cwd / "delegated.txt").write_text("delegated\n")

        def unavailable(*args):
            raise OSError("Git unavailable")

        monkeypatch.setattr(runner, "_diff_details", unavailable)

    state["change"] = change
    summary = delegate(state)
    assert summary["diff_check"] == "unavailable"
    assert summary["commit"]["reason"] == "diff check unavailable"
    assert git(state["repo"], "rev-list", "--count", "HEAD") == "1"
