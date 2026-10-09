from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import fcntl
import json
import os
import subprocess

import pytest

from cross_harness import runner
from cross_harness.cli import main
from cross_harness.config import load_config
from cross_harness.errors import AuthError, HarnessError
from cross_harness.hooks import claude_session_start
from cross_harness.maintenance import cleanup
from cross_harness.summarize import parse_events, render_summary


PAST = "2000-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"
THREAD = "00000000-0000-0000-0000-000000000001"


def read_state(run):
    return json.loads((run / "state.json").read_text())


def limit_event(reset):
    info = {"status": "rejected", "overageStatus": "unavailable"}
    if reset:
        info["resetsAt"] = datetime.fromisoformat(reset).timestamp()
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info}) + "\n"


@pytest.fixture
def case(tmp_path):
    root = tmp_path.resolve()
    home, repo, runtime = (root / name for name in ("home", "repo", "runtime"))
    repo.mkdir()
    for args in (
        ("init", "-q"), ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.com"),
        ("commit", "--allow-empty", "-m", "initial"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    config_path = home / ".config/cross-harness/config.toml"
    config_path.parent.mkdir(parents=True)

    def configure(extra="", projects=""):
        config_path.write_text(
            f'runtime_root = "{runtime}"\nauto_commit = false\nproject_auto_setup = false\n'
            + extra + '\n[roles.reviewer]\nharness = "codex"\n' + projects
        )
        return load_config(home=home)

    configure()
    task = root / "continue.md"
    task.write_text("# Goal\nContinue the remaining unit.\n\n# Checks\n- fixture\n")

    def make_run(name, reset=PAST, count=0, attempts=1, thread=THREAD, isolated=False):
        run = runtime / "runs" / name
        run.mkdir(parents=True)
        (run / "task.md").write_text("# Goal\nOriginal unit.\n\n# Checks\n- fixture\n")
        config = load_config(home=home)
        role_name = "implementer" if isolated else "reviewer"
        role = config["roles"][role_name]
        runner._prepare_commit_settings(config, role_name, repo, run)
        cwd = runner._create_isolated_worktree(repo, run) if isolated else repo
        runner._write_baseline(run, cwd)
        (run / "revival.json").write_text(json.dumps({"consecutive_revivals": count}))
        events = limit_event(reset)
        if thread:
            events = json.dumps({"type": "thread.started", "thread_id": thread}) + "\n" + events
        (run / "events.jsonl").write_text(events)
        (run / "stderr.log").write_text("")
        summary = runner.finalize_run(
            run, role_name, role, "implementation" if isolated else "review", cwd, 1, attempts,
            runtime_root=runtime, dirty_worktree_policy="allow_delegated",
        )
        return run, summary

    def outcome(status="success", error=None):
        def invoke(command, prompt, env, cwd, run, timeout):
            events = limit_event(PAST) if status == "blocked" and error == "usage limit" else ""
            if status == "failed":
                events += json.dumps({"type": "turn.failed", "error": {"message": error}}) + "\n"
            events += json.dumps({"type": "item.completed", "item": {
                "type": "command_execution", "command": "fixture", "exit_code": 0,
            }}) + "\n"
            (run / "events.jsonl").write_text(events)
            (run / "stderr.log").write_text("")
            (run / "final.json").write_text(json.dumps({
                "status": status, "work_completed": "continued", "changed_files": [],
                "tests": [], "error": error, "next_decision": None,
                "discussion_points": ["Need a decision"] if status == "discussion" else [],
            }))
            return 0 if status in {"success", "partial", "discussion"} else 1
        return invoke

    with ExitStack() as stack:
        for name in ("codex", "claude"):
            stack.enter_context(patch(f"cross_harness.runner.verify_{name}_config_ownership"))
        stack.enter_context(patch("cross_harness.runner.verify_codex_chatgpt", return_value=(Path("/usr/bin/true"), False)))
        stack.enter_context(patch("cross_harness.runner.verify_claude_subscription", return_value=(Path("/usr/bin/true"), False)))
        invoke = stack.enter_context(patch("cross_harness.runner._invoke_safe", side_effect=outcome()))
        yield SimpleNamespace(
            root=root, home=home, repo=repo, runtime=runtime, config_path=config_path,
            task=task, configure=configure, make_run=make_run, outcome=outcome, invoke=invoke,
        )


@pytest.mark.parametrize("reset", [PAST, None])
def test_finalize_persists_reset_and_revival_in_both_artifacts(case, reset):
    run, summary = case.make_run("limited", reset=reset)
    expected = datetime.fromisoformat(reset).astimezone().isoformat() if reset else None
    assert summary["rate_limit_resets_at"] == read_state(run)["rate_limit_resets_at"] == expected
    assert summary["consecutive_revivals"] == read_state(run)["consecutive_revivals"] == 0
    text = (run / "summary.txt").read_text()
    assert f"rate_limit_resets_at: {expected or 'unknown'}" in text
    assert f"revival: {'retry after ' + expected if expected else 'reset time unknown'}" in text
    assert "rate_limit_resets_at:" in render_summary(summary, 100)
    assert "revival:" in render_summary(summary, 100)


def test_codex_reset_is_persisted_and_preexecutor_limit_records_unknown(case):
    run, summary = case.make_run("codex")
    (run / "events.jsonl").write_text(json.dumps({
        "type": "turn.failed", "error": {"message": "Usage limit reached. Try again at Jan 1st, 2099 9:00 AM."},
    }) + "\n")
    role = load_config(home=case.home)["roles"]["reviewer"]
    summary = runner.finalize_run(run, "reviewer", role, "review", case.repo, 1, 1)
    assert datetime.fromisoformat(summary["rate_limit_resets_at"]).year == 2099
    assert summary["rate_limit_resets_at"] == read_state(run)["rate_limit_resets_at"]
    summary = runner.finalize_blocked_run(run, "reviewer", role, "review", case.repo, "limit", "rate_limit")
    assert summary["rate_limit_resets_at"] is None
    assert read_state(run)["rate_limit_resets_at"] is None
    assert "revival: reset time unknown" in (run / "summary.txt").read_text()


def test_summary_uses_effective_project_setting_from_custom_config(case):
    case.configure(projects=f'\n[projects."{case.repo}"]\nauto_revival = false\n')
    run, summary = case.make_run("disabled")
    assert summary["auto_revival"] is read_state(run)["auto_revival"] is False
    assert "revival: disabled by configuration" in (run / "summary.txt").read_text()


@pytest.mark.parametrize("reset,extra,count,reason", [
    (FUTURE, "", 0, "reset time has not passed"),
    (None, "", 0, "reset time unknown"),
    (PAST, "auto_revival = false\n", 0, "disabled by configuration"),
    (PAST, "", 2, "stopped after two consecutive revivals"),
])
def test_retry_refuses_ineligible_limits_and_names_reset(case, reset, extra, count, reason):
    case.configure(extra)
    run, summary = case.make_run("waiting", reset=reset, count=count)
    with pytest.raises(HarnessError, match=reason) as error:
        runner.retry(run, case.task, home=case.home)
    assert f"rate_limit_resets_at: {summary['rate_limit_resets_at'] or 'unknown'}" in str(error.value)
    case.invoke.assert_not_called()
    assert not read_state(run).get("revived")


def test_retry_resolves_current_project_override_against_recorded_root(case):
    run, summary = case.make_run("isolated", isolated=True)
    case.configure(projects=f'\n[projects."{case.repo}"]\nauto_revival = false\n')
    with pytest.raises(HarnessError, match="disabled by configuration"):
        runner.retry(run, case.task, home=case.home)
    case.invoke.assert_not_called()


def test_project_can_enable_revival_over_global_disable_for_isolated_run(case):
    case.configure("auto_revival = false\n", f'\n[projects."{case.repo}"]\nauto_revival = true\n')
    run, summary = case.make_run("isolated", isolated=True)
    assert summary["auto_revival"] is True
    assert runner.revival(case.repo, home=case.home)[0]["eligibility"] == "eligible"
    continued = runner.retry(run, case.task, home=case.home)
    assert continued["status"] == "success"
    assert case.invoke.call_count == 1


def test_reset_boundary_uses_timezone_offsets_and_rechecks_configuration(case):
    run, summary = case.make_run("limited", reset=FUTURE)
    reset = datetime.fromisoformat(summary["rate_limit_resets_at"])
    with patch("cross_harness.runner.datetime", wraps=datetime) as clock:
        clock.now.return_value = reset.astimezone(timezone.utc)
        assert runner.revival(case.repo, home=case.home)[0]["eligibility"] == "eligible"
        case.configure("auto_revival = false\n")
        assert runner.revival(case.repo, home=case.home)[0]["eligibility"] == "not revivable: disabled by configuration"
        with pytest.raises(HarnessError, match="disabled by configuration"):
            runner.retry(run, case.task, home=case.home)
    case.invoke.assert_not_called()


@pytest.mark.parametrize("reset", ["invalid", "2026-01-01T00:00:00"])
def test_invalid_or_offsetless_recorded_reset_stays_nonretryable(case, reset):
    run, _ = case.make_run("limited")
    state = read_state(run)
    state["rate_limit_resets_at"] = reset
    (run / "state.json").write_text(json.dumps(state))
    with pytest.raises(HarnessError, match="reset time unknown"):
        runner.retry(run, case.task, home=case.home)
    case.invoke.assert_not_called()


def test_retry_rejects_unfinalized_limit_even_when_reset_passed(case):
    run, _ = case.make_run("limited")
    (run / "summary.txt").unlink()
    with pytest.raises(HarnessError, match="not finalized"):
        runner.retry(run, case.task, home=case.home)
    case.invoke.assert_not_called()


def test_revival_resumes_recorded_claude_harness_after_role_configuration_changes(case):
    case.config_path.write_text(case.config_path.read_text().replace('harness = "codex"', 'harness = "claude"'))
    run, _ = case.make_run("claude")
    case.configure()
    summary = runner.retry(run, case.task, home=case.home)
    assert summary["status"] == "success"
    assert read_state(Path(summary["run_dir"]))["harness"] == "claude"
    command = case.invoke.call_args.args[0]
    assert "--resume" in command
    assert THREAD in command
    assert case.invoke.call_args.args[2]["CROSS_HARNESS_EXECUTOR"] == "claude"


@pytest.mark.parametrize("thread", [THREAD, None])
def test_revival_preserves_attempts_thread_subject_and_chain(case, thread):
    run, _ = case.make_run("limited", attempts=10, thread=thread)
    (run / "chain-paths.json").write_text('["owned.txt"]')
    state = read_state(run)
    state["escalated"] = True
    (run / "state.json").write_text(json.dumps(state))
    summary = runner.retry(run, case.task, home=case.home)
    continued = Path(summary["run_dir"])
    assert summary["status"] == "success"
    assert summary["attempt"] == read_state(continued)["attempts"] == 10
    assert summary["thread_id"] == read_state(continued)["thread_id"] == thread
    assert read_state(continued)["escalated"] is True
    assert read_state(continued)["consecutive_revivals"] == 0
    assert read_state(run)["revived"] is True
    assert read_state(run)["revived_by"] == str(continued)
    assert (run / "REVIVED").read_text().strip() == str(continued)
    assert (continued / "ROOT_WORKTREE").read_text() == (run / "ROOT_WORKTREE").read_text()
    assert (continued / "commit-subject.txt").read_text() == (run / "commit-subject.txt").read_text()
    assert json.loads((continued / "chain-paths.json").read_text()) == ["owned.txt"]
    assert (continued / "task.md").read_text() == case.task.read_text()
    argv = case.invoke.call_args.args[0]
    assert ("resume" in argv) == bool(thread)
    if thread:
        assert thread in argv
    assert "Continue the remaining unit." in case.invoke.call_args.args[1]
    assert case.invoke.call_count == 1
    with pytest.raises(HarnessError, match="already revived"):
        runner.retry(run, case.task, home=case.home)


def test_revival_reuses_isolated_worktree(case):
    run, _ = case.make_run("isolated", isolated=True)
    worktree = (run / "ISOLATED_WORKTREE").read_text().strip()
    summary = runner.retry(run, case.task, home=case.home)
    continued = Path(summary["run_dir"])
    assert (continued / "ISOLATED_WORKTREE").read_text().strip() == worktree
    assert case.invoke.call_args.args[3] == Path(worktree)
    assert (continued / "ROOT_WORKTREE").read_text().strip() == str(case.repo)
    assert summary["attempt"] == 1


def test_two_consecutive_revivals_stop_third_and_hide_revived_predecessors(case):
    run, _ = case.make_run("first")
    case.invoke.side_effect = case.outcome("blocked", "usage limit")
    for count in (1, 2):
        summary = runner.retry(run, case.task, home=case.home)
        assert read_state(run)["revived"] is True
        run = Path(summary["run_dir"])
        assert summary["attempt"] == 1
        assert summary["consecutive_revivals"] == read_state(run)["consecutive_revivals"] == count
        assert summary["thread_id"] == THREAD
    assert summary["revival"] == "stopped after two consecutive revivals"
    assert "revival: stopped after two consecutive revivals" in (run / "summary.txt").read_text()
    with pytest.raises(HarnessError, match="two consecutive revivals"):
        runner.retry(run, case.task, home=case.home)
    assert case.invoke.call_count == 2
    assert runner.revival(case.repo, home=case.home) == [{
        "run_dir": str(run), "role": "reviewer", "rate_limit_resets_at": summary["rate_limit_resets_at"],
        "eligibility": "not revivable: stopped after two consecutive revivals",
    }]


@pytest.mark.parametrize("status,error", [
    ("success", None), ("failed", "fixture failure"), ("partial", None),
    ("discussion", None), ("blocked", "requires instruction"), ("blocked", "authentication failed"),
])
def test_nonlimit_outcomes_clear_count_without_escalation(case, status, error):
    run, _ = case.make_run("limited", count=1)
    case.invoke.side_effect = case.outcome(status, error)
    with patch("cross_harness.runner.failure_signature", return_value="same"):
        state = read_state(run)
        state["signatures"] = ["same"]
        (run / "state.json").write_text(json.dumps(state))
        summary = runner.retry(run, case.task, home=case.home)
    continued = Path(summary["run_dir"])
    assert summary["status"] == status
    assert read_state(continued)["consecutive_revivals"] == 0
    assert read_state(continued)["attempts"] == 1
    assert read_state(continued)["escalated"] is False
    assert case.invoke.call_count == 1
    if error == "authentication failed":
        with pytest.raises(HarnessError, match="authentication.*safety-policy stop"):
            runner.retry(continued, case.task, home=case.home)


def test_authentication_preflight_clears_count_without_starting_executor(case):
    run, _ = case.make_run("limited", count=1)
    with patch("cross_harness.runner.verify_codex_chatgpt", side_effect=AuthError("unknown authentication")):
        summary = runner.retry(run, case.task, home=case.home)
    state = read_state(Path(summary["run_dir"]))
    assert state["blocked_category"] == "authentication"
    assert state["consecutive_revivals"] == 0
    assert state["attempts"] == 1
    assert not read_state(run).get("revived")
    case.invoke.assert_not_called()


def test_concurrent_revival_is_refused(case):
    run, _ = case.make_run("limited")
    with (run / "revival.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(HarnessError, match="already in progress"):
            runner.retry(run, case.task, home=case.home)
    case.invoke.assert_not_called()


def test_listing_filters_finalization_root_category_and_resolution(case):
    eligible, expected = case.make_run("eligible")
    waiting, _ = case.make_run("waiting", reset=FUTURE)
    unknown, _ = case.make_run("unknown", reset=None)
    for name in ("revived", "dismissed", "unfinished", "foreign", "authentication", "malformed", "nonobject"):
        run, _ = case.make_run(name)
        if name == "revived":
            (run / "REVIVED").write_text("continued elsewhere")
        elif name == "dismissed":
            runner.dismiss_revival(run)
        elif name == "unfinished":
            (run / "summary.txt").unlink()
        elif name == "foreign":
            (run / "ROOT_WORKTREE").write_text(str(case.root / "other"))
        elif name == "authentication":
            state = read_state(run)
            state["blocked_category"] = "authentication"
            (run / "state.json").write_text(json.dumps(state))
        elif name == "malformed":
            (run / "state.json").write_text("bad JSON")
        else:
            (run / "summary.json").write_text("[]")
    nested = case.repo / "nested"
    nested.mkdir()
    rows = runner.revival(nested, home=case.home)
    assert {row["run_dir"]: row["eligibility"] for row in rows} == {
        str(eligible): "eligible", str(waiting): f"waiting until {read_state(waiting)['rate_limit_resets_at']}",
        str(unknown): "not revivable: reset time unknown",
    }
    stdout = StringIO()
    with redirect_stdout(stdout):
        assert main(["--home", str(case.home), "revival", "--cwd", str(nested)]) == 0
    assert stdout.getvalue() == runner.render_revivals(rows)
    assert f"{eligible}\treviewer\t{expected['rate_limit_resets_at']}\teligible\n" in stdout.getvalue()
    with redirect_stdout(StringIO()):
        assert main(["revival", "--dismiss", "--run", str(eligible)]) == 0
    assert read_state(eligible)["revival_dismissed"] is True
    assert (eligible / "REVIVAL_DISMISSED").exists()
    assert str(eligible) not in runner.render_revivals(runner.revival(case.repo, home=case.home))
    with pytest.raises(HarnessError, match="dismissed"):
        runner.retry(eligible, case.task, home=case.home)


def test_cli_rejects_incomplete_dismissal_and_nonlimit_run(case):
    run, _ = case.make_run("limited")
    state = read_state(run)
    state["blocked_category"] = "authentication"
    (run / "state.json").write_text(json.dumps(state))
    for args, reason in (
        (["revival", "--dismiss"], "requires --run"),
        (["revival", "--run", str(run)], "requires --dismiss"),
        (["revival", "--dismiss", "--run", str(run)], "finalized usage-limit"),
    ):
        stderr = StringIO()
        with redirect_stderr(stderr):
            assert main(args) == 2
        assert reason in stderr.getvalue()


def test_session_start_prints_same_list_and_collection_fails_open(case):
    case.make_run("eligible")
    case.make_run("waiting", reset=FUTURE)
    case.make_run("isolated", reset=FUTURE, isolated=True)
    case.make_run("unknown", reset=None)
    expected = runner.render_revivals(runner.revival(case.repo, home=case.home)).rstrip()
    real_run = subprocess.run

    def auth_status(command, *args, **kwargs):
        if command[:3] == ["claude", "auth", "status"]:
            return SimpleNamespace(returncode=0, stdout='{"loggedIn":true}', stderr="")
        return real_run(command, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch("cross_harness.hooks.self_update", return_value=SimpleNamespace(state="ok", warnings=[])))
        stack.enter_context(patch("cross_harness.hooks.cleanup"))
        stack.enter_context(patch("cross_harness.hooks.detected_api_keys", return_value=[]))
        stack.enter_context(patch("cross_harness.hooks.verify_codex_chatgpt"))
        stack.enter_context(patch("cross_harness.hooks.subprocess.run", side_effect=auth_status))
        for collected in ("actual", [], OSError("unreadable"), RuntimeError("collection failed")):
            stdout = StringIO()
            with ExitStack() as attempt:
                if collected != "actual":
                    attempt.enter_context(patch(
                        "cross_harness.hooks.revival", side_effect=collected if isinstance(collected, Exception) else None,
                        return_value=collected,
                    ))
                attempt.enter_context(patch("sys.stdin", StringIO(json.dumps({"cwd": str(case.repo)}))))
                attempt.enter_context(redirect_stdout(stdout))
                assert claude_session_start(case.home) == 0
            text = stdout.getvalue()
            block = text.split("<cross-harness-session>", 1)[1].split("</cross-harness-session>", 1)[0]
            if collected == "actual":
                assert expected in block
                assert "revival --cwd <repo>" in block
                assert "revival --dismiss --run <run_dir>" in block
                assert "not revivable run requires a new delegation of the remaining work" in block
                assert "waiting until " in block
                assert "not revivable: reset time unknown" in block
                assert "Pending isolated run:" not in block
            else:
                assert "Delegated usage-limit" not in text
                assert "revival --" not in text
                assert "unreadable" not in text
                assert "collection failed" not in text


def test_interrupted_revival_is_listed_and_retryable_when_its_lock_is_free(case):
    run, _ = case.make_run("limited")
    case.invoke.side_effect = RuntimeError("wrapper interrupted")
    with pytest.raises(RuntimeError, match="wrapper interrupted"):
        runner.retry(run, case.task, home=case.home)
    successor = Path(read_state(run)["revived_by"])
    assert runner._completed_summary(successor) is None
    assert runner.revival(case.repo, home=case.home)[0]["run_dir"] == str(run)
    assert runner.revival(case.repo, home=case.home)[0]["eligibility"] == "eligible"
    case.invoke.side_effect = case.outcome()
    summary = runner.retry(run, case.task, home=case.home)
    assert summary["status"] == "success"
    assert read_state(run)["revived_by"] == summary["run_dir"]
    assert summary["attempt"] == 1
    assert runner.revival(case.repo, home=case.home) == []
    assert case.invoke.call_count == 2


def test_revival_lock_covers_eligibility_execution_and_finalization(case):
    run, _ = case.make_run("limited")
    refusal = runner._revival_refusal
    finalize = runner.finalize_run
    observed = []

    def assert_locked(stage):
        with pytest.raises(HarnessError, match="already in progress"):
            runner.dismiss_revival(run)
        assert not read_state(run).get("revival_dismissed")
        observed.append(stage)

    def eligibility(*args, **kwargs):
        assert_locked("eligibility")
        return refusal(*args, **kwargs)

    def invoke(*args):
        assert_locked("execution")
        assert runner.revival(case.repo, home=case.home) == []
        return case.outcome()(*args)

    def finalization(*args, **kwargs):
        assert_locked("finalization")
        return finalize(*args, **kwargs)

    case.invoke.side_effect = invoke
    with patch("cross_harness.runner._revival_refusal", side_effect=eligibility), patch(
        "cross_harness.runner.finalize_run", side_effect=finalization,
    ):
        summary = runner.retry(run, case.task, home=case.home)
    assert summary["status"] == "success"
    assert observed == ["eligibility", "execution", "finalization"]
    with runner._revival_lock(run, "test"):
        assert runner._completed_summary(Path(summary["run_dir"])) is not None


def test_dismissal_takes_revival_lock_and_then_prevents_retry(case):
    run, _ = case.make_run("limited")
    with runner._revival_lock(run, "test"):
        with pytest.raises(HarnessError, match="revival dismissal refused.*already in progress"):
            runner.dismiss_revival(run)
        assert not (run / "REVIVAL_DISMISSED").exists()
    runner.dismiss_revival(run)
    with pytest.raises(HarnessError, match="dismissed"):
        runner.retry(run, case.task, home=case.home)
    assert runner.revival(case.repo, home=case.home) == []
    case.invoke.assert_not_called()


@pytest.mark.parametrize("reset,count", [(FUTURE, 0), (None, 0), (PAST, 2)])
def test_awaiting_isolated_limit_is_only_in_revival_and_discard_removes_it(case, reset, count):
    run, summary = case.make_run("isolated", reset=reset, count=count, isolated=True)
    assert summary["pending"] == []
    assert runner.pending(case.repo, home=case.home) == []
    assert [row["run_dir"] for row in runner.revival(case.repo, home=case.home)] == [str(run)]
    runner.discard(run, home=case.home)
    assert (run / "DISCARDED").exists()
    assert runner.revival(case.repo, home=case.home) == []


def test_shared_isolated_chain_awaiting_revival_is_excluded_from_pending(case):
    run, _ = case.make_run("isolated", isolated=True)
    complete = case.outcome("blocked", "usage limit")

    def limit_again(*args):
        code = complete(*args)
        (args[4] / "events.jsonl").write_text(limit_event(FUTURE))
        return code

    case.invoke.side_effect = limit_again
    summary = runner.retry(run, case.task, home=case.home)
    assert summary["status"] == "blocked"
    assert summary["pending"] == []
    assert runner.pending(case.repo, home=case.home) == []
    assert [row["run_dir"] for row in runner.revival(case.repo, home=case.home)] == [summary["run_dir"]]


def test_cleanup_retains_waiting_run_and_worktree_until_retention_after_reset(case):
    blocked_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    reset = blocked_at + timedelta(days=10)
    run, _ = case.make_run("isolated", reset=reset.isoformat(), isolated=True)
    unknown, _ = case.make_run("unknown", reset=None, isolated=True)
    worktree = Path((run / "ISOLATED_WORKTREE").read_text().strip())
    (worktree / "partial.txt").write_text("keep this work\n")
    for candidate in (run, unknown):
        os.utime(candidate, (blocked_at.timestamp(), blocked_at.timestamp()))
    result = cleanup(home=case.home, now=blocked_at + timedelta(days=8))
    assert str(run) not in result["removed"]
    assert run.exists() and worktree.exists()
    assert (worktree / "partial.txt").read_text() == "keep this work\n"
    assert not unknown.exists()
    result = cleanup(home=case.home, now=reset + timedelta(days=7))
    assert str(run) not in result["removed"]
    result = cleanup(home=case.home, now=reset + timedelta(days=7, seconds=1))
    assert str(run) in result["removed"]
    assert not run.exists() and not worktree.exists()
    registered = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=case.repo, check=True, capture_output=True, text=True,
    ).stdout
    assert str(worktree) not in registered


def test_cleanup_retains_worktree_owner_of_waiting_revival_successor(case):
    run, _ = case.make_run("isolated", isolated=True)
    case.invoke.side_effect = case.outcome("blocked", "usage limit")
    summary = runner.retry(run, case.task, home=case.home)
    successor = Path(summary["run_dir"])
    state = read_state(successor)
    state["rate_limit_resets_at"] = FUTURE
    (successor / "state.json").write_text(json.dumps(state))
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    for candidate in (run, successor):
        modified = (now - timedelta(days=8)).timestamp()
        os.utime(candidate, (modified, modified))
    worktree = Path((successor / "ISOLATED_WORKTREE").read_text().strip())
    result = cleanup(home=case.home, now=now)
    assert result["removed"] == []
    assert run.exists() and successor.exists() and worktree.exists()


@pytest.mark.parametrize("elapsed,day", [(0, 9), (10, 9), (59, 9), (60, 9), (61, 10)])
def test_dateless_clock_reset_uses_minute_end_and_sixty_second_today_window(case, elapsed, day):
    start = datetime(2026, 10, 9, 15, 45).astimezone()
    events = case.root / "clock.jsonl"
    events.write_text(json.dumps({
        "type": "turn.failed", "message": "Usage limit reached. Try again at 3:45 PM.",
    }) + "\n")
    parsed = parse_events(events, reference_time=start + timedelta(seconds=elapsed))
    assert parsed["rate_limit_resets_at"] == datetime(2026, 10, day, 15, 46).astimezone().isoformat()
    assert datetime.fromisoformat(parsed["rate_limit_resets_at"]) >= start + timedelta(seconds=59)


def test_latest_rejected_claude_reset_wins_over_later_shorter_reset(case):
    events = case.root / "limits.jsonl"
    events.write_text(limit_event(FUTURE) + limit_event(PAST) + limit_event(None))
    parsed = parse_events(events)
    assert datetime.fromisoformat(parsed["rate_limit_resets_at"]) == datetime.fromisoformat(FUTURE)


@pytest.mark.parametrize("checks_section", ["", "\n# Checks\n"])
def test_revival_inherits_original_checks_when_continuation_declares_none(case, checks_section):
    run, _ = case.make_run("limited", isolated=True)
    case.task.write_text("# Goal\nContinue the remaining work.\n" + checks_section)
    with patch.dict("os.environ", {"TZ": "UTC"}):
        summary = runner.retry(run, case.task, home=case.home)
    continued = Path(summary["run_dir"])
    assert summary["status"] == "success"
    assert summary["checks"] == [{"check": "fixture", "status": "passed", "exit_code": 0}]
    assert runner._declared_checks(continued) == ["fixture"]
    assert "- fixture" in case.invoke.call_args.args[1]
    assert case.invoke.call_args.args[2]["TZ"] == "UTC"
    assert summary["attempt"] == 1


@pytest.mark.parametrize("action", ["delegate", "detached", "retry", "reply"])
def test_future_account_reset_gates_all_launch_paths_across_repositories_before_run_creation(case, action):
    source, _ = case.make_run("source")
    state = read_state(source)
    state["status"] = "discussion" if action == "reply" else "failed"
    state.pop("blocked_category")
    (source / "state.json").write_text(json.dumps(state))
    blocked, blocked_summary = case.make_run("foreign-limit", reset=FUTURE)
    (blocked / "ROOT_WORKTREE").write_text(str(case.root / "another-repository"))
    before = set((case.runtime / "runs").iterdir())
    with patch("cross_harness.runner._new_run_dir") as create:
        with pytest.raises(HarnessError, match="codex is awaiting revival") as error:
            if action == "delegate":
                runner.delegate("reviewer", "review", case.task, case.repo, home=case.home)
            elif action == "detached":
                runner.start_detached_delegate("reviewer", "review", case.task, case.repo, home=case.home)
            elif action == "reply":
                runner.reply(source, case.task, home=case.home)
            else:
                runner.retry(source, case.task, home=case.home)
        create.assert_not_called()
    assert str(blocked) in str(error.value)
    assert blocked_summary["rate_limit_resets_at"] in str(error.value)
    assert set((case.runtime / "runs").iterdir()) == before
    case.invoke.assert_not_called()


def test_account_reset_does_not_gate_other_harness_or_eligible_revival(case):
    case.make_run("future", reset=FUTURE)
    eligible, _ = case.make_run("eligible")
    summary = runner.delegate("explorer", "exploration", case.task, case.repo, home=case.home)
    assert summary["status"] == "success"
    assert read_state(Path(summary["run_dir"]))["harness"] == "claude"
    summary = runner.retry(eligible, case.task, home=case.home)
    assert summary["status"] == "success"
    assert case.invoke.call_count == 2


@pytest.mark.parametrize("resolution", ["unknown", "dismissed", "revived", "discarded"])
def test_unknown_or_resolved_limit_does_not_gate_new_delegation(case, resolution):
    run, _ = case.make_run("limit", reset=None if resolution == "unknown" else FUTURE)
    if resolution == "dismissed":
        runner.dismiss_revival(run)
    elif resolution == "revived":
        (run / "REVIVED").write_text("legacy revival\n")
    elif resolution == "discarded":
        (run / "DISCARDED").write_text("discarded\n")
    summary = runner.delegate("reviewer", "review", case.task, case.repo, home=case.home)
    assert summary["status"] == "success"
    assert case.invoke.call_count == 1
