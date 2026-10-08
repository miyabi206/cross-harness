"""Unit lifecycle contracts against real Git repositories and worktrees."""

from datetime import datetime
from pathlib import Path
import json
import re
import subprocess
import threading
import time

import pytest

from cross_harness import runner
from cross_harness.cli import main
from cross_harness.config import load_config
from cross_harness.errors import HarnessError
from test_autocommit import delegate, execution, git


@pytest.fixture
def isolated(execution):
    execution["config"].write_text('auto_commit = true\ndirty_worktree_policy = "isolate"\n')
    return execution


def runtime(state):
    return Path(load_config(state["config"], state["home"])["runtime_root"]).resolve()


def worktree(summary):
    return Path((Path(summary["run_dir"]) / "ISOLATED_WORKTREE").read_text().strip())


def snapshot(repo):
    index = Path(git(repo, "rev-parse", "--git-path", "index"))
    if not index.is_absolute():
        index = repo / index
    return (
        git(repo, "rev-parse", "HEAD"), runner._current_branch(repo),
        index.read_bytes(),
        {str(path.relative_to(repo)): runner._adopt_snapshot(path)
         for path in repo.rglob("*") if ".git" not in path.relative_to(repo).parts and not path.is_dir()},
    )


@pytest.mark.parametrize("detached", [False, True])
def test_integration_records_commit_and_removes_worktree(isolated, detached):
    state = isolated
    repo = state["repo"]
    parent = git(repo, "rev-parse", "HEAD")
    if detached:
        git(repo, "checkout", "--detach")
    commands = []
    real_git = runner._git

    def logged_git(cwd, args, timeout=30):
        commands.append((args, timeout))
        return real_git(cwd, args, timeout)

    state["change"] = lambda cwd, run: (cwd / "delegated.txt").write_text("delegated\n")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner, "_git", logged_git)
        summary = delegate(state)
    run = Path(summary["run_dir"])
    assert summary["status"] == "success"
    assert state["branch_at_execution"] is None
    assert summary["commit"]["status"] == "committed"
    assert summary["commit"]["branch"] is None
    assert summary["integration"]["status"] == "integrated"
    assert summary["integration"]["sha"] == git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "HEAD^") == parent
    assert git(repo, "rev-parse", "main") == parent
    assert re.fullmatch(r"cross-harness/\d{8}-\d{6}-implement-the-change", runner._current_branch(repo))
    assert not (run / "worktree").exists()
    assert not (run / "ISOLATED_WORKTREE").exists()
    assert (run / "INTEGRATED").read_text().strip() == summary["integration"]["sha"]
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert summary["cwd"] == str(repo.resolve())
    assert json.loads((run / "state.json").read_text())["cwd"] == str(repo.resolve())
    assert summary["pending"] == []
    assert json.loads((run / "summary.json").read_text())["integration"] == summary["integration"]
    assert "integration: integrated" in (run / "summary.txt").read_text()
    assert "pending: none" in (run / "summary.txt").read_text()
    assert any("commit" in args and timeout == 600 for args, timeout in commands)
    assert any(args[0] == "cherry-pick" and timeout == 600 for args, timeout in commands)
    assert not runner._HELD_ROOT_LOCKS


@pytest.mark.parametrize("branch", ["main", "feature", None])
def test_conflict_rolls_back_branch_head_index_and_worktree(isolated, branch):
    state = isolated
    repo = state["repo"]
    if branch == "feature":
        git(repo, "checkout", "-b", branch)
    elif branch is None:
        git(repo, "checkout", "--detach")

    def change(cwd, run):
        (cwd / "README.md").write_text("unit\n")
        (repo / "README.md").write_text("root\n")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "root writer")
        (repo / "user.txt").write_bytes(b"unrelated modified\x00\n")
        (repo / "mine.txt").write_bytes(b"unrelated untracked\xff\n")
        state["before"] = snapshot(repo)
        state["branches_before"] = git(repo, "branch", "--list")

    state["change"] = change
    summary = delegate(state)
    assert summary["status"] == "partial"
    assert summary["commit"]["status"] == "committed"
    assert summary["integration"]["status"] == "conflict"
    assert summary["integration"]["conflicted_paths"] == ["README.md"]
    assert "cherry-pick exited 1" in summary["error"]
    assert snapshot(repo) == state["before"]
    assert git(repo, "branch", "--list") == state["branches_before"]
    assert not (repo / ".git/CHERRY_PICK_HEAD").exists()
    assert worktree(summary).is_dir()
    assert git(worktree(summary), "status", "--porcelain") == ""
    assert summary["pending"][0]["run_dir"] == summary["run_dir"]
    assert summary["pending"][0]["status"] == "partial"


@pytest.mark.parametrize("changes", ["unrelated", "modified collision", "untracked collision", "staged"])
def test_dirty_root_collision_rule_preserves_user_changes(isolated, changes):
    state = isolated
    repo = state["repo"]
    (repo / "user.txt").write_bytes(b"unstaged\x00\n")
    (repo / "mine.txt").write_bytes(b"untracked\xff\n")
    if changes == "modified collision":
        state["change"] = lambda cwd, run: (cwd / "user.txt").write_text("unit\n")
    elif changes == "untracked collision":
        state["change"] = lambda cwd, run: (cwd / "mine.txt").write_text("unit\n")
    elif changes == "staged":
        git(repo, "add", "user.txt")
        (repo / "user.txt").write_bytes(b"staged and unstaged\x00\n")
    before = snapshot(repo)
    summary = delegate(state)
    assert (repo / "user.txt").read_bytes() == before[3]["user.txt"][1]
    assert (repo / "mine.txt").read_bytes() == before[3]["mine.txt"][1]
    if changes == "unrelated":
        assert summary["status"] == "success"
        assert summary["integration"]["status"] == "integrated"
        assert (repo / "delegated.txt").read_text() == "delegated\n"
    else:
        assert summary["status"] == "partial"
        assert summary["integration"]["status"] == "failed"
        assert ("staged changes" if changes == "staged" else "root path collides with unit") in summary["error"]
        assert snapshot(repo) == before
        assert not (Path(summary["run_dir"]) / "WORK_BRANCH").exists()
        assert worktree(summary).exists()


def test_integration_refuses_to_overwrite_an_ignored_user_file(isolated):
    state = isolated
    repo = state["repo"]
    (repo / ".git/info").mkdir(exist_ok=True)
    (repo / ".git/info/exclude").write_text("delegated.txt\n")
    (repo / "delegated.txt").write_text("ignored user content\n")
    before = snapshot(repo)

    def change(cwd, run):
        (cwd / "delegated.txt").write_text("unit\n")
        git(cwd, "add", "-f", "delegated.txt")

    state["change"] = change
    summary = delegate(state)
    assert summary["commit"]["status"] == "committed"
    assert summary["status"] == "partial"
    assert "ignored root path would be overwritten: delegated.txt" in summary["error"]
    assert snapshot(repo) == before
    assert worktree(summary).exists()


@pytest.mark.parametrize("root_directory", [False, True])
def test_integration_refuses_file_directory_prefix_collisions(isolated, root_directory):
    repo = isolated["repo"]
    if root_directory:
        (repo / "collision").mkdir()
        (repo / "collision/user.txt").write_bytes(b"user\xff\n")
        isolated["change"] = lambda cwd, run: (cwd / "collision").write_text("unit\n")
    else:
        (repo / "collision").write_bytes(b"user\xff\n")

        def change(cwd, run):
            (cwd / "collision").mkdir()
            (cwd / "collision/unit.txt").write_text("unit\n")

        isolated["change"] = change
    before = snapshot(repo)
    summary = delegate(isolated)
    assert summary["status"] == "partial"
    assert "root path collides with unit: collision" in summary["error"]
    assert snapshot(repo) == before
    assert not (Path(summary["run_dir"]) / "WORK_BRANCH").exists()


def test_integration_refuses_unfinished_git_operation_without_mutation(isolated, monkeypatch):
    repo = isolated["repo"]
    marker = repo / ".git/CHERRY_PICK_HEAD"
    before = snapshot(repo)

    original = runner._git

    def root_operation_after_unit_commit(cwd, args, timeout=30):
        result = original(cwd, args, timeout)
        if "commit" in args and result.returncode == 0:
            marker.write_text(before[0] + "\n")
        return result

    monkeypatch.setattr(runner, "_git", root_operation_after_unit_commit)
    summary = delegate(isolated)
    assert summary["status"] == "partial"
    assert summary["commit"]["status"] == "committed"
    assert summary["integration"]["status"] == "failed"
    assert "unfinished Git operation" in summary["error"]
    assert snapshot(repo) == before
    assert marker.read_text().strip() == before[0]
    assert not (Path(summary["run_dir"]) / "WORK_BRANCH").exists()


def test_collision_check_includes_paths_reverted_by_a_later_unit_commit(isolated):
    state = isolated
    repo = state["repo"]
    (repo / "user.txt").write_text("staged\n")
    git(repo, "add", "user.txt")
    state["change"] = lambda cwd, run: (cwd / "README.md").write_text("first unit change\n")
    first = delegate(state)
    assert first["status"] == "partial"
    git(repo, "restore", "--staged", "user.txt")
    (repo / "README.md").write_text("user modified\n")
    state["change"] = lambda cwd, run: (cwd / "README.md").write_text("before\n")
    before = snapshot(repo)
    summary = runner.retry(Path(first["run_dir"]), state["task"], state["config"], state["home"])
    assert summary["status"] == "partial"
    assert len(summary["integration"]["unit_commits"]) == 2
    assert "root path collides with unit: README.md" in summary["error"]
    assert snapshot(repo) == before


@pytest.mark.parametrize("cancelled_staging", [False, True])
def test_empty_isolated_unit_still_removes_its_worktree(isolated, cancelled_staging):
    before = snapshot(isolated["repo"])
    branches = git(isolated["repo"], "branch", "--list")
    def change(cwd, run):
        if cancelled_staging:
            (cwd / "README.md").write_text("staged\n")
            git(cwd, "add", "README.md")
            (cwd / "README.md").write_text("before\n")

    isolated["change"] = change
    summary = delegate(isolated)
    assert summary["status"] == "success"
    assert summary["commit"]["reason"] == "no changes"
    assert summary["integration"]["status"] == "integrated"
    assert not (Path(summary["run_dir"]) / "worktree").exists()
    assert git(isolated["repo"], "rev-list", "--count", "HEAD") == "1"
    assert snapshot(isolated["repo"]) == before
    assert git(isolated["repo"], "branch", "--list") == branches


def test_integration_waits_for_root_lock_only_after_unit_commit(isolated):
    state = isolated
    lock = runner._root_lock_path(runtime(state), state["repo"])
    descriptor = runner._try_lock(lock)
    observed = []
    releasers = []
    real_git = runner._git

    def git_and_release(cwd, args, timeout=30):
        result = real_git(cwd, args, timeout)
        if "commit" in args and cwd != state["repo"]:
            observed.append(git(cwd, "rev-parse", "HEAD"))

            def release_later():
                time.sleep(0.1)
                runner._release_lock(descriptor)

            thread = threading.Thread(target=release_later)
            thread.start()
            releasers.append(thread)
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner, "_git", git_and_release)
        summary = delegate(state)
    for thread in releasers:
        thread.join()
    assert summary["status"] == "success"
    assert observed == [summary["commit"]["sha"]]


def test_root_lock_timeout_keeps_committed_unit_and_root_untouched(isolated, monkeypatch):
    state = isolated
    config = load_config(state["config"], state["home"])
    config["roles"]["implementer"]["timeout_seconds"] = 1
    monkeypatch.setattr(runner, "load_config", lambda *args: config)
    before = snapshot(state["repo"])
    descriptor = runner._try_lock(runner._root_lock_path(runtime(state), state["repo"]))
    try:
        started = time.monotonic()
        summary = delegate(state)
        assert 1 <= time.monotonic() - started < 5
    finally:
        runner._release_lock(descriptor)
    assert summary["status"] == "partial"
    assert summary["commit"]["status"] == "committed"
    assert summary["integration"]["status"] == "pending"
    assert "timed out waiting for root worktree lock" in summary["error"]
    assert snapshot(state["repo"]) == before
    assert worktree(summary).exists()


def test_integration_polls_root_lock_every_half_second(isolated, monkeypatch):
    acquire = runner._acquire_root_lock
    calls = []
    sleeps = []

    def contended(runtime_root, root):
        calls.append(root)
        return False if len(calls) <= 2 else acquire(runtime_root, root)

    monkeypatch.setattr(runner, "_acquire_root_lock", contended)
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    summary = delegate(isolated)
    assert summary["status"] == "success"
    assert sleeps == [0.5, 0.5]
    assert len(calls) == 3


@pytest.mark.parametrize("failure", ["refused", "timeout"])
@pytest.mark.parametrize("dirty", [False, True])
def test_failed_cherry_pick_restores_root(isolated, monkeypatch, failure, dirty):
    if dirty:
        (isolated["repo"] / "user.txt").write_bytes(b"user modified\x00\n")
        (isolated["repo"] / "mine.txt").write_bytes(b"user untracked\xff\n")
    before = snapshot(isolated["repo"])
    branches_before = git(isolated["repo"], "branch", "--list")
    commands = []
    real_git = runner._git

    def fail_pick(cwd, args, timeout=30):
        commands.append(args)
        if args[0] == "cherry-pick" and args[1] != "--abort":
            if failure == "timeout":
                raise subprocess.TimeoutExpired(["git", *args], timeout)
            return subprocess.CompletedProcess(args, 1, "", "hook refused")
        return real_git(cwd, args, timeout)

    monkeypatch.setattr(runner, "_git", fail_pick)
    summary = delegate(isolated)
    assert summary["status"] == "partial"
    assert summary["integration"]["status"] == "failed"
    assert snapshot(isolated["repo"]) == before
    assert git(isolated["repo"], "branch", "--list") == branches_before
    assert ["reset", "--keep", before[0]] in commands
    assert commands.index(["cherry-pick", "--abort"]) < commands.index(["reset", "--keep", before[0]])
    assert not any("--hard" in args for args in commands)
    assert any(args[:2] == ["branch", "--delete"] for args in commands)
    assert not any(args[:2] == ["branch", "-D"] for args in commands)
    assert worktree(summary).exists()


def test_failed_branch_preparation_removes_only_its_created_branch(isolated, monkeypatch):
    repo = isolated["repo"]
    git(repo, "branch", "cross-harness/pre-existing")
    before = snapshot(repo)
    branches_before = git(repo, "branch", "--list")
    real_git = runner._git

    def fail_head_switch(cwd, args, timeout=30):
        if cwd == repo and args[:2] == ["symbolic-ref", "HEAD"] and args[2] != "refs/heads/main":
            return subprocess.CompletedProcess(args, 1, "", "HEAD switch refused")
        return real_git(cwd, args, timeout)

    monkeypatch.setattr(runner, "_git", fail_head_switch)
    summary = delegate(isolated)
    assert summary["status"] == "partial"
    assert "HEAD switch refused" in summary["error"]
    assert snapshot(repo) == before
    assert git(repo, "branch", "--list") == branches_before


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, UnicodeError])
def test_cleanup_removal_errors_do_not_prevent_finalization(isolated, monkeypatch, error_type):
    def fail_removal(*args):
        raise error_type("removal failed")

    monkeypatch.setattr(runner, "_remove_isolated_worktree", fail_removal)
    summary = delegate(isolated)
    run = Path(summary["run_dir"])
    assert summary["status"] == "partial"
    assert summary["integration"]["status"] == "integrated"
    assert "removal failed" in summary["integration"]["reason"]
    assert json.loads((run / "state.json").read_text())["status"] == "partial"
    assert json.loads((run / "summary.json").read_text())["integration"] == summary["integration"]
    assert (run / "summary.txt").is_file()
    assert (isolated["repo"] / "delegated.txt").read_text() == "delegated\n"


@pytest.mark.parametrize("corruption", ["unreadable marker", "undecodable marker", "unreadable summary"])
def test_cleanup_skips_unreadable_other_runs_and_finalizes(isolated, monkeypatch, corruption):
    other = runtime(isolated) / "runs" / "other"
    other.mkdir(parents=True)
    marker = other / "ISOLATED_WORKTREE"
    marker.write_text(str(isolated["repo"] / "other-worktree"))
    (other / "summary.json").write_text(json.dumps({"status": "failed"}))
    (other / "summary.txt").write_text("failed\n")
    real_read = Path.read_text
    real_git = runner._git
    picked = False

    def corrupt_after_pick(cwd, args, timeout=30):
        nonlocal picked
        result = real_git(cwd, args, timeout)
        if args[0] == "cherry-pick" and result.returncode == 0:
            picked = True
            if corruption == "undecodable marker":
                marker.write_bytes(b"\xff")
        return result

    def read(path, *args, **kwargs):
        if picked:
            if path == marker and corruption == "unreadable marker":
                raise OSError("other marker unreadable")
            if path == other / "summary.json" and corruption == "unreadable summary":
                raise OSError("other summary unreadable")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(runner, "_git", corrupt_after_pick)
    monkeypatch.setattr(Path, "read_text", read)
    summary = delegate(isolated)
    run = Path(summary["run_dir"])
    assert summary["integration"]["status"] == "integrated"
    if corruption != "unreadable summary":
        assert str(other) in summary["integration"]["reason"]
    assert (run / "INTEGRATED").exists()
    assert not (run / "worktree").exists()
    assert json.loads((run / "state.json").read_text())["cwd"] == str(isolated["repo"].resolve())
    assert (run / "summary.txt").is_file()


def test_cleanup_summary_rewrite_error_does_not_prevent_finalization(isolated, monkeypatch):
    isolated["status"] = "failed"
    first = delegate(isolated)
    previous = Path(first["run_dir"])
    isolated["status"] = "success"
    real_save = runner._save_summary

    def fail_previous(run, summary, limit=12000):
        if run == previous:
            raise RuntimeError("previous summary rewrite failed")
        return real_save(run, summary, limit)

    monkeypatch.setattr(runner, "_save_summary", fail_previous)
    summary = runner.retry(previous, isolated["task"], isolated["config"], isolated["home"])
    run = Path(summary["run_dir"])
    assert summary["status"] == "partial"
    assert summary["integration"]["status"] == "integrated"
    assert "previous summary rewrite failed" in summary["integration"]["reason"]
    assert (run / "INTEGRATED").exists()
    assert json.loads((run / "summary.json").read_text())["integration"] == summary["integration"]
    assert (run / "state.json").is_file() and (run / "summary.txt").is_file()


def test_cleanup_marker_enumeration_error_does_not_prevent_finalization(isolated, monkeypatch):
    runs = runtime(isolated) / "runs"
    real_git = runner._git
    real_iterdir = Path.iterdir
    picked = False

    def pick(cwd, args, timeout=30):
        nonlocal picked
        result = real_git(cwd, args, timeout)
        if args[0] == "cherry-pick" and result.returncode == 0:
            picked = True
        return result

    def iterdir(path):
        if picked and path == runs:
            raise OSError("run marker enumeration failed")
        return real_iterdir(path)

    monkeypatch.setattr(runner, "_git", pick)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    summary = delegate(isolated)
    run = Path(summary["run_dir"])
    assert summary["status"] == "partial"
    assert summary["integration"]["status"] == "integrated"
    assert "run marker enumeration failed" in summary["integration"]["reason"]
    assert json.loads((run / "summary.json").read_text())["integration"] == summary["integration"]
    assert (run / "state.json").is_file() and (run / "summary.txt").is_file()


def test_adopt_committed_unit_integrates_then_cleans_up(isolated):
    state = isolated
    (state["repo"] / "delegated.txt").write_text("colliding\n")
    summary = delegate(state)
    run = Path(summary["run_dir"])
    unit = worktree(summary)
    (state["repo"] / "delegated.txt").unlink()
    (state["repo"] / "mine.txt").write_bytes(b"unrelated untracked\xff\n")
    (state["repo"] / "user.txt").write_bytes(b"unrelated modified\x00\n")
    adopted = runner.adopt(run, state["config"], state["home"])
    assert adopted["changed_files"] == ["delegated.txt"]
    assert adopted["integration"]["status"] == "integrated"
    assert (state["repo"] / "mine.txt").read_bytes() == b"unrelated untracked\xff\n"
    assert (state["repo"] / "user.txt").read_bytes() == b"unrelated modified\x00\n"
    assert not unit.exists()
    assert (run / "INTEGRATED").exists()
    assert json.loads((run / "summary.json").read_text())["cwd"] == str(state["repo"].resolve())
    assert runner.pending(state["repo"], state["config"], state["home"]) == []


def test_adopt_committed_unit_returns_integration_despite_cleanup_error(isolated, monkeypatch):
    state = isolated
    (state["repo"] / "delegated.txt").write_text("colliding\n")
    first = delegate(state)
    run = Path(first["run_dir"])
    (state["repo"] / "delegated.txt").unlink()

    def fail_removal(*args):
        raise RuntimeError("adopt cleanup failed")

    monkeypatch.setattr(runner, "_remove_isolated_worktree", fail_removal)
    result = runner.adopt(run, state["config"], state["home"])
    assert result["integration"]["status"] == "integrated"
    assert "adopt cleanup failed" in result["integration"]["reason"]
    assert json.loads((run / "summary.json").read_text())["integration"] == result["integration"]
    assert json.loads((run / "state.json").read_text())["cwd"] == str(state["repo"].resolve())


def test_adopt_committed_refuses_held_lock_without_waiting(isolated):
    (isolated["repo"] / "delegated.txt").write_text("colliding\n")
    summary = delegate(isolated)
    (isolated["repo"] / "delegated.txt").unlink()
    before = snapshot(isolated["repo"])
    descriptor = runner._try_lock(runner._root_lock_path(runtime(isolated), isolated["repo"]))
    try:
        with pytest.raises(HarnessError, match="another write delegation"):
            runner.adopt(Path(summary["run_dir"]), isolated["config"], isolated["home"])
    finally:
        runner._release_lock(descriptor)
    assert snapshot(isolated["repo"]) == before
    assert worktree(summary).exists()


def test_adopt_uncommitted_preserves_unrelated_user_index_and_cleans_up(isolated):
    state = isolated
    state["config"].write_text('auto_commit = false\ndirty_worktree_policy = "isolate"\n')
    repo = state["repo"]
    (repo / "user.txt").write_text("staged\n")
    git(repo, "add", "user.txt")
    (repo / "user.txt").write_text("unstaged\n")
    index = (repo / ".git/index").read_bytes()
    summary = delegate(state)
    unit = worktree(summary)
    run = Path(summary["run_dir"])
    result = runner.adopt(run, state["config"], state["home"])
    assert result["changed_files"] == ["delegated.txt"]
    assert (repo / ".git/index").read_bytes() == index
    assert (repo / "user.txt").read_text() == "unstaged\n"
    assert (repo / "delegated.txt").read_text() == "delegated\n"
    assert not unit.exists()
    assert (run / "ADOPTED").exists()
    assert not (run / "ISOLATED_WORKTREE").exists()
    assert json.loads((run / "state.json").read_text())["cwd"] == str(repo.resolve())


def test_retry_integration_resolves_every_shared_run_then_retries_in_root(isolated):
    state = isolated
    state["status"] = "failed"
    first = delegate(state)
    previous = Path(first["run_dir"])
    unit = worktree(first)
    state["status"] = "success"
    state["change"] = lambda cwd, run: (cwd / "second.txt").write_text("second\n")
    second = runner.retry(previous, state["task"], state["config"], state["home"])
    assert second["status"] == "success"
    assert second["commit"]["paths"] == ["delegated.txt", "second.txt"]
    assert not unit.exists()
    assert (previous / "INTEGRATED").exists()
    assert json.loads((previous / "summary.json").read_text())["pending"] == []
    assert runner.pending(state["repo"], state["config"], state["home"]) == []

    def in_root(cwd, run):
        assert cwd == state["repo"].resolve()
        (cwd / "third.txt").write_text("third\n")

    state["change"] = in_root
    third = runner.retry(previous, state["task"], state["config"], state["home"])
    assert third["status"] == "success"
    assert third["cwd"] == str(state["repo"].resolve())
    assert not (Path(third["run_dir"]) / "ISOLATED_WORKTREE").exists()


@pytest.mark.parametrize("new_changes", [False, True])
def test_retry_retains_committed_unit_after_a_failed_integration(isolated, new_changes):
    state = isolated
    (state["repo"] / "delegated.txt").write_text("colliding\n")
    first = delegate(state)
    assert first["status"] == "partial"
    (state["repo"] / "delegated.txt").unlink()
    state["change"] = (
        (lambda cwd, run: (cwd / "second.txt").write_text("second\n"))
        if new_changes else (lambda cwd, run: None)
    )
    second = runner.retry(Path(first["run_dir"]), state["task"], state["config"], state["home"])
    assert second["status"] == "success"
    assert (state["repo"] / "delegated.txt").read_text() == "delegated\n"
    assert len(second["integration"]["unit_commits"]) == (2 if new_changes else 1)
    assert git(state["repo"], "rev-list", "--count", "HEAD") == ("3" if new_changes else "2")
    assert not (Path(first["run_dir"]) / "ISOLATED_WORKTREE").exists()


def test_reply_of_integrated_discussion_runs_in_root(isolated):
    state = isolated
    state["status"] = "discussion"
    discussion = delegate(state)
    previous = Path(discussion["run_dir"])
    state["status"] = "success"
    integrated = runner.reply(previous, state["task"], state["config"], state["home"])
    assert integrated["status"] == "success"
    assert (previous / "INTEGRATED").exists()

    def in_root(cwd, run):
        assert cwd == state["repo"].resolve()
        (cwd / "reply.txt").write_text("reply\n")

    state["change"] = in_root
    replied = runner.reply(previous, state["task"], state["config"], state["home"])
    assert replied["status"] == "success"
    assert replied["cwd"] == str(state["repo"].resolve())


def test_retry_of_adopted_run_runs_in_root(isolated):
    state = isolated
    state["status"] = "partial"
    first = delegate(state)
    run = Path(first["run_dir"])
    runner.adopt(run, state["config"], state["home"])
    state["status"] = "success"

    def in_root(cwd, retry_run):
        assert cwd == state["repo"].resolve()
        (cwd / "second.txt").write_text("second\n")

    state["change"] = in_root
    result = runner.retry(run, state["task"], state["config"], state["home"])
    assert result["status"] == "success"
    assert (run / "ADOPTED").exists()
    assert result["commit"]["paths"] == ["delegated.txt", "second.txt"]


def test_retry_and_reply_use_primary_project_settings(isolated):
    state = isolated
    state["config"].write_text(
        'auto_commit = false\nprotected_branches = []\ndirty_worktree_policy = "allow"\n'
        f'[projects.{json.dumps(str(state["repo"].resolve()))}]\n'
        'auto_commit = true\nprotected_branches = ["main"]\ndirty_worktree_policy = "isolate"\n'
    )
    state["status"] = "discussion"
    first = delegate(state)
    state["status"] = "failed"
    replied = runner.reply(Path(first["run_dir"]), state["task"], state["config"], state["home"])
    settings = json.loads((Path(replied["run_dir"]) / "auto-commit.json").read_text())
    assert settings["enabled"] is True
    assert settings["protected_branches"] == ["main"]
    assert settings["dirty_worktree_policy"] == "isolate"
    state["status"] = "success"
    result = runner.retry(Path(replied["run_dir"]), state["task"], state["config"], state["home"])
    assert result["status"] == "success"
    assert result["integration"]["branch"].startswith("cross-harness/")
    assert git(state["repo"], "rev-list", "--count", "main") == "1"


def test_discard_removes_worktree_and_marks_shared_chain(isolated):
    state = isolated
    state["status"] = "failed"
    first = delegate(state)
    second = runner.retry(Path(first["run_dir"]), state["task"], state["config"], state["home"])
    unit = worktree(second)
    before = snapshot(state["repo"])
    assert main(["--home", str(state["home"]), "discard", "--run", second["run_dir"], "--config", str(state["config"])]) == 0
    assert snapshot(state["repo"]) == before
    assert not unit.exists()
    for summary in (first, second):
        run = Path(summary["run_dir"])
        assert (run / "DISCARDED").exists()
        assert not (run / "ISOLATED_WORKTREE").exists()
    assert runner.pending(state["repo"], state["config"], state["home"]) == []


@pytest.mark.parametrize("live", ["self", "shared"])
def test_discard_refuses_live_run_or_live_shared_worktree(isolated, monkeypatch, live):
    isolated["status"] = "failed"
    summary = delegate(isolated)
    run = Path(summary["run_dir"])
    unit = worktree(summary)
    other = run.parent / "other"
    other.mkdir()
    (other / "ISOLATED_WORKTREE").write_text(str(unit))
    monkeypatch.setattr(runner, "_supervisor_alive", lambda candidate: candidate == (run if live == "self" else other))
    with pytest.raises(HarnessError, match="in progress|another live run"):
        runner.discard(run, isolated["config"], isolated["home"])
    assert unit.exists()


def test_discard_rejects_root_run_and_unsafe_marker(execution):
    execution["status"] = "partial"
    summary = delegate(execution)
    run = Path(summary["run_dir"])
    with pytest.raises(HarnessError, match="not isolated"):
        runner.discard(run, execution["config"], execution["home"])
    (run / "ISOLATED_WORKTREE").write_text(str(execution["repo"]))
    with pytest.raises(HarnessError, match="unsafe isolated worktree"):
        runner.discard(run, execution["config"], execution["home"])
    assert execution["repo"].exists()


def test_commit_partial_run_keeps_staged_user_changes(execution):
    state = execution
    state["config"].write_text('dirty_worktree_policy = "allow"\n')
    state["status"] = "partial"
    repo = state["repo"]
    (repo / "user.txt").write_text("staged\n")
    git(repo, "add", "user.txt")
    (repo / "user.txt").write_text("unstaged\n")
    first = delegate(state)
    run = Path(first["run_dir"])
    assert main(["--home", str(state["home"]), "commit", "--run", str(run), "--config", str(state["config"])]) == 0
    result = json.loads((run / "summary.json").read_text())
    assert result["status"] == "partial"
    assert result["commit"]["status"] == "committed"
    assert result["commit"]["paths"] == ["delegated.txt"]
    assert git(repo, "show", ":user.txt") == "staged"
    assert (repo / "user.txt").read_text() == "unstaged\n"
    assert json.loads((run / "summary.json").read_text())["commit"] == result["commit"]
    with pytest.raises(HarnessError, match="skipped or failed commit"):
        runner.commit_run(run, state["config"], state["home"])


def test_commit_rechecks_protection_and_retries_failed_hook(execution):
    state = execution
    repo = state["repo"]
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    first = delegate(state)
    assert first["commit"]["status"] == "failed"
    hook.unlink()
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    result = runner.commit_run(Path(first["run_dir"]), state["config"], state["home"])
    assert result["commit"]["status"] == "committed"
    assert result["commit"]["branch"].startswith("cross-harness/")
    assert git(repo, "rev-list", "--count", "main") == "1"


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_commit_refuses_changed_fingerprint_without_mutation(execution, change):
    execution["status"] = "partial"
    summary = delegate(execution)
    path = execution["repo"] / "delegated.txt"
    if change == "edit":
        path.write_text("user changed\n")
    else:
        path.unlink()
    before = snapshot(execution["repo"])
    with pytest.raises(HarnessError, match="fingerprint changed: delegated.txt"):
        runner.commit_run(Path(summary["run_dir"]), execution["config"], execution["home"])
    assert snapshot(execution["repo"]) == before


def test_commit_refuses_held_root_lock(execution):
    execution["status"] = "partial"
    summary = delegate(execution)
    descriptor = runner._try_lock(runner._root_lock_path(runtime(execution), execution["repo"]))
    try:
        with pytest.raises(HarnessError, match="another write delegation"):
            runner.commit_run(Path(summary["run_dir"]), execution["config"], execution["home"])
    finally:
        runner._release_lock(descriptor)
    assert (execution["repo"] / "delegated.txt").exists()


@pytest.mark.parametrize("invalid", ["success", "failed", "read-only", "unfinished", "disabled"])
def test_commit_requires_a_finalized_partial_write_run(execution, invalid):
    state = execution
    state["status"] = "partial"
    summary = delegate(state)
    run = Path(summary["run_dir"])
    if invalid == "unfinished":
        (run / "summary.txt").unlink()
    elif invalid == "read-only":
        record = json.loads((run / "state.json").read_text())
        record["role"] = "tester"
        (run / "state.json").write_text(json.dumps(record))
    else:
        if invalid == "disabled":
            summary["commit"]["status"] = "disabled"
        else:
            summary["status"] = invalid
        (run / "summary.json").write_text(json.dumps(summary))
    before = snapshot(state["repo"])
    with pytest.raises(HarnessError, match="finalized partial write run"):
        runner.commit_run(run, state["config"], state["home"])
    assert snapshot(state["repo"]) == before


@pytest.mark.parametrize("replace_deleted_path", [False, True])
def test_commit_validates_recorded_deletions(execution, replace_deleted_path):
    state = execution
    state["status"] = "partial"
    state["change"] = lambda cwd, run: (cwd / "README.md").unlink()
    summary = delegate(state)
    run = Path(summary["run_dir"])
    if replace_deleted_path:
        (state["repo"] / "README.md").symlink_to("missing-file")
        before = snapshot(state["repo"])
        with pytest.raises(HarnessError, match="fingerprint changed: README.md"):
            runner.commit_run(run, state["config"], state["home"])
        assert snapshot(state["repo"]) == before
    else:
        result = runner.commit_run(run, state["config"], state["home"])
        assert result["commit"]["paths"] == ["README.md"]
        assert git(state["repo"], "status", "--porcelain") == ""


def test_pending_lists_only_finished_units_of_the_repository(isolated, capsys):
    state = isolated
    state["status"] = "failed"
    first = delegate(state)
    unfinished = runtime(state) / "runs" / "unfinished"
    unfinished.mkdir()
    (unfinished / "ISOLATED_WORKTREE").write_text(str(worktree(first)))
    second = delegate(state)
    assert {item["run_dir"] for item in second["pending"]} == {first["run_dir"], second["run_dir"]}
    result = runner.pending(state["repo"], state["config"], state["home"])
    assert {item["run_dir"] for item in result} == {first["run_dir"], second["run_dir"]}
    assert main(["--home", str(state["home"]), "pending", "--cwd", str(state["repo"]), "--config", str(state["config"])]) == 0
    output = capsys.readouterr().out
    assert first["run_dir"] in output and second["run_dir"] in output
    assert "unfinished" not in output


def test_work_branch_collision_uses_numeric_suffix(execution, monkeypatch):
    fixed = datetime(2026, 10, 9, 12, 34, 56)

    class LocalClock:
        @staticmethod
        def now(*args):
            return fixed

    monkeypatch.setattr(runner, "datetime", LocalClock)
    git(execution["repo"], "branch", "cross-harness/20261009-123456-implement-the-change")
    summary = delegate(execution)
    assert summary["commit"]["branch"] == "cross-harness/20261009-123456-implement-the-change-1"


@pytest.fixture
def linked(execution):
    primary = execution["repo"]
    launch = primary.parent / "linked"
    git(primary, "worktree", "add", "-b", "launch", str(launch))
    (primary / "user.txt").write_text("staged in primary\n")
    git(primary, "add", "user.txt")
    (primary / "user.txt").write_bytes(b"primary unstaged\x00\n")
    (primary / "mine.txt").write_bytes(b"primary untracked\xff\n")
    execution["primary"] = primary
    execution["primary_snapshot"] = snapshot(primary)
    execution["repo"] = launch
    execution["config"].write_text(
        'auto_commit = false\nprotected_branches = []\n'
        f'[projects.{json.dumps(str(launch.resolve()))}]\n'
        'auto_commit = true\nprotected_branches = ["launch"]\n'
    )
    return execution


def test_linked_root_writer_uses_launch_branch_settings_and_lock(linked):
    primary_lock = runner._try_lock(runner._root_lock_path(runtime(linked), linked["primary"]))

    def change(cwd, run):
        assert cwd == linked["repo"].resolve()
        assert runner._root_lock_path(runtime(linked), cwd) in runner._HELD_ROOT_LOCKS
        (cwd / "delegated.txt").write_text("linked unit\n")

    linked["change"] = change
    try:
        summary = delegate(linked)
    finally:
        runner._release_lock(primary_lock)
    assert summary["status"] == "success"
    assert summary["commit"]["sha"] == git(linked["repo"], "rev-parse", "HEAD")
    assert summary["commit"]["branch"].startswith("cross-harness/")
    assert git(linked["repo"], "rev-list", "--count", "launch") == "1"
    assert snapshot(linked["primary"]) == linked["primary_snapshot"]
    run = Path(summary["run_dir"])
    assert (run / "ROOT_WORKTREE").read_text().strip() == str(linked["repo"].resolve())


@pytest.mark.parametrize("resume", ["retry", "reply"])
def test_linked_isolated_integration_and_resume_stay_in_launch_worktree(linked, resume):
    with linked["config"].open("a") as config:
        config.write('dirty_worktree_policy = "isolate"\n')
    linked["status"] = "discussion" if resume == "reply" else "failed"
    first = delegate(linked)
    previous = Path(first["run_dir"])
    assert runner.pending(linked["primary"], linked["config"], linked["home"]) == []
    assert len(runner.pending(linked["repo"], linked["config"], linked["home"])) == 1
    linked["status"] = "success"
    second = getattr(runner, resume)(previous, linked["task"], linked["config"], linked["home"])
    assert second["status"] == "success"
    assert second["integration"]["root"] == str(linked["repo"].resolve())
    assert snapshot(linked["primary"]) == linked["primary_snapshot"]

    def root_change(cwd, run):
        assert cwd == linked["repo"].resolve()
        assert runner._root_lock_path(runtime(linked), cwd) in runner._HELD_ROOT_LOCKS
        (cwd / "next.txt").write_text("next\n")

    linked["change"] = root_change
    third = getattr(runner, resume)(previous, linked["task"], linked["config"], linked["home"])
    assert third["status"] == "success"
    assert third["cwd"] == str(linked["repo"].resolve())
    assert snapshot(linked["primary"]) == linked["primary_snapshot"]
    assert not runner._HELD_ROOT_LOCKS


@pytest.mark.parametrize("action", ["committed adopt", "uncommitted adopt", "discard", "commit"])
def test_linked_resolution_and_pending_use_recorded_root(linked, action):
    if action == "uncommitted adopt":
        linked["config"].write_text(linked["config"].read_text().replace('auto_commit = true', 'auto_commit = false'))
    with linked["config"].open("a") as config:
        config.write('dirty_worktree_policy = "isolate"\n')
    if action == "committed adopt":
        (linked["repo"] / "delegated.txt").write_text("collision\n")
    elif action in {"discard", "commit"}:
        linked["status"] = "failed" if action == "discard" else "partial"
    summary = delegate(linked)
    run = Path(summary["run_dir"])
    unit = worktree(summary)
    assert runner.pending(linked["primary"], linked["config"], linked["home"]) == []
    assert [item["run_dir"] for item in runner.pending(linked["repo"], linked["config"], linked["home"])] == [str(run)]
    assert [item["run_dir"] for item in runner.pending(unit, linked["config"], linked["home"])] == [str(run)]
    primary_lock = runner._try_lock(runner._root_lock_path(runtime(linked), linked["primary"]))
    try:
        if action == "committed adopt":
            (linked["repo"] / "delegated.txt").unlink()
            launch_lock = runner._try_lock(runner._root_lock_path(runtime(linked), linked["repo"]))
            try:
                with pytest.raises(HarnessError, match="another write delegation"):
                    runner.adopt(run, linked["config"], linked["home"])
            finally:
                runner._release_lock(launch_lock)
        command = runner.discard if action == "discard" else runner.commit_run if action == "commit" else runner.adopt
        command(run, linked["config"], linked["home"])
    finally:
        runner._release_lock(primary_lock)
    assert not unit.exists()
    assert snapshot(linked["primary"]) == linked["primary_snapshot"]
    assert json.loads((run / "state.json").read_text())["cwd"] == str(linked["repo"].resolve())
    assert runner.pending(linked["repo"], linked["config"], linked["home"]) == []


def test_resolution_never_prunes_unrelated_worktree_metadata(isolated, monkeypatch):
    missing = isolated["repo"].parent / "missing"
    git(isolated["repo"], "worktree", "add", "--detach", str(missing))
    import shutil
    shutil.rmtree(missing)
    original = runner._git
    commands = []

    def tracked(cwd, args, timeout=30):
        commands.append(args)
        return original(cwd, args, timeout)

    monkeypatch.setattr(runner, "_git", tracked)
    summary = delegate(isolated)
    assert summary["integration"]["status"] == "integrated"
    assert str(missing) in git(isolated["repo"], "worktree", "list", "--porcelain")
    for action in ("adopt", "discard"):
        isolated["status"] = "partial" if action == "adopt" else "failed"
        pending_summary = delegate(isolated)
        getattr(runner, action)(Path(pending_summary["run_dir"]), isolated["config"], isolated["home"])
    runner.pending(isolated["repo"], isolated["config"], isolated["home"])
    assert not any(args[:2] == ["worktree", "prune"] for args in commands)
    assert str(missing) in git(isolated["repo"], "worktree", "list", "--porcelain")


@pytest.mark.parametrize("failure", ["delegated cleanup", "pending collection", "rendering"])
def test_root_commit_finalizes_despite_later_errors(execution, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise RuntimeError(failure)

    target = {"delegated cleanup": "_forget_committed_paths", "pending collection": "_run_pending",
              "rendering": "render_summary"}[failure]
    monkeypatch.setattr(runner, target, fail)
    summary = delegate(execution)
    run = Path(summary["run_dir"])
    assert summary["commit"]["status"] == "committed"
    assert summary["commit"]["sha"] == git(execution["repo"], "rev-parse", "HEAD")
    assert summary["status"] == "partial"
    assert failure in " ".join(summary["cleanup_errors"])
    assert json.loads((run / "summary.json").read_text())["commit"] == summary["commit"]
    assert json.loads((run / "state.json").read_text())["status"] == "partial"
    assert (run / "summary.txt").is_file()


def test_baseline_failure_after_branch_creation_is_finalized(execution, monkeypatch):
    monkeypatch.setattr(runner, "_write_baseline", lambda *args: (_ for _ in ()).throw(OSError("baseline failed")))
    with pytest.raises(OSError, match="baseline failed"):
        delegate(execution)
    assert runner._current_branch(execution["repo"]).startswith("cross-harness/")
    run = next((runtime(execution) / "runs").iterdir())
    summary = json.loads((run / "summary.json").read_text())
    state = json.loads((run / "state.json").read_text())
    assert summary["status"] == state["status"] == "blocked"
    assert state["blocked_category"] == "work_branch"
    assert execution["calls"] == []
    assert not runner._HELD_ROOT_LOCKS


def test_failed_keep_reset_reports_rollback_failure_and_keeps_unit(isolated, monkeypatch):
    original = runner._git
    commands = []

    def fail(cwd, args, timeout=30):
        commands.append(args)
        if args[0] == "cherry-pick" or args[:2] == ["reset", "--keep"]:
            return subprocess.CompletedProcess(args, 1, "", "rollback refused")
        return original(cwd, args, timeout)

    monkeypatch.setattr(runner, "_git", fail)
    summary = delegate(isolated)
    assert summary["status"] == "partial"
    assert "integration rollback failed" in summary["integration"]["reason"]
    assert worktree(summary).exists()
    assert not any("--hard" in args for args in commands)


@pytest.mark.parametrize("touch_leftover", [False, True])
def test_fresh_run_excludes_other_unit_leftovers_and_retry_carries_own_paths(execution, touch_leftover):
    execution["status"] = "partial"
    earlier = delegate(execution)
    execution["status"] = "success"

    def change(cwd, run):
        if touch_leftover:
            (cwd / "delegated.txt").write_text("changed leftover\n")
        (cwd / "own.txt").write_text("own unit\n")

    execution["change"] = change
    fresh = delegate(execution)
    assert fresh["commit"]["paths"] == ["own.txt"]
    assert fresh["commit"]["excluded_paths"] == ["delegated.txt"]
    assert git(execution["repo"], "ls-tree", "--name-only", "HEAD", "delegated.txt") == ""
    assert (execution["repo"] / "delegated.txt").read_text() == ("changed leftover\n" if touch_leftover else "delegated\n")
    run = Path(fresh["run_dir"])
    assert json.loads((run / "chain-paths.json").read_text()) == ["own.txt"]
    # The earlier chain can commit its path only while its recorded fingerprint is intact.
    if not touch_leftover:
        execution["change"] = lambda cwd, run: (cwd / "retry.txt").write_text("retry\n")
        retried = runner.retry(Path(earlier["run_dir"]), execution["task"], execution["config"], execution["home"])
        assert retried["commit"]["paths"] == ["delegated.txt", "retry.txt"]


@pytest.mark.parametrize("operation", ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_START"])
@pytest.mark.parametrize("action", ["delegate", "isolated delegate", "commit", "isolated commit"])
def test_unfinished_operations_refuse_before_branch_or_commit(execution, operation, action):
    isolated = action.startswith("isolated")
    if isolated:
        execution["config"].write_text('auto_commit = true\ndirty_worktree_policy = "isolate"\n')
    run = None
    if "commit" in action:
        execution["status"] = "partial"
        run = Path(delegate(execution)["run_dir"])
        git(execution["repo"], "symbolic-ref", "HEAD", "refs/heads/main")
    location = Path(git(execution["repo"], "rev-parse", "--git-path", operation))
    if not location.is_absolute():
        location = execution["repo"] / location
    if operation.startswith("rebase-"):
        location.mkdir()
    else:
        location.write_text(git(execution["repo"], "rev-parse", "HEAD") + "\n")
    before = snapshot(execution["repo"])
    branches = git(execution["repo"], "branch", "--list")
    calls = len(execution["calls"])
    with pytest.raises(HarnessError, match="unfinished Git operation"):
        if "commit" in action:
            runner.commit_run(run, execution["config"], execution["home"])
        else:
            delegate(execution)
    assert snapshot(execution["repo"]) == before
    assert git(execution["repo"], "branch", "--list") == branches
    assert len(execution["calls"]) == calls
    if "delegate" in action:
        run = next((runtime(execution) / "runs").iterdir())
        assert json.loads((run / "state.json").read_text())["blocked_category"] == "work_branch"
        assert json.loads((run / "summary.json").read_text())["status"] == "blocked"


@pytest.mark.parametrize("fingerprint_changed", [False, True])
def test_commit_partial_isolated_unit_verifies_and_integrates(isolated, fingerprint_changed):
    isolated["status"] = "partial"
    summary = delegate(isolated)
    run = Path(summary["run_dir"])
    unit = worktree(summary)
    before = snapshot(isolated["repo"])
    if fingerprint_changed:
        (unit / "delegated.txt").write_text("edited afterwards\n")
        with pytest.raises(HarnessError, match="fingerprint changed"):
            runner.commit_run(run, isolated["config"], isolated["home"])
        assert snapshot(isolated["repo"]) == before
        assert unit.exists()
        assert git(unit, "rev-list", "--count", "HEAD") == "1"
    else:
        result = runner.commit_run(run, isolated["config"], isolated["home"])
        assert result["commit"]["status"] == "committed"
        assert result["integration"]["status"] == "integrated"
        assert not unit.exists()
        assert result["cwd"] == str(isolated["repo"].resolve())
        assert json.loads((run / "state.json").read_text())["cwd"] == result["cwd"]
        assert (isolated["repo"] / "delegated.txt").read_text() == "delegated\n"


@pytest.mark.parametrize("action", ["adopt", "discard"])
@pytest.mark.parametrize("status", ["partial", "discussion"])
def test_already_integrated_resolution_only_finishes_cleanup(isolated, monkeypatch, action, status):
    real_remove = runner._remove_isolated_worktree
    monkeypatch.setattr(runner, "_remove_isolated_worktree", lambda *args: (_ for _ in ()).throw(OSError("remove failed")))
    summary = delegate(isolated)
    run = Path(summary["run_dir"])
    unit = worktree(summary)
    assert summary["integration"]["status"] == "integrated"
    if status == "discussion":
        for name in ("state.json", "summary.json"):
            path = run / name
            record = json.loads(path.read_text())
            record["status"] = "discussion"
            path.write_text(json.dumps(record))
    before = snapshot(isolated["repo"])
    monkeypatch.setattr(runner, "_remove_isolated_worktree", real_remove)
    if action == "discard":
        assert main(["--home", str(isolated["home"]), "discard", "--run", str(run),
                     "--config", str(isolated["config"])]) == 0
    result = getattr(runner, action)(run, isolated["config"], isolated["home"])
    assert result["integration"]["status"] == "integrated"
    assert snapshot(isolated["repo"]) == before
    assert not unit.exists()
    assert not (run / "DISCARDED").exists()
    assert (run / "INTEGRATED").exists()
    assert json.loads((run / "summary.json").read_text())["integration"]["status"] == "integrated"
    assert getattr(runner, action)(run, isolated["config"], isolated["home"])["integration"]["status"] == "integrated"


def test_failed_isolated_unit_cannot_be_committed(isolated):
    isolated["status"] = "failed"
    summary = delegate(isolated)
    unit = worktree(summary)
    before = snapshot(isolated["repo"])
    with pytest.raises(HarnessError, match="finalized partial write run"):
        runner.commit_run(Path(summary["run_dir"]), isolated["config"], isolated["home"])
    assert unit.exists()
    assert git(unit, "rev-list", "--count", "HEAD") == "1"
    assert snapshot(isolated["repo"]) == before


def test_failed_uncommitted_isolated_unit_requires_retry_or_discard(isolated):
    isolated["status"] = "failed"
    summary = delegate(isolated)
    before = snapshot(isolated["repo"])
    with pytest.raises(HarnessError, match="requires retry or discard"):
        runner.adopt(Path(summary["run_dir"]), isolated["config"], isolated["home"])
    assert snapshot(isolated["repo"]) == before
    assert worktree(summary).exists()


@pytest.mark.parametrize("resume", ["retry", "reply"])
def test_allow_resume_excludes_user_edits_to_earlier_chain_paths(execution, resume):
    execution["config"].write_text('auto_commit = true\ndirty_worktree_policy = "allow"\n')
    execution["status"] = "discussion" if resume == "reply" else "partial"
    earlier = delegate(execution)
    (execution["repo"] / "delegated.txt").write_bytes(b"user edit\x00\n")
    execution["status"] = "success"
    execution["change"] = lambda cwd, run: (cwd / "own.txt").write_text("new unit\n")
    result = getattr(runner, resume)(Path(earlier["run_dir"]), execution["task"], execution["config"], execution["home"])
    assert result["commit"]["paths"] == ["own.txt"]
    assert result["commit"]["excluded_paths"] == ["delegated.txt"]
    assert git(execution["repo"], "ls-tree", "--name-only", "HEAD", "delegated.txt") == ""
    assert (execution["repo"] / "delegated.txt").read_bytes() == b"user edit\x00\n"


@pytest.mark.parametrize("failure", ["pending", "summary rewriting"])
def test_explicit_isolated_commit_finalizes_after_cleanup_errors(isolated, monkeypatch, failure):
    isolated["status"] = "partial"
    summary = delegate(isolated)
    run = Path(summary["run_dir"])
    target = "_pending_isolated_runs" if failure == "pending" else "_save_summary"
    monkeypatch.setattr(runner, target, lambda *args: (_ for _ in ()).throw(RuntimeError(failure)))
    result = runner.commit_run(run, isolated["config"], isolated["home"])
    assert result["commit"]["status"] == "committed"
    assert result["integration"]["status"] == "integrated"
    assert failure in result["integration"]["reason"]
    assert json.loads((run / "summary.json").read_text())["integration"] == result["integration"]
    assert json.loads((run / "state.json").read_text())["cwd"] == str(isolated["repo"].resolve())
    assert (run / "summary.txt").is_file()
