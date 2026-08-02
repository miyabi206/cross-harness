from pathlib import Path
from unittest.mock import patch
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import cross_harness.runner as runner
from cross_harness.errors import AuthError, DirtyWorktreeError, HarnessError, SupervisorDiedError
from cross_harness.files import load_json, sha256
from cross_harness.config import default_config
from cross_harness.runner import CLAUDE_EXECUTOR_CHARTER, CODEX_EXECUTOR_CHARTER, _claude_command, _claude_sandbox_profile, _codex_command, _contain_claude_write_command, _escalated_role, _executor_task, _filtered_executor_stderr, _invoke_safe, _self_reversions, _tee, _write_baseline, _write_claude_final_from_events, _write_execution_record, adopt, delegate, retry, start_detached_delegate, wait_for_run
from cross_harness.runner import finalize_blocked_run, finalize_run
from cross_harness.summarize import parse_events


def git(cwd: Path, *args: str):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("before\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")
        self.task = self.root / "task.md"
        self.task.write_text("# Goal\nRead the repository and report success.\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_tee_writes_and_flushes_small_block_before_pipe_closes(self):
        reader_fd, writer_fd = os.pipe()
        target = self.root / "streamed-events.jsonl"
        reader = os.fdopen(reader_fd, "rb")
        writer = os.fdopen(writer_fd, "wb")
        thread = threading.Thread(target=_tee, args=(reader, target), daemon=True)
        thread.start()
        try:
            payload = b'{"type":"turn.started"}\n'
            writer.write(payload)
            writer.flush()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if target.exists() and target.read_bytes() == payload:
                    break
                time.sleep(0.01)
            self.assertEqual(payload, target.read_bytes())
        finally:
            writer.close()
            thread.join(timeout=1)
            reader.close()
        self.assertFalse(thread.is_alive())

    def fake_invoke(self, command, task, env, cwd, run_dir, timeout):
        self.assertEqual("1", env["CROSS_HARNESS_ACTIVE"])
        self.assertIn(env["CROSS_HARNESS_EXECUTOR"], {"claude", "codex"})
        execution = json.loads((run_dir / "execution.json").read_text())
        self.assertEqual(env["CROSS_HARNESS_PARENT"], execution["parent_harness"])
        (run_dir / "events.jsonl").write_text(
            '{"type":"thread.started","thread_id":"00000000-0000-0000-0000-000000000001"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n'
        )
        (run_dir / "stderr.log").write_text("")
        (run_dir / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "inspected", "changed_files": [],
            "tests": ["read-only inspection"], "error": None, "next_decision": None,
        }))
        return 0

    def test_execution_record_includes_role_model_and_effort(self):
        run = self.root / "execution-record"
        run.mkdir()
        role = {"harness": "codex", "model": "gpt-5.6-luna", "effort": "high", "write": True}
        _write_execution_record(run, "implementer", role, "implementation", self.repo, "claude")
        execution = json.loads((run / "execution.json").read_text())
        self.assertEqual("gpt-5.6-luna", execution["model"])
        self.assertEqual("high", execution["effort"])

    def test_codex_executor_task_injects_charter_without_changing_claude_task(self):
        task = "# Goal\nImplement the requested change.\n"

        codex_task = _executor_task(task, "codex")

        self.assertTrue(codex_task.startswith(CODEX_EXECUTOR_CHARTER + "\n\n# Delegated task\n\n"))
        self.assertTrue(codex_task.endswith(task))
        self.assertIn("exactly these six fields", codex_task)
        self.assertIn("delegate to another agent", codex_task)
        self.assertIn("launch Claude", codex_task)
        self.assertEqual(task, _executor_task(task, "claude"))
        self.assertNotIn(CODEX_EXECUTOR_CHARTER, CLAUDE_EXECUTOR_CHARTER)

    def _failed_write_run(self, name: str, changed_file: str, contents: str, attempt: int = 1) -> Path:
        """Create a real failed write-run artifact for retry guard tests."""
        run = self.root / name
        run.mkdir()
        _write_baseline(run, self.repo)
        (self.repo / changed_file).write_text(contents)
        (run / "events.jsonl").write_text(
            '{"type":"thread.started","thread_id":"00000000-0000-0000-0000-000000000001"}\n'
            '{"type":"turn.failed","error":{"message":"fixture failure"}}\n'
        )
        (run / "stderr.log").write_text("fixture failure")
        (run / "final.json").write_text(json.dumps({
            "status": "failed", "work_completed": "", "changed_files": [],
            "tests": [], "error": "fixture failure", "next_decision": None,
        }))
        role = default_config()["roles"]["implementer"]
        finalize_run(run, "implementer", role, "implementation", self.repo, 1, attempt)
        return run

    @staticmethod
    def _successful_retry(command, task, env, cwd, run_dir, timeout):
        (run_dir / "events.jsonl").write_text(
            '{"type":"item.completed","item":{"type":"command_execution","command":"fixture","status":"completed","exit_code":0}}\n'
            '{"type":"turn.completed","usage":{}}\n'
        )
        (run_dir / "stderr.log").write_text("")
        (run_dir / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": ["fixture"], "error": None, "next_decision": None,
        }))
        return 0

    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner.verify_claude_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_read_only_delegation_generates_bounded_artifacts(self, invoke, ownership, verify):
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self.fake_invoke
        summary = delegate("tester", "test", self.task, self.repo, home=self.home)
        run = Path(summary["run_dir"])
        self.assertEqual("partial", summary["status"])
        self.assertIn("no checks declared", summary["error"])
        self.assertTrue((run / "task.md").exists())
        self.assertTrue((run / "events.jsonl").exists())
        self.assertTrue((run / "summary.txt").exists())
        self.assertTrue((run / "baseline.json").exists())
        self.assertEqual(1, invoke.call_count)
        self.assertEqual("claude", invoke.call_args.args[2]["CROSS_HARNESS_EXECUTOR"])

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_write_delegation_sets_write_executor_marker(self, invoke, ownership, verify):
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self.fake_invoke
        summary = delegate("implementer", "implementation", self.task, self.repo, home=self.home)
        environment = invoke.call_args.args[2]
        execution = json.loads((Path(summary["run_dir"]) / "execution.json").read_text())
        self.assertEqual("1", environment["CROSS_HARNESS_WRITE"])
        self.assertEqual("claude", environment["CROSS_HARNESS_PARENT"])
        self.assertEqual(environment["CROSS_HARNESS_PARENT"], execution["parent_harness"])
        self.assertTrue(invoke.call_args.args[1].startswith(CODEX_EXECUTOR_CHARTER))
        self.assertEqual("partial", summary["status"])
        self.assertIn("no checks declared", summary["error"])

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_delegate_writes_json_artifacts_for_invalid_utf8_changes(self, invoke, ownership, verify):
        file_name = os.fsdecode(b"bad-\xff-delegate.txt")
        path = self.repo / file_name
        path.write_bytes(b"before\n")
        git(self.repo, "add", file_name)
        git(self.repo, "commit", "-m", "invalid utf8 delegate path")

        def invoke_with_invalid_path(command, task, env, cwd, run_dir, timeout):
            (cwd / file_name).write_bytes(b"after\n")
            (run_dir / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
            (run_dir / "stderr.log").write_text("")
            (run_dir / "final.json").write_text(json.dumps({
                "status": "success", "work_completed": "done", "changed_files": [],
                "tests": [], "error": None, "next_decision": None,
            }))
            return 0

        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = invoke_with_invalid_path

        summary = delegate("implementer", "implementation", self.task, self.repo, home=self.home)
        run = Path(summary["run_dir"])

        self.assertEqual(file_name, summary["changed_files"][0])
        self.assertEqual([], load_json(run / "baseline.json", {})["changed_files"])
        self.assertEqual(file_name, load_json(run / "summary.json", {})["diff_summary"][0]["file"])
        records = load_json(self.home / ".local/state/cross-harness/delegated-changes.json", {})
        self.assertEqual({file_name}, set(records[str(self.repo.resolve())]))
        (run / "summary.json").read_bytes().decode("utf-8")
        (run / "summary.txt").read_bytes().decode("utf-8")

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_delegate_releases_root_lock_when_baseline_fails(self, invoke, ownership, verify):
        runtime_root = self.home / ".local/state/cross-harness"
        with patch("cross_harness.runner._write_baseline", side_effect=RuntimeError("baseline failed")):
            with self.assertRaisesRegex(RuntimeError, "baseline failed"):
                delegate("implementer", "implementation", self.task, self.repo, home=self.home)

        self.assertTrue(runner._acquire_root_lock(runtime_root, self.repo))
        runner._release_root_lock(runtime_root, self.repo)
        invoke.assert_not_called()
        ownership.assert_not_called()
        verify.assert_not_called()

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_delegate_does_not_release_a_preexisting_root_lock(self, invoke, ownership, verify):
        runtime_root = self.home / ".local/state/cross-harness"
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self.fake_invoke
        self.assertTrue(runner._acquire_root_lock(runtime_root, self.repo))
        try:
            summary = delegate("implementer", "implementation", self.task, self.repo, home=self.home)
            run = Path(summary["run_dir"])
            self.assertTrue((run / "ISOLATED_WORKTREE").is_file())
            self.assertIsNone(runner._try_lock(runner._root_lock_path(runtime_root, self.repo)))
        finally:
            runner._release_root_lock(runtime_root, self.repo)

        ownership.assert_called_once()
        verify.assert_called_once()

    def test_try_lock_converts_open_failures_to_harness_error(self):
        lock_path = self.root / "runtime" / "locks" / "root.lock"
        with patch("cross_harness.runner.os.open", side_effect=OSError("read-only filesystem")):
            with self.assertRaisesRegex(HarnessError, "could not open lock file"):
                runner._try_lock(lock_path)

    def test_delegated_changes_lock_timeout_is_bounded_and_diagnostic(self):
        runtime_root = self.root / "runtime"
        with patch.object(runner, "_try_lock", return_value=None), patch.object(
            runner, "_DELEGATED_CHANGES_LOCK_TIMEOUT_SECONDS", 0
        ):
            with self.assertRaisesRegex(HarnessError, "timed out waiting for lock"):
                runner._acquire_delegated_changes_lock(runtime_root)

    def test_live_supervisor_counts_toward_global_parallel_limit(self):
        config = default_config()
        runtime_root = self.root / "runtime"
        config["runtime_root"] = str(runtime_root)
        config["max_parallel"] = 1
        run_dir = runtime_root / "runs" / "live"
        run_dir.mkdir(parents=True)
        runner._write_execution_record(run_dir, "tester", config["roles"]["tester"], "test", self.repo, "claude")
        (run_dir / "supervisor.pid").write_text("123\n")
        with patch.object(runner, "_supervisor_alive", return_value=True):
            mutex = runner._acquire_parallel_mutex(runtime_root)
            try:
                error = runner._parallel_limit_error(config, runtime_root, "reviewer")
            finally:
                runner._release_lock(mutex)
        self.assertIn("global max_parallel limit 1", error)
        self.assertIn("'reviewer'", error)

    def test_exited_supervisor_no_longer_counts_toward_parallel_limit(self):
        config = default_config()
        runtime_root = self.root / "runtime"
        config["runtime_root"] = str(runtime_root)
        config["max_parallel"] = 1
        run_dir = runtime_root / "runs" / "dead"
        run_dir.mkdir(parents=True)
        runner._write_execution_record(run_dir, "tester", config["roles"]["tester"], "test", self.repo, "claude")
        (run_dir / "supervisor.pid").write_text("123\n")
        with patch.object(runner, "_supervisor_alive", return_value=False):
            mutex = runner._acquire_parallel_mutex(runtime_root)
            try:
                error = runner._parallel_limit_error(config, runtime_root, "reviewer")
            finally:
                runner._release_lock(mutex)
        self.assertIsNone(error)

    def test_role_parallel_limit_counts_only_the_same_role(self):
        config = default_config()
        runtime_root = self.root / "runtime"
        config["runtime_root"] = str(runtime_root)
        config["max_parallel"] = 3
        config["roles"]["tester"]["max_parallel"] = 1
        run_dir = runtime_root / "runs" / "tester"
        run_dir.mkdir(parents=True)
        runner._write_execution_record(run_dir, "tester", config["roles"]["tester"], "test", self.repo, "claude")
        (run_dir / "supervisor.pid").write_text("123\n")
        with patch.object(runner, "_supervisor_alive", return_value=True):
            mutex = runner._acquire_parallel_mutex(runtime_root)
            try:
                tester_error = runner._parallel_limit_error(config, runtime_root, "tester")
                reviewer_error = runner._parallel_limit_error(config, runtime_root, "reviewer")
            finally:
                runner._release_lock(mutex)
        self.assertIn("role 'tester' max_parallel limit 1", tester_error)
        self.assertIsNone(reviewer_error)

    def test_foreground_process_exit_does_not_release_live_supervisor_capacity(self):
        config = default_config()
        runtime_root = self.root / "runtime"
        config["runtime_root"] = str(runtime_root)
        config["max_parallel"] = 1
        run_dir = runtime_root / "runs" / "interrupted-foreground"
        run_dir.mkdir(parents=True)
        runner._write_execution_record(run_dir, "tester", config["roles"]["tester"], "test", self.repo, "claude")
        (run_dir / "supervisor.pid").write_text("123\n")
        (run_dir / "executor.pid").write_text("999\n")  # The foreground executor may be gone.
        with patch.object(runner, "_supervisor_alive", return_value=True):
            mutex = runner._acquire_parallel_mutex(runtime_root)
            try:
                error = runner._parallel_limit_error(config, runtime_root, "reviewer")
            finally:
                runner._release_lock(mutex)
        self.assertIn("global max_parallel limit 1", error)

    def test_live_run_without_execution_record_counts_toward_its_role_limit(self):
        config = default_config()
        runtime_root = self.root / "runtime"
        config["runtime_root"] = str(runtime_root)
        config["max_parallel"] = 2
        config["roles"]["tester"]["max_parallel"] = 1
        run_dir = runtime_root / "runs" / "reserved"
        run_dir.mkdir(parents=True)
        (run_dir / "role").write_text("tester\n")
        (run_dir / "supervisor.pid").write_text("123\n")
        with patch.object(runner, "_supervisor_alive", return_value=True):
            error = runner._parallel_limit_error(config, runtime_root, "tester")
        self.assertIn("role 'tester' max_parallel limit 1", error)

    def test_live_run_with_empty_recorded_role_counts_toward_target_role_limit(self):
        config = default_config()
        runtime_root = self.root / "runtime"
        config["runtime_root"] = str(runtime_root)
        config["max_parallel"] = 2
        config["roles"]["tester"]["max_parallel"] = 1
        run_dir = runtime_root / "runs" / "unknown-empty"
        run_dir.mkdir(parents=True)
        (run_dir / "role").write_text("\n")
        (run_dir / "execution.json").write_text(json.dumps({"role_name": ""}))
        (run_dir / "supervisor.pid").write_text("123\n")
        with patch.object(runner, "_supervisor_alive", return_value=True):
            error = runner._parallel_limit_error(config, runtime_root, "tester")
        self.assertIn("role 'tester' max_parallel limit 1", error)

    def test_live_run_with_unknown_recorded_role_counts_toward_target_role_limit(self):
        config = default_config()
        runtime_root = self.root / "runtime"
        config["runtime_root"] = str(runtime_root)
        config["max_parallel"] = 2
        config["roles"]["tester"]["max_parallel"] = 1
        run_dir = runtime_root / "runs" / "unknown-role"
        run_dir.mkdir(parents=True)
        (run_dir / "role").write_text("unconfigured\n")
        (run_dir / "supervisor.pid").write_text("123\n")
        with patch.object(runner, "_supervisor_alive", return_value=True):
            error = runner._parallel_limit_error(config, runtime_root, "tester")
        self.assertIn("role 'tester' max_parallel limit 1", error)

    def test_orphaned_live_run_does_not_consume_parallel_capacity(self):
        config = default_config()
        runtime_root = self.root / "runtime"
        config["runtime_root"] = str(runtime_root)
        config["max_parallel"] = 1
        run_dir = runtime_root / "runs" / "orphaned"
        run_dir.mkdir(parents=True)
        (run_dir / "role").write_text("tester\n")
        (run_dir / "supervisor.pid").write_text("123\n")
        (run_dir / "ORPHANED").write_text("marked\n")
        with patch.object(runner, "_supervisor_alive", return_value=True):
            self.assertIsNone(runner._parallel_limit_error(config, runtime_root, "reviewer"))

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_synchronous_delegate_blocks_at_parallel_limit(self, invoke, ownership, verify):
        config = default_config()
        runtime_root = self.home / ".local/state/cross-harness"
        config["runtime_root"] = str(runtime_root)
        config["max_parallel"] = 1
        config["roles"]["tester"]["harness"] = "codex"
        live = runtime_root / "runs" / "live"
        live.mkdir(parents=True)
        (live / "role").write_text("tester\n")
        (live / "supervisor.pid").write_text("123\n")
        verify.return_value = (Path("/usr/bin/true"), False)
        with patch.object(runner, "load_config", return_value=config), patch.object(
            runner, "_supervisor_alive", return_value=True
        ):
            summary = delegate("tester", "test", self.task, self.repo, home=self.home)
        self.assertEqual("blocked", summary["status"])
        self.assertIn("global max_parallel limit 1", summary["error"])
        invoke.assert_not_called()

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_retry_blocks_at_parallel_limit(self, invoke, ownership, verify):
        config = default_config()
        runtime_root = self.home / ".local/state/cross-harness"
        config["runtime_root"] = str(runtime_root)
        config["max_parallel"] = 1
        config["roles"]["tester"]["harness"] = "codex"
        previous = self.root / "retry-limit"
        previous.mkdir()
        (previous / "state.json").write_text(json.dumps({
            "role": "tester", "kind": "test", "cwd": str(self.repo),
            "thread_id": None, "attempts": 0, "signatures": [], "escalated": False,
            "status": "failed", "model": "haiku", "effort": "medium",
        }))
        live = runtime_root / "runs" / "live"
        live.mkdir(parents=True)
        (live / "role").write_text("tester\n")
        (live / "supervisor.pid").write_text("123\n")
        verify.return_value = (Path("/usr/bin/true"), False)
        with patch.object(runner, "load_config", return_value=config), patch.object(
            runner, "_supervisor_alive", return_value=True
        ):
            summary = retry(previous, self.task, home=self.home)
        self.assertEqual("blocked", summary["status"])
        self.assertIn("global max_parallel limit 1", summary["error"])
        invoke.assert_not_called()

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_security_reviewer_review_does_not_require_high_risk_confirmation(
        self, invoke, ownership, verify
    ):
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self.fake_invoke

        summary = delegate("security_reviewer", "review", self.task, self.repo, home=self.home)

        self.assertEqual("success", summary["status"])
        self.assertEqual(1, invoke.call_count)
        with self.assertRaisesRegex(HarnessError, "requires --confirm-high-risk"):
            delegate("security_reviewer", "security_review", self.task, self.repo, home=self.home)

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_codex_parent_harness_is_passed_and_recorded(self, invoke, ownership, verify):
        config = self.root / "codex-parent.toml"
        contents = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(contents.replace('parent_harness = "claude"', 'parent_harness = "codex"', 1))
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self.fake_invoke

        summary = delegate("implementer", "implementation", self.task, self.repo, config_path=config, home=self.home)

        environment = invoke.call_args.args[2]
        execution = json.loads((Path(summary["run_dir"]) / "execution.json").read_text())
        self.assertEqual("codex", environment["CROSS_HARNESS_PARENT"])
        self.assertEqual(environment["CROSS_HARNESS_PARENT"], execution["parent_harness"])

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_delegate_summary_records_defaulted_settings_from_partial_config(self, invoke, ownership, verify):
        config = self.root / "partial-delegate.toml"
        config.write_text('[roles.implementer]\nmodel = "custom-implementer"\n')
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self.fake_invoke

        summary = delegate("implementer", "implementation", self.task, self.repo, config_path=config, home=self.home)
        recorded_summary = json.loads((Path(summary["run_dir"]) / "summary.json").read_text())

        self.assertIn("roles.tester.model", recorded_summary["defaulted_settings"])
        self.assertNotIn("roles.implementer.model", recorded_summary["defaulted_settings"])

    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner.verify_claude_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_read_only_summary_excludes_preexisting_untracked_file(self, invoke, ownership, verify):
        (self.repo / "mine.txt").write_text("pre-existing\n")
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self.fake_invoke
        summary = delegate("tester", "test", self.task, self.repo, home=self.home)
        self.assertEqual([], summary["changed_files"])
        self.assertEqual([], summary["diff_summary"])

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_dirty_worktree_blocks_write_before_codex(self, invoke, ownership, verify):
        (self.repo / "user-change.txt").write_text("mine\n")
        config = self.root / "stop.toml"
        contents = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(contents.replace(
            'dirty_worktree_policy = "allow_delegated"', 'dirty_worktree_policy = "stop"', 1
        ))
        with self.assertRaises(DirtyWorktreeError):
            delegate("implementer", "implementation", self.task, self.repo, config_path=config, home=self.home)
        invoke.assert_not_called()
        verify.assert_not_called()
        ownership.assert_not_called()
        self.assertEqual("mine\n", (self.repo / "user-change.txt").read_text())
        runs = list((self.home.resolve() / ".local/state/cross-harness/runs").iterdir())
        self.assertEqual(1, len(runs))
        state = json.loads((runs[0] / "state.json").read_text())
        self.assertEqual("dirty_worktree", state["blocked_category"])
        self.assertTrue((runs[0] / "BLOCKED").exists())

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_allow_policy_delegates_in_dirty_worktree(self, invoke, ownership, verify):
        (self.repo / "user-change.txt").write_text("mine\n")
        config = self.root / "allow.toml"
        contents = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(contents.replace(
            'dirty_worktree_policy = "allow_delegated"', 'dirty_worktree_policy = "allow"', 1
        ))
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self.fake_invoke

        delegate("implementer", "implementation", self.task, self.repo, config_path=config, home=self.home)

        self.assertEqual(1, invoke.call_count)
        ownership.assert_called_once()

    @patch("cross_harness.runner.verify_claude_subscription", side_effect=AuthError("not logged in"))
    @patch("cross_harness.runner.verify_claude_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_auth_failure_saves_blocked_state_before_codex(self, invoke, ownership, verify):
        summary = delegate("tester", "test", self.task, self.repo, home=self.home)
        self.assertEqual("blocked", summary["status"])
        run = Path(summary["run_dir"])
        state = json.loads((run / "state.json").read_text())
        self.assertEqual("authentication", state["blocked_category"])
        self.assertEqual(0, state["attempts"])
        self.assertTrue((run / "BLOCKED").exists())
        invoke.assert_not_called()

    def test_turn_failure_overrides_zero_process_exit(self):
        run = self.root / "failed-run"
        run.mkdir()
        (run / "events.jsonl").write_text('{"type":"turn.failed","error":{"message":"invalid schema"}}\n')
        (run / "stderr.log").write_text("")
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000}
        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)
        self.assertEqual("failed", summary["status"])
        self.assertIn("invalid schema", summary["error"])

    def test_finalize_summaries_record_defaulted_settings_and_default_to_empty(self):
        role = {"model": "haiku", "effort": "medium", "output_limit_chars": 8000, "write": False}
        explicit_run = self.root / "explicit-defaulted"
        explicit_run.mkdir()
        (explicit_run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (explicit_run / "stderr.log").write_text("")
        (explicit_run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))

        explicit_summary = finalize_run(
            explicit_run, "tester", role, "review", self.repo, 0, 1,
            defaulted_settings=["roles.tester.timeout_seconds"],
        )

        self.assertEqual(["roles.tester.timeout_seconds"], explicit_summary["defaulted_settings"])
        self.assertEqual(
            ["roles.tester.timeout_seconds"],
            json.loads((explicit_run / "summary.json").read_text())["defaulted_settings"],
        )

        implicit_run = self.root / "implicit-defaulted"
        implicit_run.mkdir()
        (implicit_run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (implicit_run / "stderr.log").write_text("")
        (implicit_run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        implicit_summary = finalize_run(implicit_run, "tester", role, "review", self.repo, 0, 1)
        self.assertEqual([], implicit_summary["defaulted_settings"])
        implicit_summary_on_disk = json.loads((implicit_run / "summary.json").read_text())
        self.assertIn("defaulted_settings", implicit_summary_on_disk)
        self.assertEqual([], implicit_summary_on_disk["defaulted_settings"])

        blocked_explicit = self.root / "blocked-explicit-defaulted"
        blocked_explicit.mkdir()
        blocked_summary = finalize_blocked_run(
            blocked_explicit, "tester", role, "test", self.repo, "configuration unavailable", "configuration",
            defaulted_settings=["fallback.codex"],
        )
        self.assertEqual(["fallback.codex"], blocked_summary["defaulted_settings"])
        self.assertEqual(
            ["fallback.codex"],
            json.loads((blocked_explicit / "summary.json").read_text())["defaulted_settings"],
        )

        blocked_implicit = self.root / "blocked-implicit-defaulted"
        blocked_implicit.mkdir()
        implicit_blocked_summary = finalize_blocked_run(
            blocked_implicit, "tester", role, "test", self.repo, "configuration unavailable", "configuration",
        )
        self.assertEqual([], implicit_blocked_summary["defaulted_settings"])
        implicit_blocked_summary_on_disk = json.loads((blocked_implicit / "summary.json").read_text())
        self.assertIn("defaulted_settings", implicit_blocked_summary_on_disk)
        self.assertEqual([], implicit_blocked_summary_on_disk["defaulted_settings"])

    def test_success_ignores_benign_codex_model_cache_stderr(self):
        run = self.root / "benign-codex-cache-stderr-run"
        run.mkdir()
        (run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        stderr = """2026-07-21T12:35:26.280306Z ERROR codex_models_manager::cache: failed to load models cache: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:36:09.559475Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:37:03.347706Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:37:30.366180Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:37:43.062416Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:37:45.115478Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:37:58.937357Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:38:01.356853Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:38:16.038313Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:38:20.977552Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:38:48.047286Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:38:51.071847Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
2026-07-21T12:38:55.987904Z ERROR codex_models_manager::manager: failed to renew cache TTL: missing field `supports_reasoning_summaries` at line 88 column 5
"""
        (run / "stderr.log").write_text(stderr)
        self.assertEqual("", _filtered_executor_stderr(stderr))
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000}

        summary = finalize_run(run, "reviewer", role, "review", self.repo, 0, 1)

        self.assertEqual("success", summary["status"])
        self.assertIsNone(summary["error"])

    def test_success_ignores_rate_limit_and_authentication_words_in_stderr(self):
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000}
        cases = (
            "npm WARN unauthorized registry entry\n",
            "df: disk quota exceeded\n",
            "INFO authentication module loaded\n",
        )
        for index, stderr in enumerate(cases):
            with self.subTest(stderr=stderr):
                run = self.root / f"success-stderr-pattern-{index}-run"
                run.mkdir()
                (run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
                (run / "stderr.log").write_text(stderr)
                (run / "final.json").write_text(json.dumps({
                    "status": "success", "work_completed": "done", "changed_files": [],
                    "tests": [], "error": None, "next_decision": None,
                }))

                summary = finalize_run(run, "reviewer", role, "review", self.repo, 0, 1)

                self.assertEqual("success", summary["status"])
                self.assertIsNone(summary["error"])
                state = json.loads((run / "state.json").read_text())
                self.assertNotIn("blocked_category", state)

    def test_codex_cache_filter_preserves_other_modules_and_levels(self):
        stderr = (
            "2026-07-21T12:35:26.280306Z WARN codex_models_manager::cache: failed to load models cache: stale\n"
            "2026-07-21T12:35:26.280306Z ERROR codex_models_manager::manager: failed to update cache TTL: stale\n"
            "2026-07-21T12:35:26.280306Z ERROR codex_models_manager_extra::cache: failed to load models cache: stale\n"
        )

        self.assertEqual(stderr.rstrip("\n"), _filtered_executor_stderr(stderr))

    def test_invalid_final_status_fails_closed_and_records_reason(self):
        run = self.root / "invalid-final-status-run"
        run.mkdir()
        (run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "completed", "work_completed": "tests failed", "changed_files": [],
            "tests": ["uv run pytest -q: 14 failed"], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000}

        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        self.assertEqual("failed", summary["status"])
        self.assertIn("invalid final status 'completed'", summary["error"])
        self.assertIn("success", summary["error"])
        self.assertIn("partial", summary["error"])

    def test_finalized_dict_tests_are_normalized_and_artifacts_are_written(self):
        run = self.root / "dict-tests-run"
        run.mkdir()
        (run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "tested",
            "changed_files": [{"z": 1, "a": "result"}, 3],
            "tests": [{"result": "passed", "command": "uv run pytest -q"}],
            "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000}

        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        expected_test = '{"command": "uv run pytest -q", "result": "passed"}'
        expected_changed = ['{"a": "result", "z": 1}', "3"]
        self.assertEqual([expected_test], summary["tests"])
        self.assertEqual([], summary["changed_files"])
        self.assertEqual(expected_changed, summary["reported_changed_files"])
        self.assertEqual(expected_changed, summary["unverified_changed_files"])
        self.assertTrue((run / "summary.txt").exists())
        self.assertTrue((run / "summary.json").exists())
        self.assertIn(expected_test, (run / "summary.txt").read_text())
        self.assertEqual([expected_test], json.loads((run / "summary.json").read_text())["tests"])

    def test_large_event_log_is_preserved_and_summary_compresses_over_ninety_percent(self):
        run = self.root / "large-output-run"
        run.mkdir()
        payload = "x" * 1000
        events = "".join(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": payload}}) + "\n"
            for _ in range(200)
        )
        events += '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
        (run / "events.jsonl").write_text(events)
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": ["fixture passed"], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000}
        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)
        self.assertGreater(summary["raw_artifact_bytes"], 200_000)
        self.assertGreaterEqual(summary["compression_percent"], 90)
        self.assertLessEqual(len((run / "summary.txt").read_text()), 8000)
        self.assertIn(payload, (run / "events.jsonl").read_text())

    def test_diff_summary_includes_staged_and_untracked_files(self):
        (self.repo / "README.md").write_text("before\nafter\n")
        git(self.repo, "add", "README.md")
        (self.repo / "new.txt").write_text("new\n")
        run = self.root / "diff-run"
        run.mkdir()
        (run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "changed",
            "changed_files": ["README.md", "new.txt", "reported-only.txt"],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-terra", "effort": "medium", "output_limit_chars": 8000}
        summary = finalize_run(run, "implementer", role, "implementation", self.repo, 0, 1)
        by_file = {item["file"]: item for item in summary["diff_summary"]}
        self.assertEqual("1", by_file["README.md"]["added"])
        self.assertTrue(by_file["new.txt"]["untracked"])
        self.assertEqual(["README.md", "new.txt"], summary["changed_files"])
        self.assertEqual(
            ["README.md", "new.txt", "reported-only.txt"],
            summary["reported_changed_files"],
        )
        self.assertEqual(["reported-only.txt"], summary["unverified_changed_files"])
        self.assertIn("diff_stat", (run / "summary.txt").read_text())

    def test_diff_details_preserves_invalid_utf8_file_names(self):
        raw_name = b"bad-\xff-name.txt"
        file_name = os.fsdecode(raw_name)
        path = self.repo / file_name
        path.write_bytes(b"before\n")
        git(self.repo, "add", file_name)
        git(self.repo, "commit", "-m", "invalid utf8 path")
        path.write_bytes(b"after\n")

        _, details, changed = runner._diff_details(self.repo)

        self.assertEqual([file_name], changed)
        self.assertEqual(file_name, details[0]["file"])
        run = self.root / "invalid-utf8-baseline"
        run.mkdir()
        _write_baseline(run, self.repo)
        baseline = load_json(run / "baseline.json", {})
        self.assertEqual(file_name, baseline["diff_summary"][0]["file"])

    def test_unreported_changes_are_normalized_without_affecting_unverified_changes(self):
        (self.repo / "README.md").write_text("before\nafter\n")
        (self.repo / "new.txt").write_text("new\n")
        (self.repo / "unreported.txt").write_text("external\n")
        run = self.root / "unreported-diff-run"
        run.mkdir()
        (run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "changed",
            "changed_files": [f"{self.repo}//README.md", "./new.txt"],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-terra", "effort": "medium", "output_limit_chars": 8000}

        summary = finalize_run(run, "implementer", role, "implementation", self.repo, 0, 1)

        self.assertEqual("partial", summary["status"])
        self.assertEqual(["unreported.txt"], summary["unreported_changed_files"])
        self.assertEqual(
            [f"{self.repo}//README.md", "./new.txt"],
            summary["unverified_changed_files"],
        )
        self.assertIn("unreported_changed_files: unreported.txt", (run / "summary.txt").read_text())

    def test_read_only_role_changes_fail_after_execution(self):
        run = self.root / "read-only-change-run"
        run.mkdir()
        _write_baseline(run, self.repo)
        (self.repo / "README.md").write_text("after\n")
        (run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "inspected", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))

        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}
        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        self.assertEqual("failed", summary["status"])
        self.assertIn("read-only role modified the worktree", summary["error"])
        self.assertEqual(["README.md"], summary["changed_files"])

    def test_read_only_failed_command_overrides_reported_success_and_preserves_string_test(self):
        run = self.root / "read-only-command-failure-run"
        run.mkdir()
        (run / "task.md").write_text("# Checks\n- uv run pytest -q\n")
        (run / "events.jsonl").write_text(json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "uv run pytest -q",
                "status": "failed",
                "exit_code": 1,
                "aggregated_output": "14 failed, 100 passed",
            },
        }) + "\n")
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "inspected", "changed_files": [],
            "tests": "14 failed, 100 passed", "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        self.assertEqual("failed", summary["status"])
        self.assertIn("declared check failed: uv run pytest -q (exit 1)", summary["error"])
        self.assertEqual([{"check": "uv run pytest -q", "status": "failed", "exit_code": 1}], summary["checks"])
        self.assertEqual(["14 failed, 100 passed"], summary["tests"])
        self.assertIn("tests (executor-reported): 14 failed, 100 passed", (run / "summary.txt").read_text())

    def test_read_only_cross_harness_hook_rejection_does_not_override_success(self):
        run = self.root / "read-only-policy-denial-run"
        run.mkdir()
        (run / "events.jsonl").write_text(json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "git status",
                "status": "failed",
                "exit_code": 1,
                "aggregated_output": "PreToolUse:Bash hook error: [/Users/example/.local/bin/cross-harness hook claude-pre-tool-use]: cross-harness: nested executor launch from delegated Claude is blocked",
            },
        }) + "\n")
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "inspected", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        self.assertEqual("partial", summary["status"])
        self.assertIn("no checks declared", summary["error"])

    def test_read_only_successful_command_does_not_override_reported_success(self):
        run = self.root / "read-only-command-success-run"
        run.mkdir()
        command = "sed -n '1,20p' README.md"
        (run / "events.jsonl").write_text(
            json.dumps({
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": command,
                    "status": "in_progress",
                    "exit_code": None,
                },
            }) + "\n" + json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": command,
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "read successfully",
                },
            }) + "\n"
        )
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "inspected", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "security_reviewer", role, "review", self.repo, 0, 1)

        self.assertEqual("success", summary["status"])
        self.assertEqual([], parse_events(run / "events.jsonl")["commands"])

    def test_write_role_failed_command_does_not_override_reported_success(self):
        run = self.root / "write-command-failure-run"
        run.mkdir()
        (run / "task.md").write_text("# Checks\n- scripts/test.sh\n")
        (run / "events.jsonl").write_text("".join(json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution", "command": command, "status": status,
                "exit_code": exit_code, "aggregated_output": output,
            },
        }) + "\n" for command, status, exit_code, output in (
            ("scripts/test.sh", "completed", 0, "passed"),
            ("uv run pytest -q", "failed", 1, "failure before fix"),
        )))
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "implemented", "changed_files": [],
            "tests": ["uv run pytest -q: passed after fix"], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-terra", "effort": "medium", "output_limit_chars": 8000, "write": True}

        summary = finalize_run(run, "implementer", role, "implementation", self.repo, 0, 1)

        self.assertEqual("success", summary["status"])
        self.assertEqual(1, summary["unrelated_failed_command_count"])

    def test_read_only_summary_includes_last_unrelated_failed_command_without_status_change(self):
        run = self.root / "read-only-command-failure-run"
        run.mkdir()
        (run / "events.jsonl").write_text("".join(json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution", "command": command, "status": "failed",
                "exit_code": exit_code,
            },
        }) + "\n" for command, exit_code in (("false", 1), ("tool --bad argument", 7))))
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "reviewed", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "reviewer", role, "review", self.repo, 0, 1)

        self.assertEqual("success", summary["status"])
        self.assertEqual(2, summary["unrelated_failed_command_count"])
        self.assertEqual({"command": "tool --bad argument", "exit_code": 7}, summary["last_unrelated_failed_command"])
        self.assertIn("last_unrelated_failed_command: tool --bad argument (exit 7)", (run / "summary.txt").read_text())

    def test_isolate_policy_does_not_inspect_diff_details_before_creating_worktree(self):
        run = self.root / "isolate-prepare-run"
        run.mkdir()
        (self.repo / "user-change.txt").write_text("mine\n")
        config = default_config()
        config["dirty_worktree_policy"] = "isolate"
        role = config["roles"]["implementer"]
        isolated = self.root / "isolated"

        with patch.object(runner, "_diff_details") as diff_details, patch.object(
            runner, "_create_isolated_worktree", return_value=isolated,
        ) as create_worktree:
            actual = runner._prepare_write_execution(
                config, "implementer", role, "implementation", self.repo, self.home, run,
            )

        self.assertEqual(isolated, actual)
        diff_details.assert_not_called()
        create_worktree.assert_called_once_with(self.repo, run)

    def test_second_write_prepare_forces_isolated_worktree_when_root_lock_is_held(self):
        runtime_root = self.root / "runtime"
        first_run = self.root / "first-run"
        second_run = self.root / "second-run"
        first_run.mkdir()
        second_run.mkdir()
        config = default_config()
        role = config["roles"]["implementer"]

        try:
            self.assertEqual(
                self.repo,
                runner._prepare_write_execution(
                    config, "implementer", role, "implementation", self.repo,
                    runtime_root, first_run,
                ),
            )
            isolated = runner._prepare_write_execution(
                config, "implementer", role, "implementation", self.repo,
                runtime_root, second_run,
            )
        finally:
            runner._release_root_lock(runtime_root, self.repo)

        self.assertNotEqual(self.repo, isolated)
        self.assertEqual(isolated, Path((second_run / "ISOLATED_WORKTREE").read_text().strip()))
        lock_path = runner._root_lock_path(runtime_root, self.repo)
        self.assertTrue(lock_path.is_file())
        self.assertTrue(lock_path.is_relative_to(runtime_root))
        self.assertFalse(lock_path.is_relative_to(self.repo))

    def _adopt_fixture(self, name="adopt"):
        runtime_root = self.home / ".local/state/cross-harness"
        run = runtime_root / "runs" / f"{name}-run"
        run.mkdir(parents=True)
        worktree = run / "worktree"
        git(self.repo, "worktree", "add", "--detach", str(worktree), "HEAD")
        (run / "ISOLATED_WORKTREE").write_text(str(worktree) + "\n")
        (run / "summary.json").write_text(json.dumps({"status": "success"}))
        (run / "summary.txt").write_text("success\n")
        (run / "state.json").write_text(json.dumps({"status": "success"}))
        return run, worktree

    def test_adopt_preserves_non_ascii_and_special_tracked_paths(self):
        file_name = '日本語 "quoted" [x].txt'
        (self.repo / file_name).write_text("before\n")
        git(self.repo, "add", file_name)
        git(self.repo, "commit", "-m", "special path")
        run, worktree = self._adopt_fixture("adopt-special-path")
        try:
            (worktree / file_name).write_text("adopted\n")

            result = adopt(run, home=self.home)

            self.assertEqual([file_name], result["changed_files"])
            self.assertEqual("adopted\n", (self.repo / file_name).read_text())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_treats_colon_and_glob_paths_as_literal(self):
        file_names = (":weird.txt", "*.glob.txt")
        for file_name in file_names:
            (self.repo / file_name).write_text("before\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "colon and glob paths")
        run, worktree = self._adopt_fixture("adopt-literal-paths")
        try:
            for file_name in file_names:
                (worktree / file_name).write_text("adopted\n")

            result = adopt(run, home=self.home)

            self.assertEqual(sorted(file_names), result["changed_files"])
            for file_name in file_names:
                self.assertEqual("adopted\n", (self.repo / file_name).read_text())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_accepts_a_retry_run_worktree_marker(self):
        config = self.root / "isolate-adopt.toml"
        default = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(default.replace('dirty_worktree_policy = "allow_delegated"', 'dirty_worktree_policy = "isolate"', 1))
        self.task.write_text("# Goal\nContinue.\n\n# Checks\n- fixture\n")
        verify_result = (Path("/usr/bin/true"), False)

        def fail_in_isolated_worktree(command, task, env, cwd, run_dir, timeout):
            (cwd / "delegated.txt").write_text("delegated\n")
            (run_dir / "events.jsonl").write_text('{"type":"turn.failed","error":{"message":"fixture failure"}}\n')
            (run_dir / "stderr.log").write_text("fixture failure")
            (run_dir / "final.json").write_text(json.dumps({
                "status": "failed", "work_completed": "", "changed_files": [],
                "tests": [], "error": "fixture failure", "next_decision": None,
            }))
            return 1

        calls = []

        def invoke(*args):
            calls.append(args)
            return fail_in_isolated_worktree(*args) if len(calls) == 1 else self._successful_retry(*args)

        with patch("cross_harness.runner.verify_codex_chatgpt", return_value=verify_result), patch(
            "cross_harness.runner.verify_codex_config_ownership"
        ), patch("cross_harness.runner._invoke_safe", side_effect=invoke):
            failed = runner.delegate(
                "implementer", "implementation", self.task, self.repo,
                config_path=config, home=self.home,
            )
            retried = adopt(
                Path(runner.retry(Path(failed["run_dir"]), self.task, config_path=config, home=self.home)["run_dir"]),
                config_path=config, home=self.home,
            )

        self.assertEqual(["delegated.txt"], retried["changed_files"])
        self.assertEqual("delegated\n", (self.repo / "delegated.txt").read_text())

    def test_adopt_rejects_marker_outside_run_directory(self):
        run, worktree = self._adopt_fixture("adopt-outside-marker")
        try:
            outside = self.root / "outside-worktree"
            (run / "ISOLATED_WORKTREE").write_text(str(outside) + "\n")

            with self.assertRaisesRegex(HarnessError, "outside run directory"):
                adopt(run, home=self.home)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_rejects_submodule_changes_with_a_clear_error(self):
        subrepo = self.root / "subrepo"
        subrepo.mkdir()
        git(subrepo, "init")
        git(subrepo, "config", "user.email", "test@example.com")
        git(subrepo, "config", "user.name", "Test")
        (subrepo / "sub.txt").write_text("before\n")
        git(subrepo, "add", ".")
        git(subrepo, "commit", "-m", "submodule initial")
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", str(subrepo), "sub")
        git(self.repo, "commit", "-m", "submodule")
        (subrepo / "sub.txt").write_text("after\n")
        git(subrepo, "add", ".")
        git(subrepo, "commit", "-m", "submodule update")
        new_submodule_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=subrepo, check=True, capture_output=True, text=True
        ).stdout.strip()
        run, worktree = self._adopt_fixture("adopt-submodule")
        try:
            git(worktree, "update-index", "--add", "--cacheinfo", f"160000,{new_submodule_head},sub")

            with self.assertRaisesRegex(HarnessError, "does not support submodules"):
                adopt(run, home=self.home)

            self.assertEqual("before\n", (self.repo / "sub" / "sub.txt").read_text())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_rejects_new_submodule_changes_before_writing_the_root(self):
        subrepo = self.root / "new-subrepo"
        subrepo.mkdir()
        git(subrepo, "init")
        git(subrepo, "config", "user.email", "test@example.com")
        git(subrepo, "config", "user.name", "Test")
        (subrepo / "sub.txt").write_text("before\n")
        git(subrepo, "add", ".")
        git(subrepo, "commit", "-m", "submodule initial")
        submodule_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=subrepo, check=True, capture_output=True, text=True
        ).stdout.strip()
        run, worktree = self._adopt_fixture("adopt-new-submodule")
        try:
            (worktree / "README.md").write_text("adopted\n")
            git(worktree, "update-index", "--add", "--cacheinfo", f"160000,{submodule_head},sub")

            with self.assertRaisesRegex(HarnessError, "does not support submodules"):
                adopt(run, home=self.home)

            self.assertEqual("before\n", (self.repo / "README.md").read_text())
            self.assertFalse((self.repo / "sub").exists())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_applies_tracked_and_untracked_changes_and_records_fingerprints(self):
        run, worktree = self._adopt_fixture()
        try:
            (worktree / "README.md").write_text("adopted\n")
            (worktree / "new.txt").write_text("new\n")

            result = adopt(run, home=self.home)

            self.assertEqual(["README.md", "new.txt"], result["changed_files"])
            self.assertEqual("adopted\n", (self.repo / "README.md").read_text())
            self.assertEqual("new\n", (self.repo / "new.txt").read_text())
            records = json.loads(
                (self.home / ".local/state/cross-harness/delegated-changes.json").read_text()
            )
            self.assertEqual(
                {
                    "README.md": sha256(self.repo / "README.md"),
                    "new.txt": sha256(self.repo / "new.txt"),
                },
                records[str(self.repo.resolve())],
            )
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_uses_git_filter_comparison_for_a_clean_root_file(self):
        git(self.repo, "config", "filter.roundtrip.clean", "sed s/SMUDGE$//")
        git(self.repo, "config", "filter.roundtrip.smudge", "sed s/$/SMUDGE/")
        (self.repo / ".gitattributes").write_text("f.txt filter=roundtrip\n")
        (self.repo / "f.txt").write_text("base\n")
        git(self.repo, "add", ".gitattributes", "f.txt")
        git(self.repo, "commit", "-m", "filtered file")
        git(self.repo, "rm", "f.txt")
        git(self.repo, "checkout", "HEAD", "--", "f.txt")
        self.assertEqual(b"baseSMUDGE\n", (self.repo / "f.txt").read_bytes())
        run, worktree = self._adopt_fixture("adopt-filter")
        try:
            (worktree / "f.txt").write_text("adopted\n")

            result = adopt(run, home=self.home)

            self.assertEqual(["f.txt"], result["changed_files"])
            self.assertEqual("adopted\n", (self.repo / "f.txt").read_text())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_ignores_umask_only_mode_difference(self):
        run, worktree = self._adopt_fixture("adopt-mode")
        try:
            (worktree / "README.md").write_text("adopted\n")
            (worktree / "README.md").chmod(0o664)

            result = adopt(run, home=self.home)

            self.assertEqual(["README.md"], result["changed_files"])
            self.assertEqual("adopted\n", (self.repo / "README.md").read_text())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_handles_tracked_symlink_changes(self):
        (self.repo / "target.txt").write_text("target\n")
        (self.repo / "link").symlink_to("target.txt")
        git(self.repo, "add", "target.txt", "link")
        git(self.repo, "commit", "-m", "symlink")
        run, worktree = self._adopt_fixture("adopt-symlink")
        try:
            (worktree / "link").unlink()
            (worktree / "link").symlink_to("README.md")

            result = adopt(run, home=self.home)

            self.assertEqual(["link"], result["changed_files"])
            self.assertEqual("README.md", os.readlink(self.repo / "link"))
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_rejects_a_run_without_a_finalized_summary(self):
        run, worktree = self._adopt_fixture("adopt-incomplete")
        try:
            (run / "summary.json").unlink()

            with self.assertRaisesRegex(HarnessError, "summary is not finalized"):
                adopt(run, home=self.home)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_rejects_a_live_run(self):
        run, worktree = self._adopt_fixture("adopt-running")
        try:
            (run / "supervisor.pid").write_text(f"{os.getpid()}\n")

            with self.assertRaisesRegex(HarnessError, "run is still in progress"):
                adopt(run, home=self.home)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_rejects_a_live_run_sharing_the_isolated_worktree(self):
        run, worktree = self._adopt_fixture("adopt-shared-live")
        live_run = run.parent / "adopt-shared-live-other-run"
        live_run.mkdir()
        try:
            (live_run / "ISOLATED_WORKTREE").write_text(str(worktree) + "\n")
            (live_run / "supervisor.pid").write_text(f"{os.getpid()}\n")
            (worktree / "new.txt").write_text("new\n")

            with self.assertRaisesRegex(HarnessError, "another live run shares"):
                adopt(run, home=self.home)

            self.assertFalse((self.repo / "new.txt").exists())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_rejects_when_shared_worktree_runs_cannot_be_enumerated(self):
        run, worktree = self._adopt_fixture("adopt-shared-enumeration-error")
        try:
            with patch.object(Path, "iterdir", side_effect=OSError("runs unavailable")):
                with self.assertRaisesRegex(HarnessError, "could not inspect runs"):
                    runner._adopt_reject_live_shared_worktree(
                        self.home / ".local/state/cross-harness", run, worktree
                    )
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_rejects_when_shared_worktree_marker_cannot_be_read(self):
        runtime_root = self.home / ".local/state/cross-harness"
        runs_root = runtime_root / "runs"
        run = runs_root / "adopt-shared-marker-error-run"
        candidate = runs_root / "adopt-shared-marker-error-other-run"
        run.mkdir(parents=True)
        candidate.mkdir()
        worktree = runs_root / "adopt-shared-marker-error-run" / "worktree"
        marker = candidate / "ISOLATED_WORKTREE"
        marker.write_text(str(worktree) + "\n")
        try:
            with patch.object(Path, "read_text", side_effect=OSError("marker unavailable")):
                with self.assertRaisesRegex(HarnessError, "could not read shared-worktree marker"):
                    runner._adopt_reject_live_shared_worktree(runtime_root, run, worktree)
        finally:
            marker.unlink()
            candidate.rmdir()
            run.rmdir()

    def test_adopt_rollback_does_not_erase_a_parallel_record(self):
        run, worktree = self._adopt_fixture("adopt-rollback")
        runtime_root = self.home / ".local/state/cross-harness"
        ready = self.root / "adopt-rollback-ready"
        try:
            (worktree / "new.txt").write_text("new\n")
            records_path = runtime_root / "delegated-changes.json"
            records_path.parent.mkdir(parents=True, exist_ok=True)
            records_path.write_text(json.dumps({str(self.repo.resolve()): {}}))
            record_changes_locked = runner._record_delegated_changes_locked

            def fail_after_parallel_record(*args, **kwargs):
                pid = os.fork()
                if pid == 0:
                    ready.touch()
                    try:
                        descriptor = runner._acquire_delegated_changes_lock(runtime_root)
                        try:
                            record_changes_locked(
                            runtime_root, self.root, self.repo,
                            [{"file": "parallel.txt", "fingerprint": "parallel"}],
                            [{"file": "parallel.txt", "fingerprint": "parallel"}],
                            )
                        finally:
                            runner._release_lock(descriptor)
                    except BaseException:
                        os._exit(1)
                    os._exit(0)
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                raise OSError("adoption write failed")

            with patch.object(runner, "_record_delegated_changes_locked", side_effect=fail_after_parallel_record):
                with self.assertRaisesRegex(HarnessError, "adopt failed"):
                    adopt(run, home=self.home)

            _, status = os.waitpid(-1, 0)
            self.assertEqual(0, os.waitstatus_to_exitcode(status))
            records = json.loads(records_path.read_text())
            self.assertEqual("parallel", records[str(self.repo.resolve())]["parallel.txt"])
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_reports_conflicts_without_changing_root_worktree(self):
        run, worktree = self._adopt_fixture("adopt-conflict")
        try:
            (worktree / "README.md").write_text("isolated\n")
            (self.repo / "README.md").write_text("user\n")

            with self.assertRaisesRegex(HarnessError, r"conflict[\s\S]*README\.md"):
                adopt(run, home=self.home)

            self.assertEqual("user\n", (self.repo / "README.md").read_text())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_blocks_when_root_lock_is_owned(self):
        run, worktree = self._adopt_fixture("adopt-lock")
        runtime_root = self.home / ".local/state/cross-harness"
        try:
            (worktree / "new.txt").write_text("new\n")
            self.assertTrue(runner._acquire_root_lock(runtime_root, self.repo))
            try:
                with self.assertRaisesRegex(HarnessError, "another write delegation owns the root worktree"):
                    adopt(run, home=self.home)
            finally:
                runner._release_root_lock(runtime_root, self.repo)
            self.assertFalse((self.repo / "new.txt").exists())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_adopt_rolls_back_when_post_write_verification_reports_missing_paths(self):
        run, worktree = self._adopt_fixture("adopt-missing-path")
        original_diff_details = runner._diff_details
        try:
            (worktree / "new.txt").write_text("new\n")
            with patch.object(
                runner,
                "_diff_details",
                side_effect=[original_diff_details(worktree), ("", [], [])],
            ):
                with self.assertRaisesRegex(HarnessError, "missing paths:[\\s\\S]*new\\.txt"):
                    adopt(run, home=self.home)

            self.assertFalse((self.repo / "new.txt").exists())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, check=True)

    def test_last_declared_check_execution_controls_the_result(self):
        run = self.root / "last-check-execution-run"
        run.mkdir()
        (run / "task.md").write_text("# Checks\n- scripts/test.sh\n")
        (run / "events.jsonl").write_text("".join(json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution", "command": "scripts/test.sh", "status": status,
                "exit_code": exit_code,
            },
        }) + "\n" for status, exit_code in (("completed", 0), ("failed", 2))))
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        self.assertEqual("failed", summary["status"])
        self.assertEqual(2, summary["checks"][0]["exit_code"])

    def test_piped_declared_check_does_not_mask_prior_failure(self):
        run = self.root / "piped-check-fail-open-run"
        run.mkdir()
        (run / "task.md").write_text("# Checks\n- ./scripts/test.sh\n")
        (run / "events.jsonl").write_text("".join(json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution", "command": command, "status": status,
                "exit_code": exit_code,
            },
        }) + "\n" for command, status, exit_code in (
            ("./scripts/test.sh", "failed", 1),
            ("./scripts/test.sh 2>&1 | tail -100", "completed", 0),
        )))
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        self.assertEqual("failed", summary["status"])
        self.assertEqual(1, summary["checks"][0]["exit_code"])

    def test_declared_check_failure_overrides_success_for_write_role(self):
        run = self.root / "write-declared-check-failure-run"
        run.mkdir()
        (run / "task.md").write_text("# Checks\n- scripts/test.sh\n")
        (run / "events.jsonl").write_text(json.dumps({
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "scripts/test.sh", "status": "failed", "exit_code": 2},
        }) + "\n")
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "implemented", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-terra", "effort": "medium", "output_limit_chars": 8000, "write": True}

        summary = finalize_run(run, "implementer", role, "implementation", self.repo, 0, 1)

        self.assertEqual("failed", summary["status"])
        self.assertIn("declared check failed: scripts/test.sh (exit 2)", summary["error"])

    def test_unrun_declared_check_downgrades_success_to_partial(self):
        run = self.root / "unrun-declared-check-run"
        run.mkdir()
        (run / "task.md").write_text("# Checks\n- scripts/test.sh\n")
        (run / "events.jsonl").write_text("")
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        self.assertEqual("partial", summary["status"])
        self.assertEqual("not_run", summary["checks"][0]["status"])

    def test_policy_denied_declared_check_is_not_run(self):
        run = self.root / "policy-denied-declared-check-run"
        run.mkdir()
        (run / "task.md").write_text("# Checks\n- scripts/test.sh\n")
        (run / "events.jsonl").write_text(json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution", "command": "scripts/test.sh", "status": "failed", "exit_code": 1,
                "aggregated_output": "PreToolUse:Bash hook error: [/Users/example/.local/bin/cross-harness hook claude-pre-tool-use]: cross-harness: nested executor launch from delegated Claude is blocked",
            },
        }) + "\n")
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        self.assertEqual("partial", summary["status"])
        self.assertEqual(0, summary["unrelated_failed_command_count"])

    def test_write_role_git_restore_downgrades_success_to_partial(self):
        run = self.root / "git-restore-run"
        run.mkdir()
        (run / "events.jsonl").write_text(json.dumps({
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "git restore README.md", "status": "completed", "exit_code": 0},
        }) + "\n")
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-terra", "effort": "medium", "output_limit_chars": 8000, "write": True}

        summary = finalize_run(run, "implementer", role, "implementation", self.repo, 0, 1)

        self.assertEqual("partial", summary["status"])
        self.assertEqual("README.md", summary["self_reversions"][0]["target"])
        self.assertIn("tracked files restored from Git: README.md", summary["error"])

    def test_check_with_missing_exit_code_is_not_run(self):
        run = self.root / "missing-check-exit-run"
        run.mkdir()
        (run / "task.md").write_text("# Checks\n- scripts/test.sh\n")
        (run / "events.jsonl").write_text(json.dumps({
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "scripts/test.sh", "exit_code": None},
        }) + "\n")
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        self.assertEqual("partial", summary["status"])
        self.assertEqual("not_run", summary["checks"][0]["status"])

    def test_reading_check_text_does_not_override_a_failed_check_execution(self):
        run = self.root / "read-check-text-run"
        run.mkdir()
        (run / "task.md").write_text("# Checks\n- scripts/test.sh\n")
        events = [
            {"command": '/bin/zsh -lc "scripts/test.sh"', "status": "failed", "exit_code": 1},
            {"command": '/bin/zsh -lc "cat scripts/test.sh"', "status": "completed", "exit_code": 0},
        ]
        (run / "events.jsonl").write_text("".join(json.dumps({
            "type": "item.completed", "item": {"type": "command_execution", **event},
        }) + "\n" for event in events))
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        self.assertEqual("failed", summary["status"])
        self.assertEqual(1, summary["checks"][0]["exit_code"])

    def test_execution_kind_without_declared_checks_is_unverified_partial(self):
        run = self.root / "undeclared-checks-run"
        run.mkdir()
        (run / "events.jsonl").write_text("")
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)

        self.assertEqual("partial", summary["status"])
        self.assertIn("no checks declared; outcome could not be verified", summary["error"])
        self.assertIn("task create --check", summary["error"])

    def test_stash_pop_and_apply_are_not_self_reversions(self):
        events = [
            {"command": "git stash pop", "exit_code": 0},
            {"command": "git stash apply", "exit_code": 0},
        ]

        self.assertEqual([], _self_reversions(self.repo, events))

    def test_self_reversion_error_is_retained_for_failed_status(self):
        run = self.root / "failed-with-reversion-run"
        run.mkdir()
        (run / "task.md").write_text("# Checks\n- scripts/test.sh\n")
        (run / "events.jsonl").write_text(json.dumps({
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "git restore README.md", "exit_code": 0},
        }) + "\n")
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "failed", "work_completed": "failed", "changed_files": [],
            "tests": [], "error": "check failed", "next_decision": None,
        }))
        role = {"model": "gpt-5.6-terra", "effort": "medium", "output_limit_chars": 8000, "write": True}

        summary = finalize_run(run, "implementer", role, "implementation", self.repo, 0, 1)

        self.assertEqual("failed", summary["status"])
        self.assertIn("tracked files restored from Git: README.md", summary["error"])

    def test_self_reversions_detects_multiline_git_c_show_redirect_by_basename(self):
        report = self.repo / "mics/j1/j1_report.pdf"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"original pdf\n")
        git(self.repo, "add", "mics/j1/j1_report.pdf")
        git(self.repo, "commit", "-m", "track report")
        command = '''/bin/zsh -lc "git show HEAD:j1_report.aux >/dev/null 2>&1 || true
git -C /Users/itoutaisei/uec/Latex show HEAD:mics/j1/j1_report.aux > j1_report.aux
git -C /Users/itoutaisei/uec/Latex show HEAD:mics/j1/j1_report.log > j1_report.log
git -C /Users/itoutaisei/uec/Latex show HEAD:mics/j1/j1_report.out > j1_report.out
git -C /Users/itoutaisei/uec/Latex show HEAD:mics/j1/j1_report.pdf > j1_report.pdf
git -C /Users/itoutaisei/uec/Latex show HEAD:mics/j1/j1_report.synctex.gz > j1_report.synctex.gz"'''
        events = [{"command": command, "exit_code": 0}]

        reversions = _self_reversions(self.repo, events)

        self.assertIn({
            "command": command,
            "source": "git show HEAD redirect",
            "target": "j1_report.pdf",
        }, reversions)

    def test_self_reversions_loads_tracked_files_at_most_once_per_finalize_run(self):
        for name in ("first.txt", "second.txt"):
            path = self.repo / "reports" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(name + "\n")
        git(self.repo, "add", "reports")
        git(self.repo, "commit", "-m", "track reports")
        command = '''/bin/zsh -lc "printf 'checking reports\\n'
git -C /Users/itoutaisei/uec/Latex show HEAD:reports/first.txt > first.txt
git -C /Users/itoutaisei/uec/Latex show HEAD:reports/second.txt > second.txt"'''
        events = [{"command": command, "exit_code": 0}]
        original_git = runner._git
        full_ls_files_calls = 0

        def counting_git(cwd, args, timeout=30):
            nonlocal full_ls_files_calls
            if args == ["ls-files"]:
                full_ls_files_calls += 1
            return original_git(cwd, args, timeout)

        with patch("cross_harness.runner._git", side_effect=counting_git):
            reversions = _self_reversions(self.repo, events)

        self.assertEqual(1, full_ls_files_calls)
        self.assertEqual(["first.txt", "second.txt"], [item["target"] for item in reversions])

    def test_finalize_run_marks_self_reversion_check_unavailable_when_git_fails(self):
        command = '''/bin/zsh -lc "printf 'before check\\n'
git -C /Users/itoutaisei/uec/Latex show HEAD:README.md > README.md"'''
        role = {"model": "gpt-5.6-terra", "effort": "medium", "output_limit_chars": 8000, "write": True}
        original_git = runner._git

        for failure in ("timeout", "oserror"):
            with self.subTest(failure=failure):
                run = self.root / f"self-reversion-{failure}-run"
                run.mkdir()
                (run / "events.jsonl").write_text(json.dumps({
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution", "command": command,
                        "status": "completed", "exit_code": 0,
                    },
                }) + "\n")
                (run / "stderr.log").write_text("")
                (run / "final.json").write_text(json.dumps({
                    "status": "success", "work_completed": "done", "changed_files": [],
                    "tests": [], "error": None, "next_decision": None,
                }))
                failed = False

                def failing_git(cwd, args, timeout=30):
                    nonlocal failed
                    if not failed and args[0] == "ls-files":
                        failed = True
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired(["git", *args], timeout)
                        raise OSError("git executable unavailable")
                    return original_git(cwd, args, timeout)

                with patch("cross_harness.runner._git", side_effect=failing_git):
                    summary = finalize_run(run, "implementer", role, "implementation", self.repo, 0, 1)

                self.assertEqual("unavailable", summary["self_reversion_check"])
                self.assertTrue((run / "summary.txt").exists())
                self.assertTrue((run / "summary.json").exists())
                self.assertIn("self_reversion_check: unavailable", (run / "summary.txt").read_text())
                self.assertEqual(
                    "unavailable",
                    json.loads((run / "summary.json").read_text())["self_reversion_check"],
                )

    def test_finalize_run_marks_diff_check_unavailable_when_all_git_calls_fail(self):
        role = {"model": "gpt-5.6-terra", "effort": "medium", "output_limit_chars": 8000, "write": True}

        for failure in ("timeout", "oserror"):
            with self.subTest(failure=failure):
                run = self.root / f"diff-check-{failure}-run"
                run.mkdir()
                (run / "events.jsonl").write_text("")
                (run / "stderr.log").write_text("")
                (run / "final.json").write_text(json.dumps({
                    "status": "success", "work_completed": "done", "changed_files": [],
                    "tests": [], "error": None, "next_decision": None,
                }))

                def failing_git(cwd, args, timeout=30):
                    if failure == "timeout":
                        raise subprocess.TimeoutExpired(["git", *args], timeout)
                    raise OSError("git executable unavailable")

                with patch("cross_harness.runner._git", side_effect=failing_git):
                    summary = finalize_run(run, "implementer", role, "implementation", self.repo, 0, 1)

                self.assertEqual("unavailable", summary["diff_check"])
                self.assertEqual([], summary["changed_files"])
                self.assertEqual([], summary["unreported_changed_files"])
                self.assertEqual([], summary["diff_summary"])
                self.assertTrue((run / "summary.txt").exists())
                self.assertTrue((run / "summary.json").exists())
                self.assertIn("diff_check: unavailable", (run / "summary.txt").read_text())
                self.assertNotIn("unreported_changed_files:", (run / "summary.txt").read_text())
                self.assertEqual(
                    "unavailable",
                    json.loads((run / "summary.json").read_text())["diff_check"],
                )

    def test_finalize_run_survives_delegated_change_recording_git_failure(self):
        runtime_root = self.root / "runtime"
        run = self.root / "record-failure-run"
        run.mkdir()
        (run / "events.jsonl").write_text("")
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-terra", "effort": "medium", "output_limit_chars": 8000, "write": True}

        with patch("cross_harness.runner._record_delegated_changes", side_effect=OSError("git unavailable")):
            summary = finalize_run(
                run, "implementer", role, "review", self.repo, 0, 1,
                runtime_root=runtime_root, dirty_worktree_policy="allow_delegated",
            )

        self.assertEqual("unavailable", summary["diff_check"])
        self.assertEqual([], summary["changed_files"])
        self.assertEqual([], summary["diff_summary"])
        self.assertFalse((runtime_root / "delegated-changes.json").exists())
        self.assertTrue((run / "summary.txt").exists())
        self.assertTrue((run / "summary.json").exists())
        self.assertIn("diff_check: unavailable", (run / "summary.txt").read_text())

    def test_task_file_with_credential_material_is_rejected(self):
        self.task.write_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n")
        with self.assertRaises(HarnessError):
            delegate("tester", "test", self.task, self.repo, home=self.home)

    def test_active_executor_cannot_nest_wrapper_delegation(self):
        with patch.dict(os.environ, {"CROSS_HARNESS_ACTIVE": "1"}):
            with self.assertRaisesRegex(HarnessError, "nested cross-harness"):
                delegate("tester", "test", self.task, self.repo, home=self.home)

    def test_claude_command_uses_headless_streaming_and_minimal_permissions(self):
        run = self.root / "claude-run"
        agents = self.home / ".claude/agents"
        agents.mkdir(parents=True)
        (agents / "cross-harness-reviewer.md").write_text(
            "---\nname: cross-harness-reviewer\ndescription: Ignore this metadata\n"
            "tools: Read\nmodel: opus\neffort: low\n---\nReview the supplied diff only.\n"
        )
        read_only = {
            "harness": "claude", "model": "sonnet", "effort": "high", "write": False,
        }
        command = _claude_command(Path("/usr/local/bin/claude"), "reviewer", read_only, self.repo, run, agents)
        self.assertEqual("-p", command[1])
        self.assertEqual("stream-json", command[command.index("--output-format") + 1])
        self.assertIn("--verbose", command)
        self.assertEqual("manual", command[command.index("--permission-mode") + 1])
        self.assertNotIn("--agent", command)
        self.assertEqual("Bash,Read,Grep,Glob", command[command.index("--allowedTools") + 1])
        claude_schema = json.loads(command[command.index("--json-schema") + 1])
        self.assertNotIn("$schema", claude_schema)
        self.assertFalse(claude_schema["additionalProperties"])
        self.assertEqual(["string", "null"], claude_schema["properties"]["error"]["type"])
        disallowed_index = command.index("--disallowed-tools")
        self.assertEqual(["Edit", "Write", "NotebookEdit"], command[disallowed_index + 1:disallowed_index + 4])
        self.assertEqual("sonnet", command[command.index("--model") + 1])
        self.assertNotIn("-C", command)
        self.assertNotIn("bypassPermissions", command)
        instruction = command[command.index("--append-system-prompt") + 1]
        self.assertIn("Cross-harness executor", instruction)
        self.assertIn("Do not follow the orchestrator charter", instruction)
        self.assertIn("exactly these six fields", instruction)
        self.assertIn("status (one of success, failed, blocked, partial)", instruction)
        self.assertIn("work_completed (string)", instruction)
        self.assertIn("changed_files (array of strings)", instruction)
        self.assertIn("tests (array of strings)", instruction)
        self.assertIn("error (string or null)", instruction)
        self.assertIn("next_decision (string or null)", instruction)
        self.assertIn("only a JSON object", instruction)
        self.assertIn("Do not write the result to a file", instruction)
        self.assertNotIn(str(run / "final.json"), instruction)
        self.assertIn("Review the supplied diff only.", instruction)
        self.assertNotIn("description: Ignore this metadata", instruction)
        self.assertNotIn("tools: Read", instruction)
        self.assertNotIn("model: opus", instruction)
        self.assertNotIn("effort: low", instruction)

        resumed = _claude_command(Path("/usr/local/bin/claude"), "reviewer", read_only, self.repo, run, agents, "session-1")
        self.assertEqual("session-1", resumed[resumed.index("--resume") + 1])

        writable = dict(read_only, write=True)
        write_command = _claude_command(Path("/usr/local/bin/claude"), "implementer", writable, self.repo, run, agents)
        self.assertNotIn("--agent", write_command)
        self.assertEqual("manual", write_command[write_command.index("--permission-mode") + 1])
        allowed = write_command[write_command.index("--allowedTools") + 1]
        self.assertEqual(
            f"Bash,Read,Grep,Glob,Edit(//{self.repo.resolve().as_posix().lstrip('/')}/**),Write(//{self.repo.resolve().as_posix().lstrip('/')}/**)",
            allowed,
        )
        self.assertNotIn("--disallowed-tools", write_command)
        self.assertNotIn("--settings", write_command)
        self.assertNotIn(str((self.root / "outside").resolve()), allowed)

        (agents / "cross-harness-security_reviewer.md").write_text(
            "---\nname: cross-harness-security_reviewer\n---\n"
        )
        security_command = _claude_command(
            Path("/usr/local/bin/claude"), "security_reviewer", read_only, self.repo, run, agents
        )
        self.assertNotIn("--agent", security_command)

    def test_claude_command_without_installed_agent_uses_executor_charter(self):
        run = self.root / "claude-uninstalled-agent-run"
        agents = self.home / ".claude/agents"
        role = {"harness": "claude", "model": "sonnet", "effort": "high", "write": False}

        command = _claude_command(Path("/usr/local/bin/claude"), "reviewer", role, self.repo, run, agents)

        self.assertNotIn("--agent", command)
        instruction = command[command.index("--append-system-prompt") + 1]
        self.assertIn("Cross-harness executor", instruction)
        self.assertIn("Do not ask the user questions", instruction)
        self.assertIn("exactly these six fields", instruction)

    def test_codex_resume_reapplies_sandbox_through_config_override(self):
        run = self.root / "codex-resume"
        read_only = {"harness": "codex", "model": "gpt-5.6-luna", "effort": "low", "write": False}
        command = _codex_command(Path("/usr/local/bin/codex"), read_only, self.repo, run, "thread-1")
        self.assertNotIn("--sandbox", command)
        self.assertNotIn("-C", command)
        self.assertIn('sandbox_mode="read-only"', command)
        self.assertIn('model_reasoning_summary="detailed"', command)

        fresh = _codex_command(Path("/usr/local/bin/codex"), read_only, self.repo, run)
        self.assertIn('model_reasoning_summary="detailed"', fresh)

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner.verify_claude_config_ownership")
    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner._invoke_safe")
    def test_claude_delegation_selects_claude_auth_and_executor(self, invoke, verify_claude, claude_ownership, ownership, verify_codex):
        verify_claude.return_value = (Path("/usr/local/bin/claude"), False)

        def complete(command, task, env, cwd, run_dir, timeout):
            self.assertEqual("claude", env["CROSS_HARNESS_EXECUTOR"])
            self.assertEqual(
                env["CROSS_HARNESS_PARENT"],
                json.loads((run_dir / "execution.json").read_text())["parent_harness"],
            )
            self.assertEqual("manual", command[command.index("--permission-mode") + 1])
            self.assertNotIn("--agent", command)
            (run_dir / "events.jsonl").write_text(
                '{"type":"result","session_id":"session-1","is_error":false,"usage":{"input_tokens":10,"output_tokens":2},"result":"{\\"status\\": \\"success\\", \\"work_completed\\": \\"reviewed\\", \\"changed_files\\": [\\"README.md\\"], \\"tests\\": [\\"review\\"], \\"error\\": null, \\"next_decision\\": \\"ship it\\"}"}\n'
            )
            (run_dir / "stderr.log").write_text("")
            return 0

        invoke.side_effect = complete
        summary = delegate("reviewer", "review", self.task, self.repo, home=self.home)
        run = Path(summary["run_dir"])
        self.assertEqual("success", summary["status"])
        self.assertEqual("session-1", summary["thread_id"])
        self.assertEqual("reviewed", summary["work_completed"])
        self.assertEqual([], summary["changed_files"])
        self.assertEqual(["README.md"], summary["reported_changed_files"])
        self.assertEqual(["README.md"], summary["unverified_changed_files"])
        self.assertEqual(["review"], summary["tests"])
        self.assertEqual("ship it", summary["next_decision"])
        self.assertTrue((run / "final.json").exists())
        self.assertEqual("claude", json.loads((run / "execution.json").read_text())["harness"])
        self.assertFalse(json.loads((run / "execution.json").read_text())["write"])
        verify_claude.assert_called_once()
        claude_ownership.assert_called_once_with(self.home.resolve(), self.repo.resolve())
        verify_codex.assert_not_called()
        ownership.assert_not_called()

    def test_claude_result_code_fence_creates_final_json(self):
        run = self.root / "claude-fenced-result"
        run.mkdir()
        result = {
            "status": "partial", "work_completed": "reviewed", "changed_files": [],
            "tests": [], "error": None, "next_decision": "follow up",
        }
        (run / "events.jsonl").write_text(json.dumps({
            "type": "result", "result": f"```json\n{json.dumps(result)}\n```",
        }) + "\n")

        _write_claude_final_from_events(run)

        self.assertEqual(result, json.loads((run / "final.json").read_text()))

    def test_claude_result_prose_followed_by_code_fence_creates_final_json(self):
        run = self.root / "claude-prose-fenced-result"
        run.mkdir()
        result = {
            "status": "partial", "work_completed": "reviewed", "changed_files": [],
            "tests": [], "error": None, "next_decision": "follow up",
        }
        (run / "events.jsonl").write_text(json.dumps({
            "type": "result",
            "result": f"I completed the review.\n\n```json\n{json.dumps(result)}\n```",
        }) + "\n")

        _write_claude_final_from_events(run)

        self.assertEqual(result, json.loads((run / "final.json").read_text()))

    def test_claude_structured_result_also_retains_last_assistant_text(self):
        run = self.root / "claude-structured-result-with-text"
        run.mkdir()
        result = {
            "status": "success", "work_completed": "test", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }
        (run / "events.jsonl").write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "ignore"},
                {"type": "text", "text": "first review"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": "final substantive review"},
            ]}}) + "\n" + json.dumps({"type": "result", "result": json.dumps(result)}) + "\n"
        )

        _write_claude_final_from_events(run)

        self.assertEqual(result, json.loads((run / "final.json").read_text()))
        self.assertEqual("final substantive review", (run / "final.txt").read_text())

    def test_existing_claude_final_json_still_retains_assistant_text(self):
        run = self.root / "claude-existing-structured-result"
        run.mkdir()
        result = {
            "status": "success", "work_completed": "placeholder", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }
        (run / "final.json").write_text(json.dumps(result))
        (run / "events.jsonl").write_text(json.dumps({
            "type": "assistant", "content": [
                {"type": "thinking", "thinking": "ignore"},
                {"type": "text", "text": "retained review text"},
            ],
        }) + "\n")

        _write_claude_final_from_events(run)

        self.assertEqual(result, json.loads((run / "final.json").read_text()))
        self.assertEqual("retained review text", (run / "final.txt").read_text())

    def test_claude_assistant_text_does_not_replace_json_fallback(self):
        run = self.root / "claude-json-fallback"
        run.mkdir()
        (run / "events.jsonl").write_text(
            json.dumps({"type": "assistant", "content": [{"type": "text", "text": "assistant review"}]})
            + "\n" + json.dumps({"type": "result", "result": "not JSON"}) + "\n"
        )

        _write_claude_final_from_events(run)

        self.assertEqual("not JSON", (run / "final.txt").read_text())

    def test_claude_final_extraction_ignores_unreadable_or_invalid_events(self):
        run = self.root / "claude-invalid-events"
        run.mkdir()
        (run / "events.jsonl").write_text('{not json}\n')

        _write_claude_final_from_events(run)

        self.assertFalse((run / "final.txt").exists())

    def test_unparseable_claude_result_does_not_create_final_json(self):
        run = self.root / "claude-invalid-result"
        run.mkdir()
        (run / "events.jsonl").write_text(
            '{"type":"result","is_error":false,"result":"not JSON"}\n'
        )
        (run / "stderr.log").write_text("")

        _write_claude_final_from_events(run)

        self.assertFalse((run / "final.json").exists())
        self.assertEqual("not JSON", (run / "final.txt").read_text())
        role = {"model": "sonnet", "effort": "high", "output_limit_chars": 8000, "write": False}
        summary = finalize_run(run, "reviewer", role, "review", self.repo, 0, 1)
        self.assertEqual("success", summary["status"])
        self.assertEqual("", summary["work_completed"])
        self.assertEqual(str(run / "final.txt"), summary["final_message"])
        self.assertEqual(str(run / "final.txt"), summary["final_text"])

    def test_finalize_run_has_no_final_message_without_final_artifact(self):
        run = self.root / "no-final-artifact"
        run.mkdir()
        (run / "events.jsonl").write_text("")
        (run / "stderr.log").write_text("")

        role = {"model": "sonnet", "effort": "high", "output_limit_chars": 8000, "write": False}
        summary = finalize_run(run, "reviewer", role, "review", self.repo, 0, 1)

        self.assertIsNone(summary["final_message"])
        self.assertIsNone(summary["final_text"])

    @patch("cross_harness.runner.verify_claude_config_ownership")
    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner._invoke_safe")
    def test_claude_write_role_records_write_authorization(self, invoke, verify_claude, claude_ownership):
        config = self.root / "claude-implementer.toml"
        contents = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(contents.replace(
            '[roles.implementer]\nharness = "codex"',
            '[roles.implementer]\nharness = "claude"',
            1,
        ))
        verify_claude.return_value = (Path("/usr/local/bin/claude"), False)

        def complete(command, task, env, cwd, run_dir, timeout):
            self.assertEqual("claude", env["CROSS_HARNESS_EXECUTOR"])
            self.assertEqual("manual", command[command.index("--permission-mode") + 1])
            self.assertNotIn("--agent", command)
            self.assertIn(f"Edit(//{cwd.resolve().as_posix().lstrip('/')}/**)", command[command.index("--allowedTools") + 1])
            self.assertIn(f"Write(//{cwd.resolve().as_posix().lstrip('/')}/**)", command[command.index("--allowedTools") + 1])
            (run_dir / "events.jsonl").write_text('{"type":"result","is_error":false,"usage":{}}\n')
            (run_dir / "stderr.log").write_text("")
            (run_dir / "final.json").write_text(json.dumps({
                "status": "success", "work_completed": "implemented", "changed_files": [],
                "tests": [], "error": None, "next_decision": None,
            }))
            return 0

        invoke.side_effect = complete
        summary = delegate("implementer", "implementation", self.task, self.repo, config_path=config, home=self.home)
        environment = invoke.call_args.args[2]
        run = Path(summary["run_dir"])
        self.assertEqual("claude", environment["CROSS_HARNESS_EXECUTOR"])
        self.assertEqual("1", environment["CROSS_HARNESS_WRITE"])
        self.assertTrue(json.loads((run / "execution.json").read_text())["write"])
        claude_ownership.assert_called_once_with(self.home.resolve(), self.repo.resolve())

    @patch("cross_harness.runner.shutil.which", return_value="/usr/bin/sandbox-exec")
    @patch("cross_harness.runner.sys.platform", "darwin")
    @patch("cross_harness.runner.verify_claude_config_ownership")
    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner._invoke_safe")
    def test_darwin_claude_write_role_is_contained_and_audited(
        self, invoke, verify_claude, claude_ownership, which
    ):
        config = self.root / "claude-darwin-implementer.toml"
        contents = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(contents.replace(
            '[roles.implementer]\nharness = "codex"',
            '[roles.implementer]\nharness = "claude"',
            1,
        ))
        verify_claude.return_value = (Path("/usr/local/bin/claude"), False)

        def complete(command, task, env, cwd, run_dir, timeout):
            self.assertEqual(["/usr/bin/sandbox-exec", "-f"], command[:2])
            self.assertEqual(str(run_dir / "sandbox-exec.sb"), command[2])
            self.assertEqual("/usr/local/bin/claude", command[3])
            (run_dir / "events.jsonl").write_text('{"type":"result","is_error":false,"usage":{}}\n')
            (run_dir / "stderr.log").write_text("")
            (run_dir / "final.json").write_text(json.dumps({
                "status": "success", "work_completed": "implemented", "changed_files": [],
                "tests": [], "error": None, "next_decision": None,
            }))
            return 0

        invoke.side_effect = complete
        summary = delegate("implementer", "implementation", self.task, self.repo, config_path=config, home=self.home)
        run = Path(summary["run_dir"])
        profile = (run / "sandbox-exec.sb").read_text()
        record = json.loads((run / "command.json").read_text())
        self.assertIn("(deny file-write*)", profile)
        self.assertIn(f'(allow file-write* (subpath "{self.repo.resolve()}"))', profile)
        self.assertIn(f'(allow file-write* (subpath "{(self.home / ".claude").resolve()}"))', profile)
        self.assertIn('(allow file-write-data (literal "/dev/null"))', profile)
        self.assertTrue(record["sandbox_exec"]["enabled"])
        self.assertEqual(str(run / "sandbox-exec.sb"), record["sandbox_exec"]["profile"])
        which.assert_called_once_with("sandbox-exec")

    @patch("cross_harness.runner.sys.platform", "linux")
    @patch("cross_harness.runner.verify_claude_config_ownership")
    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner._invoke_safe")
    def test_non_darwin_claude_write_role_records_disabled_containment(
        self, invoke, verify_claude, claude_ownership
    ):
        config = self.root / "claude-linux-implementer.toml"
        contents = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(contents.replace(
            '[roles.implementer]\nharness = "codex"',
            '[roles.implementer]\nharness = "claude"',
            1,
        ))
        verify_claude.return_value = (Path("/usr/local/bin/claude"), False)

        def complete(command, task, env, cwd, run_dir, timeout):
            self.assertEqual("/usr/local/bin/claude", command[0])
            (run_dir / "events.jsonl").write_text('{"type":"result","is_error":false,"usage":{}}\n')
            (run_dir / "stderr.log").write_text("")
            (run_dir / "final.json").write_text(json.dumps({
                "status": "success", "work_completed": "implemented", "changed_files": [],
                "tests": [], "error": None, "next_decision": None,
            }))
            return 0

        invoke.side_effect = complete
        summary = delegate("implementer", "implementation", self.task, self.repo, config_path=config, home=self.home)
        run = Path(summary["run_dir"])
        record = json.loads((run / "command.json").read_text())
        self.assertFalse(record["sandbox_exec"]["enabled"])
        self.assertEqual("platform_not_darwin", record["sandbox_exec"]["reason"])
        self.assertFalse((run / "sandbox-exec.sb").exists())

    @patch("cross_harness.runner.shutil.which", return_value=None)
    @patch("cross_harness.runner.sys.platform", "darwin")
    def test_missing_sandbox_exec_does_not_claim_containment(self, which):
        run = self.root / "sandbox-unavailable-run"
        run.mkdir()
        command, record = _contain_claude_write_command(
            ["/usr/local/bin/claude", "-p"],
            {"harness": "claude", "write": True},
            self.repo,
            run,
            self.home,
        )

        self.assertEqual(["/usr/local/bin/claude", "-p"], command)
        self.assertFalse(record["enabled"])
        self.assertEqual("sandbox_exec_unavailable", record["reason"])
        self.assertIsNone(record["profile"])
        self.assertFalse((run / "sandbox-exec.sb").exists())
        which.assert_called_once_with("sandbox-exec")

    def test_sandbox_profile_escapes_quote_and_backslash_in_paths(self):
        execution_root = self.root / 'root "quoted" \\ slash'
        home = self.root / 'home "quoted" \\ slash'

        profile = _claude_sandbox_profile(execution_root, home)

        escaped = str(execution_root.resolve()).replace('\\', '\\\\').replace('"', '\\"')
        self.assertIn('subpath "' + escaped + '"', profile)
        self.assertNotIn('subpath "' + str(execution_root) + '") (allow', profile)

    def test_retry_budget_exhaustion_stops_before_auth_or_executor(self):
        previous = self.root / "retry-exhausted"
        previous.mkdir()
        (previous / "state.json").write_text(json.dumps({
            "role": "tester", "kind": "test", "cwd": str(self.repo),
            "thread_id": "00000000-0000-0000-0000-000000000001",
            "attempts": 3, "signatures": ["one", "two", "three"],
            "escalated": True, "status": "failed",
            "model": "gpt-5.6-terra", "effort": "high",
        }))
        with patch("cross_harness.runner.verify_codex_chatgpt") as verify, patch("cross_harness.runner._invoke_safe") as invoke:
            with self.assertRaisesRegex(HarnessError, "retry budget exhausted"):
                retry(previous, self.task, home=self.home)
        verify.assert_not_called()
        invoke.assert_not_called()

    def test_retry_rejects_credential_task_files_before_creating_a_run(self):
        previous = self.root / "retry-credential-previous"
        previous.mkdir()
        (previous / "state.json").write_text(json.dumps({
            "role": "tester", "kind": "test", "cwd": str(self.repo),
            "thread_id": "00000000-0000-0000-0000-000000000001",
            "attempts": 1, "signatures": [], "escalated": False, "status": "failed",
            "model": "haiku", "effort": "medium",
        }))
        runtime_runs = self.home / ".local/state/cross-harness/runs"
        credential_task = self.root / "credentials.json"
        credential_task.write_text("dummy credential material")

        with self.assertRaisesRegex(HarnessError, "credential or environment files cannot be used as task files"):
            retry(previous, credential_task, home=self.home)
        self.assertFalse(runtime_runs.exists())

        secret_task = self.root / "retry-secret.md"
        secret_task.write_text("OPENAI_API_KEY=dummy-secret-value-for-fixture")
        with self.assertRaisesRegex(HarnessError, "task file appears to contain credential material; refusing delegation"):
            retry(previous, secret_task, home=self.home)
        self.assertFalse(runtime_runs.exists())

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_retry_write_role_blocks_dirty_stop_policy_before_executor(self, invoke, ownership, verify):
        previous = self.root / "retry-dirty-stop"
        previous.mkdir()
        (previous / "state.json").write_text(json.dumps({
            "role": "implementer", "kind": "implementation", "cwd": str(self.repo / "."),
            "thread_id": "session-1", "attempts": 1, "signatures": [], "escalated": False,
            "status": "failed", "model": "gpt-5.6-terra", "effort": "high",
        }))
        (self.repo / "user-change.txt").write_text("mine\n")
        config = self.root / "retry-stop.toml"
        contents = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(contents.replace(
            'dirty_worktree_policy = "allow_delegated"', 'dirty_worktree_policy = "stop"', 1
        ))

        with self.assertRaises(DirtyWorktreeError):
            retry(previous, self.task, config_path=config, home=self.home)

        invoke.assert_not_called()
        verify.assert_not_called()
        ownership.assert_not_called()
        blocked_runs = list((self.home / ".local/state/cross-harness/runs").iterdir())
        state = json.loads((blocked_runs[0] / "state.json").read_text())
        self.assertEqual("dirty_worktree", state["blocked_category"])
        runtime_root = self.home / ".local/state/cross-harness"
        try:
            self.assertTrue(runner._acquire_root_lock(runtime_root, self.repo))
        finally:
            runner._release_root_lock(runtime_root, self.repo)

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_retry_root_lock_blocks_without_isolated_worktree(self, invoke, ownership, verify):
        previous = self._failed_write_run("retry-root-lock", "delegated.txt", "delegated\n")
        runtime_root = self.home / ".local/state/cross-harness"
        self.assertTrue(runner._acquire_root_lock(runtime_root, self.repo))
        try:
            with self.assertRaisesRegex(DirtyWorktreeError, "another write delegation owns the root worktree"):
                retry(previous, self.task, home=self.home)
        finally:
            runner._release_root_lock(runtime_root, self.repo)

        invoke.assert_not_called()
        verify.assert_not_called()
        ownership.assert_not_called()
        blocked_runs = list((runtime_root / "runs").iterdir())
        self.assertEqual(1, len(blocked_runs))
        state = json.loads((blocked_runs[0] / "state.json").read_text())
        self.assertEqual("blocked", state["status"])
        self.assertEqual("dirty_worktree", state["blocked_category"])
        self.assertIn("another write delegation owns the root worktree", state["blocked_reason"])
        self.assertFalse((blocked_runs[0] / "ISOLATED_WORKTREE").exists())

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_retry_continues_failed_write_run_changes(self, invoke, ownership, verify):
        self.task.write_text("# Goal\nContinue.\n\n# Checks\n- fixture\n")
        previous = self._failed_write_run("failed-write", "delegated.txt", "delegated\n")
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self._successful_retry

        summary = retry(previous, self.task, home=self.home)

        self.assertEqual("success", summary["status"])
        self.assertEqual(1, invoke.call_count)
        runtime_root = self.home / ".local/state/cross-harness"
        try:
            self.assertTrue(runner._acquire_root_lock(runtime_root, self.repo))
        finally:
            runner._release_root_lock(runtime_root, self.repo)

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe", side_effect=RuntimeError("executor failed"))
    def test_retry_releases_root_lock_on_executor_exception(self, invoke, ownership, verify):
        previous = self._failed_write_run("retry-exception", "delegated.txt", "delegated\n")
        verify.return_value = (Path("/usr/bin/true"), False)

        with self.assertRaisesRegex(RuntimeError, "executor failed"):
            retry(previous, self.task, home=self.home)

        runtime_root = self.home / ".local/state/cross-harness"
        try:
            self.assertTrue(runner._acquire_root_lock(runtime_root, self.repo))
        finally:
            runner._release_root_lock(runtime_root, self.repo)

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_retry_summary_records_defaulted_settings_from_partial_config(self, invoke, ownership, verify):
        self.task.write_text("# Goal\nContinue.\n\n# Checks\n- fixture\n")
        previous = self._failed_write_run("partial-config-failed-write", "delegated.txt", "delegated\n")
        config = self.root / "partial-retry.toml"
        config.write_text('[roles.implementer]\nmodel = "custom-implementer"\n')
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self._successful_retry

        summary = retry(previous, self.task, config_path=config, home=self.home)
        recorded_summary = json.loads((Path(summary["run_dir"]) / "summary.json").read_text())

        self.assertIn("roles.tester.model", recorded_summary["defaulted_settings"])
        self.assertNotIn("roles.implementer.model", recorded_summary["defaulted_settings"])

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_retry_rejects_changes_not_left_by_failed_run(self, invoke, ownership, verify):
        previous = self._failed_write_run("failed-write", "delegated.txt", "delegated\n")
        (self.repo / "user-change.txt").write_text("mine\n")

        with self.assertRaises(DirtyWorktreeError):
            retry(previous, self.task, home=self.home)

        invoke.assert_not_called()
        verify.assert_not_called()
        ownership.assert_not_called()

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_allow_policy_retries_with_changes_outside_previous_run_diff(self, invoke, ownership, verify):
        self.task.write_text("# Goal\nContinue.\n\n# Checks\n- fixture\n")
        previous = self._failed_write_run("failed-write", "delegated.txt", "delegated\n")
        (self.repo / "user-change.txt").write_text("mine\n")
        config = self.root / "retry-allow.toml"
        contents = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(contents.replace(
            'dirty_worktree_policy = "allow_delegated"', 'dirty_worktree_policy = "allow"', 1
        ))
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self._successful_retry

        summary = retry(previous, self.task, config_path=config, home=self.home)

        self.assertEqual("success", summary["status"])
        self.assertEqual(1, invoke.call_count)
        ownership.assert_called_once()

    def test_retry_blocks_when_previous_diff_check_was_unavailable(self):
        previous = self._failed_write_run("failed-diff-check", "delegated.txt", "delegated\n")
        summary_path = previous / "summary.json"
        previous_summary = json.loads(summary_path.read_text())
        previous_summary["diff_check"] = "unavailable"
        summary_path.write_text(json.dumps(previous_summary))

        with patch.dict(os.environ, {"CROSS_HARNESS_ACTIVE": ""}):
            with self.assertRaisesRegex(DirtyWorktreeError, "previous run diff could not be obtained"):
                retry(previous, self.task, home=self.home)

        blocked_runs = list((self.home / ".local/state/cross-harness/runs").iterdir())
        blocked_state = json.loads((blocked_runs[0] / "state.json").read_text())
        self.assertEqual("dirty_worktree", blocked_state["blocked_category"])
        self.assertIn("previous run diff could not be obtained", blocked_state["blocked_reason"])

    def test_retry_records_git_inspection_failures_before_raising(self):
        previous = self._failed_write_run("failed-git-inspection", "delegated.txt", "delegated\n")
        role = default_config()["roles"]["implementer"]
        runtime_root = self.root / "runtime"

        for index, (target, failure) in enumerate((
            ("_dirty", HarnessError("could not inspect Git worktree")),
            ("_diff_details", subprocess.TimeoutExpired(["git", "diff"], 30)),
            ("_diff_details", OSError("git executable unavailable")),
        )):
            with self.subTest(target=target):
                retry_run = self.root / f"blocked-{index}-{target}"
                retry_run.mkdir()
                try:
                    with patch.object(runner, target, side_effect=failure):
                        with self.assertRaisesRegex(DirtyWorktreeError, "could not inspect Git worktree changes"):
                            runner._prepare_retry_execution(
                                default_config(), "implementer", role, "implementation", self.repo,
                                retry_run, previous, runtime_root=runtime_root,
                                attempts=1, thread_id="session-1", signatures=[],
                            )
                finally:
                    runner._release_root_lock(runtime_root, self.repo)
                state = json.loads((retry_run / "state.json").read_text())
                self.assertEqual("blocked", state["status"])
                self.assertEqual("dirty_worktree", state["blocked_category"])
                self.assertIn("could not inspect Git worktree changes", state["blocked_reason"])

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_isolated_retry_reuses_failed_run_worktree(self, invoke, ownership, verify):
        config = self.root / "isolate.toml"
        default = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(default.replace('dirty_worktree_policy = "allow_delegated"', 'dirty_worktree_policy = "isolate"', 1))
        self.task.write_text("# Goal\nContinue.\n\n# Checks\n- fixture\n")
        (self.repo / "pre-existing.txt").write_text("outside isolated worktree\n")
        verify.return_value = (Path("/usr/bin/true"), False)

        def fail_in_isolated_worktree(command, task, env, cwd, run_dir, timeout):
            (cwd / "delegated.txt").write_text("delegated\n")
            (run_dir / "events.jsonl").write_text('{"type":"turn.failed","error":{"message":"fixture failure"}}\n')
            (run_dir / "stderr.log").write_text("fixture failure")
            (run_dir / "final.json").write_text(json.dumps({
                "status": "failed", "work_completed": "", "changed_files": [],
                "tests": [], "error": "fixture failure", "next_decision": None,
            }))
            return 1

        invoke.side_effect = fail_in_isolated_worktree
        failed = delegate("implementer", "implementation", self.task, self.repo, config_path=config, home=self.home)
        previous = Path(failed["run_dir"])
        worktree = Path((previous / "ISOLATED_WORKTREE").read_text().strip())

        def retry_in_same_worktree(command, task, env, cwd, run_dir, timeout):
            self.assertEqual(worktree, cwd)
            self.assertTrue((cwd / "delegated.txt").is_file())
            return self._successful_retry(command, task, env, cwd, run_dir, timeout)

        invoke.side_effect = retry_in_same_worktree
        summary = retry(previous, self.task, config_path=config, home=self.home)
        retry_run = Path(summary["run_dir"])
        self.assertEqual(str(worktree), (retry_run / "ISOLATED_WORKTREE").read_text().strip())
        self.assertEqual(2, invoke.call_count)

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_retry_chain_accepts_each_previous_run_delta(self, invoke, ownership, verify):
        self.task.write_text("# Goal\nContinue.\n\n# Checks\n- fixture\n")
        previous = self._failed_write_run("first-failed-write", "one.txt", "one\n")
        verify.return_value = (Path("/usr/bin/true"), False)

        def fail_with_second_change(command, task, env, cwd, run_dir, timeout):
            (cwd / "two.txt").write_text("two\n")
            (run_dir / "events.jsonl").write_text('{"type":"turn.failed","error":{"message":"second fixture failure"}}\n')
            (run_dir / "stderr.log").write_text("second fixture failure")
            (run_dir / "final.json").write_text(json.dumps({
                "status": "failed", "work_completed": "", "changed_files": [],
                "tests": [], "error": "second fixture failure", "next_decision": None,
            }))
            return 1

        invoke.side_effect = fail_with_second_change
        second = retry(previous, self.task, home=self.home)
        invoke.side_effect = self._successful_retry
        third = retry(Path(second["run_dir"]), self.task, home=self.home)

        self.assertEqual("success", third["status"])
        self.assertEqual(2, invoke.call_count)

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_default_allow_delegated_accepts_recorded_changes_and_rejects_other_changes(self, invoke, ownership, verify):
        self.task.write_text("# Goal\nMake a delegated change.\n\n# Checks\n- fixture\n")
        verify.return_value = (Path("/usr/bin/true"), False)

        def complete(command, task, env, cwd, run_dir, timeout):
            if not (self.repo / "delegated.txt").exists():
                (self.repo / "delegated.txt").write_text("delegated\n")
            (run_dir / "events.jsonl").write_text(
                '{"type":"item.completed","item":{"type":"command_execution","command":"fixture","status":"completed","exit_code":0}}\n'
                '{"type":"turn.completed","usage":{}}\n'
            )
            (run_dir / "stderr.log").write_text("")
            (run_dir / "final.json").write_text(json.dumps({
                "status": "success", "work_completed": "done", "changed_files": [],
                "tests": [], "error": None, "next_decision": None,
            }))
            return 0

        invoke.side_effect = complete
        first = delegate("implementer", "implementation", self.task, self.repo, home=self.home)
        runtime_root = self.home / ".local/state/cross-harness"
        records = json.loads((runtime_root / "delegated-changes.json").read_text())
        self.assertIn("delegated.txt", records[str(self.repo.resolve())])

        second = delegate("implementer", "implementation", self.task, self.repo, home=self.home)
        self.assertEqual("success", second["status"])
        summary = retry(Path(first["run_dir"]), self.task, home=self.home)
        self.assertEqual("success", summary["status"])
        self.assertEqual(3, invoke.call_count)

        (self.repo / "user-change.txt").write_text("mine\n")
        with self.assertRaises(DirtyWorktreeError):
            delegate("implementer", "implementation", self.task, self.repo, home=self.home)
        self.assertEqual(3, invoke.call_count)

    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner.verify_claude_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_retry_allows_executor_reported_block(self, invoke, ownership, verify):
        run = self.root / "executor-reported-block"
        run.mkdir()
        (run / "events.jsonl").write_text("")
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "blocked", "work_completed": "", "changed_files": [],
            "tests": [], "error": "needs revised instructions", "next_decision": None,
        }))
        role = default_config()["roles"]["tester"]
        finalize_run(run, "tester", role, "test", self.repo, 1, 0)
        state = json.loads((run / "state.json").read_text())
        self.assertEqual("executor_reported", state["blocked_category"])
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self._successful_retry

        summary = retry(run, self.task, home=self.home)

        self.assertEqual("partial", summary["status"])
        self.assertEqual(1, invoke.call_count)

    def test_retry_refuses_safety_and_nonreusable_block_categories(self):
        cases = (
            ("authentication", "safety-policy stop"),
            ("rate_limit", "safety-policy stop"),
            ("dirty_worktree", "create a new delegate"),
            ("missing_isolated_worktree", "create a new delegate"),
        )
        for category, message in cases:
            with self.subTest(category=category):
                run = self.root / f"{category}-block"
                run.mkdir()
                (run / "state.json").write_text(json.dumps({
                    "role": "tester", "kind": "test", "cwd": str(self.repo),
                    "thread_id": "session-1", "attempts": 0, "signatures": [], "escalated": False,
                    "status": "blocked", "blocked_category": category,
                }))

                with self.assertRaisesRegex(HarnessError, message):
                    retry(run, self.task, home=self.home)

    def test_finalize_records_only_run_delta_and_unchanged_trusted_changes(self):
        runtime_root = self.root / "explicit-runtime"
        run = self.root / "external-run"
        run.mkdir()
        (self.repo / "trusted.txt").write_text("trusted\n")
        (self.repo / "user-change.txt").write_text("user\n")
        _write_baseline(run, self.repo)
        (self.repo / "delegated.txt").write_text("delegated\n")
        records_path = runtime_root / "delegated-changes.json"
        records_path.parent.mkdir(parents=True)
        records_path.write_text(json.dumps({
            str(self.repo.resolve()): {"trusted.txt": sha256(self.repo / "trusted.txt")}
        }))
        (run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-terra", "effort": "high", "output_limit_chars": 8000, "write": True}

        summary = finalize_run(
            run, "implementer", role, "review", self.repo, 0, 1,
            runtime_root=runtime_root, dirty_worktree_policy="allow_delegated",
        )

        self.assertEqual("success", summary["status"])
        records = json.loads((runtime_root / "delegated-changes.json").read_text())
        self.assertEqual(
            {"trusted.txt", "delegated.txt"}, set(records[str(self.repo.resolve())])
        )

    def test_finalize_records_partial_write_runs(self):
        runtime_root = self.root / "runtime"
        run = self.root / "partial"
        run.mkdir()
        _write_baseline(run, self.repo)
        (self.repo / "partial.txt").write_text("partial\n")
        (run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "partial", "work_completed": "", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-terra", "effort": "high", "output_limit_chars": 8000, "write": True}

        summary = finalize_run(
            run, "implementer", role, "review", self.repo, 0, 1,
            runtime_root=runtime_root, dirty_worktree_policy="allow_delegated",
        )

        self.assertEqual("partial", summary["status"])
        records = json.loads((runtime_root / "delegated-changes.json").read_text())
        self.assertIn("partial.txt", records[str(self.repo.resolve())])

    def test_delegated_change_records_are_process_locked_read_modify_writes(self):
        runtime_root = self.root / "runtime"
        ready_one = self.root / "record-one-ready"
        ready_two = self.root / "record-two-ready"
        go = self.root / "record-go"

        def worker(file_name: str, ready: Path) -> None:
            original_read = runner._read_delegated_changes

            def delayed_read(runtime: Path):
                records = original_read(runtime)
                ready.touch()
                while not go.exists():
                    time.sleep(0.01)
                return records

            runner._read_delegated_changes = delayed_read
            try:
                runner._record_delegated_changes(
                    runtime_root, self.root, self.repo,
                    [
                        {"file": "one.txt", "fingerprint": "one.txt"},
                        {"file": "two.txt", "fingerprint": "two.txt"},
                    ],
                    [{"file": file_name, "fingerprint": file_name}],
                )
            except BaseException:
                os._exit(1)
            os._exit(0)

        first_pid = os.fork()
        if first_pid == 0:
            worker("one.txt", ready_one)
        deadline = time.monotonic() + 2
        while not ready_one.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready_one.exists())

        second_pid = os.fork()
        if second_pid == 0:
            worker("two.txt", ready_two)
        time.sleep(0.2)
        self.assertFalse(ready_two.exists())
        go.touch()

        _, first_status = os.waitpid(first_pid, 0)
        _, second_status = os.waitpid(second_pid, 0)
        self.assertEqual(0, os.waitstatus_to_exitcode(first_status))
        self.assertEqual(0, os.waitstatus_to_exitcode(second_status))
        records = json.loads((runtime_root / "delegated-changes.json").read_text())
        self.assertEqual(
            {"one.txt", "two.txt"}, set(records[str(self.repo.resolve())]),
        )

    def test_finalize_records_delegated_changes_only_for_allow_delegated_policy(self):
        runtime_root = self.root / "runtime"
        role = {"model": "gpt-5.6-terra", "effort": "high", "output_limit_chars": 8000, "write": True}

        allow_run = self.root / "allow"
        allow_run.mkdir()
        _write_baseline(allow_run, self.repo)
        (self.repo / "allow-change.txt").write_text("allow\n")
        (allow_run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (allow_run / "stderr.log").write_text("")
        (allow_run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))

        finalize_run(
            allow_run, "implementer", role, "review", self.repo, 0, 1,
            runtime_root=runtime_root, dirty_worktree_policy="allow",
        )

        self.assertFalse((runtime_root / "delegated-changes.json").exists())

        delegated_run = self.root / "allow-delegated"
        delegated_run.mkdir()
        _write_baseline(delegated_run, self.repo)
        (self.repo / "delegated-change.txt").write_text("delegated\n")
        (delegated_run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (delegated_run / "stderr.log").write_text("")
        (delegated_run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))

        finalize_run(
            delegated_run, "implementer", role, "review", self.repo, 0, 1,
            runtime_root=runtime_root, dirty_worktree_policy="allow_delegated",
        )

        records = json.loads((runtime_root / "delegated-changes.json").read_text())
        self.assertEqual(
            {"delegated-change.txt"}, set(records[str(self.repo.resolve())])
        )

    def test_delegated_changes_record_and_match_non_ascii_tracked_path(self):
        file_name = "日本語\tdelegated.txt"
        (self.repo / file_name).write_text("before\n")
        git(self.repo, "add", file_name)
        git(self.repo, "commit", "-m", "delegated special path")
        (self.repo / file_name).write_text("after\n")
        runtime_root = self.root / "delegated-special-runtime"
        run = self.root / "delegated-special-run"
        run.mkdir()

        _, current, changed = runner._diff_details(self.repo)
        self.assertEqual([file_name], changed)
        runner._record_delegated_changes(runtime_root, run, self.repo, current, current)

        self.assertTrue(runner._delegated_changes_match(
            runtime_root, self.repo, runner._dirty(self.repo), current,
        ))
        records = json.loads((runtime_root / "delegated-changes.json").read_text())
        self.assertEqual({file_name}, set(records[str(self.repo.resolve())]))

    def test_finalize_records_invalid_utf8_path_and_writes_readable_json(self):
        file_name = os.fsdecode(b"bad-\xff-delegated.txt")
        path = self.repo / file_name
        path.write_bytes(b"before\n")
        git(self.repo, "add", file_name)
        git(self.repo, "commit", "-m", "invalid utf8 delegated path")

        runtime_root = self.root / "invalid-utf8-runtime"
        run = self.root / "invalid-utf8-finalize"
        run.mkdir()
        _write_baseline(run, self.repo)
        path.write_bytes(b"after\n")
        (run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-terra", "effort": "high", "output_limit_chars": 8000, "write": True}

        finalize_run(
            run, "implementer", role, "review", self.repo, 0, 1,
            runtime_root=runtime_root, dirty_worktree_policy="allow_delegated",
        )

        summary = load_json(run / "summary.json", {})
        records = load_json(runtime_root / "delegated-changes.json", {})
        self.assertEqual(file_name, summary["changed_files"][0])
        self.assertEqual(file_name, summary["diff_summary"][0]["file"])
        self.assertEqual({file_name}, set(records[str(self.repo.resolve())]))
        (run / "summary.json").read_bytes().decode("utf-8")
        (run / "summary.txt").read_bytes().decode("utf-8")

    def test_finalize_does_not_record_isolated_write_runs(self):
        runtime_root = self.root / "runtime"
        records_path = runtime_root / "delegated-changes.json"
        records_path.parent.mkdir(parents=True)
        initial = {str(self.repo.resolve()): {"prior.txt": "fingerprint"}}
        records_path.write_text(json.dumps(initial))
        role = {"model": "gpt-5.6-terra", "effort": "high", "output_limit_chars": 8000, "write": True}

        run = self.root / "isolated"
        run.mkdir()
        _write_baseline(run, self.repo)
        (self.repo / "isolated.txt").write_text("isolated\n")
        (run / "events.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        (run / "ISOLATED_WORKTREE").write_text(str(self.repo) + "\n")
        finalize_run(run, "implementer", role, "review", self.repo, 0, 1, runtime_root=runtime_root)

        self.assertEqual(initial, json.loads(records_path.read_text()))

    @patch("cross_harness.runner.failure_signature", return_value="same-signature")
    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_escalation_continues_retry_recorded_changes(self, invoke, ownership, verify, signature):
        previous = self._failed_write_run("retry-escalation", "existing.txt", "existing\n")
        verify.return_value = (Path("/usr/bin/true"), False)

        release = None

        def fail_with_change(command, task, env, cwd, run_dir, timeout):
            (self.repo / "delegated.txt").write_text("delegated\n")
            if invoke.call_count == 2:
                lock_path = runner._root_lock_path(self.home / ".local/state/cross-harness", self.repo)
                self.assertIsNone(runner._try_lock(lock_path))
                self.assertEqual(0, release.call_count)
            (run_dir / "events.jsonl").write_text('{"type":"turn.failed","error":{"message":"failed"}}\n')
            (run_dir / "stderr.log").write_text("failed")
            (run_dir / "final.json").write_text(json.dumps({
                "status": "failed", "work_completed": "", "changed_files": [],
                "tests": [], "error": "failed", "next_decision": None,
            }))
            return 1

        invoke.side_effect = fail_with_change
        with patch.object(runner, "_release_root_lock", wraps=runner._release_root_lock) as release:
            result = retry(previous, self.task, home=self.home)
            self.assertEqual(1, release.call_count)
        self.assertEqual(2, invoke.call_count)
        self.assertTrue(all(call.args[1].startswith(CODEX_EXECUTOR_CHARTER) for call in invoke.call_args_list))
        self.assertTrue(json.loads((Path(result["run_dir"]) / "state.json").read_text())["escalated"])

    @patch("cross_harness.runner.verify_claude_config_ownership")
    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner._invoke_safe")
    def test_claude_retry_resumes_recorded_session(self, invoke, verify_claude, claude_ownership):
        config = self.root / "claude-retry.toml"
        default = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(default.replace(
            '[roles.reviewer]\nharness = "claude"\nmodel = "opus"\neffort = "high"\nmax_parallel = 1\nretries = 0',
            '[roles.reviewer]\nharness = "claude"\nmodel = "sonnet"\neffort = "high"\nmax_parallel = 1\nretries = 2',
            1,
        ))
        previous = self.root / "claude-retry-previous"
        previous.mkdir()
        (previous / "state.json").write_text(json.dumps({
            "role": "reviewer", "kind": "review", "cwd": str(self.repo),
            "thread_id": "session-1", "attempts": 1, "signatures": [],
            "escalated": False, "status": "failed", "model": "sonnet", "effort": "high",
        }))
        verify_claude.return_value = (Path("/usr/local/bin/claude"), False)

        def complete(command, task, env, cwd, run_dir, timeout):
            self.assertEqual("claude", env["CROSS_HARNESS_EXECUTOR"])
            self.assertEqual("session-1", command[command.index("--resume") + 1])
            (run_dir / "events.jsonl").write_text('{"type":"result","session_id":"session-2","is_error":false,"usage":{}}\n')
            (run_dir / "stderr.log").write_text("")
            (run_dir / "final.json").write_text(json.dumps({
                "status": "success", "work_completed": "reviewed", "changed_files": [],
                "tests": ["review"], "error": None, "next_decision": None,
            }))
            return 0

        invoke.side_effect = complete
        summary = retry(previous, self.task, config_path=config, home=self.home)
        self.assertEqual("success", summary["status"])
        self.assertEqual("session-2", summary["thread_id"])
        verify_claude.assert_called_once()
        claude_ownership.assert_called_once_with(self.home.resolve(), self.repo.resolve())

    def test_escalation_uses_claude_fallback_and_effort_order(self):
        role = {"harness": "claude", "model": "sonnet", "effort": "xhigh"}
        escalated = _escalated_role(role, default_config())
        self.assertEqual("opus", escalated["model"])
        self.assertEqual("max", escalated["effort"])

    def test_escalation_keeps_claude_model_at_fallback_ceiling(self):
        role = {"harness": "claude", "model": "opus", "effort": "high"}
        escalated = _escalated_role(role, default_config())
        self.assertEqual("opus", escalated["model"])
        self.assertEqual("xhigh", escalated["effort"])

    def test_rate_limit_event_fails_closed(self):
        run = self.root / "rate-run"
        run.mkdir()
        (run / "events.jsonl").write_text('{"type":"turn.failed","error":{"message":"usage limit reached"}}\n')
        (run / "stderr.log").write_text("")
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000}
        summary = finalize_run(run, "tester", role, "test", self.repo, 0, 1)
        self.assertEqual("blocked", summary["status"])
        self.assertIn("no fallback", summary["error"])
        state = json.loads((run / "state.json").read_text())
        self.assertEqual("rate_limit", state["blocked_category"])
        self.assertTrue((run / "BLOCKED").exists())

    def test_claude_stderr_rate_limit_fails_closed(self):
        run = self.root / "claude-stderr-rate-limit-run"
        run.mkdir()
        (run / "events.jsonl").write_text("")
        (run / "stderr.log").write_text("Claude AI usage limit reached\n")
        role = {"harness": "claude", "model": "sonnet", "effort": "high", "output_limit_chars": 8000}

        summary = finalize_run(run, "reviewer", role, "review", self.repo, 1, 1)

        self.assertEqual("blocked", summary["status"])
        state = json.loads((run / "state.json").read_text())
        self.assertEqual("rate_limit", state["blocked_category"])

    def test_claude_structured_rate_limit_and_authentication_events_block_without_retry(self):
        cases = (
            ("rate_limit", '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1784648400,"rateLimitType":"five_hour","overageStatus":"unavailable","overageResetsAt":null,"isUsingOverage":false}}\n'),
            ("authentication", '{"type":"result","subtype":"error_during_execution","error":"authentication_failed","result":"redacted"}\n'),
        )
        role = {"harness": "claude", "model": "sonnet", "effort": "high", "output_limit_chars": 8000, "write": False}
        for category, events in cases:
            with self.subTest(category=category):
                run = self.root / f"claude-{category}-run"
                run.mkdir()
                (run / "events.jsonl").write_text(events)
                (run / "stderr.log").write_text("")
                (run / "final.json").write_text(json.dumps({
                    "status": "success", "work_completed": "done", "changed_files": [],
                    "tests": [], "error": None, "next_decision": None,
                }))

                summary = finalize_run(run, "reviewer", role, "review", self.repo, 0, 1)

                self.assertEqual("blocked", summary["status"])
                state = json.loads((run / "state.json").read_text())
                self.assertEqual(category, state["blocked_category"])
                with self.assertRaisesRegex(HarnessError, "safety-policy stop"):
                    retry(run, self.task, home=self.home)

    def test_rejected_overage_allowed_notice_does_not_block_completed_run(self):
        run = self.root / "overage-allowed-completed-run"
        run.mkdir()
        (run / "events.jsonl").write_text(
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1784648400,"rateLimitType":"five_hour","overageStatus":"allowed","overageResetsAt":1784640000,"isUsingOverage":true}}\n'
        )
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "done", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"harness": "claude", "model": "sonnet", "effort": "high", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "reviewer", role, "review", self.repo, 0, 1)

        self.assertEqual("success", summary["status"])
        self.assertEqual("overage_allowed", summary["rate_limit_notice"])
        self.assertIsNone(summary["error"])
        self.assertIn("rate_limit_notice: overage_allowed", (run / "summary.txt").read_text())
        state = json.loads((run / "state.json").read_text())
        self.assertNotIn("blocked_category", state)
        self.assertFalse((run / "BLOCKED").exists())

    def test_rejected_overage_allowed_blocks_uncompleted_run(self):
        run = self.root / "overage-allowed-uncompleted-run"
        run.mkdir()
        (run / "events.jsonl").write_text(
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1784648400,"rateLimitType":"five_hour","overageStatus":"allowed","overageResetsAt":1784640000,"isUsingOverage":true}}\n'
        )
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "failed", "work_completed": "", "changed_files": [],
            "tests": [], "error": "interrupted", "next_decision": None,
        }))
        role = {"harness": "claude", "model": "sonnet", "effort": "high", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "reviewer", role, "review", self.repo, 1, 1)

        self.assertEqual("blocked", summary["status"])
        state = json.loads((run / "state.json").read_text())
        self.assertEqual("rate_limit", state["blocked_category"])

    def test_read_only_change_does_not_override_rate_limit_block(self):
        run = self.root / "read-only-rate-limit-run"
        run.mkdir()
        _write_baseline(run, self.repo)
        (self.repo / "README.md").write_text("after\n")
        (run / "events.jsonl").write_text(
            '{"type":"turn.failed","error":{"message":"usage limit reached"}}\n'
        )
        (run / "stderr.log").write_text("")
        role = {"model": "gpt-5.6-luna", "effort": "low", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "tester", role, "test", self.repo, 1, 1)

        self.assertEqual("blocked", summary["status"])
        state = json.loads((run / "state.json").read_text())
        self.assertEqual("blocked", state["status"])
        self.assertEqual("rate_limit", state["blocked_category"])

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_isolation_policy_preserves_dirty_original(self, invoke, ownership, verify):
        config = self.root / "isolate.toml"
        default = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(default.replace('dirty_worktree_policy = "allow_delegated"', 'dirty_worktree_policy = "isolate"', 1))
        (self.repo / "user-change.txt").write_text("mine\n")
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self.fake_invoke
        summary = delegate("implementer", "implementation", self.task, self.repo, config_path=config, home=self.home)
        run = Path(summary["run_dir"])
        isolated = Path((run / "ISOLATED_WORKTREE").read_text().strip())
        self.assertTrue(isolated.exists())
        self.assertEqual("mine\n", (self.repo / "user-change.txt").read_text())
        self.assertNotEqual(self.repo, isolated)

    def test_timeout_marks_run_interrupted(self):
        run = self.root / "timeout-run"
        run.mkdir()
        code = _invoke_safe(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            "task",
            {"PATH": "/usr/bin:/bin"},
            self.repo,
            run,
            0.1,
        )
        self.assertEqual(124, code)
        self.assertEqual("timeout\n", (run / "INTERRUPTED").read_text())

    def test_timeout_preserves_claude_session_for_resume(self):
        run = self.root / "claude-timeout-run"
        run.mkdir()
        (run / "events.jsonl").write_text(
            '{"type":"assistant","session_id":"session-timeout","message":{"content":[]}}\n'
        )
        (run / "stderr.log").write_text("")
        role = {"harness": "claude", "model": "sonnet", "effort": "high", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "reviewer", role, "review", self.repo, 124, 1)

        self.assertEqual("session-timeout", summary["thread_id"])
        state = json.loads((run / "state.json").read_text())
        self.assertEqual("session-timeout", state["thread_id"])

    def test_failed_claude_non_bash_tool_result_does_not_override_success_report(self):
        run = self.root / "claude-tool-failure-run"
        run.mkdir()
        (run / "events.jsonl").write_text(
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"read-1","name":"Read","input":{"file_path":"missing.txt"}}]}}\n'
            '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"read-1","is_error":true,"content":"not found"}]}}\n'
        )
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": "success", "work_completed": "reviewed", "changed_files": [],
            "tests": ["uv run pytest -q"], "error": None, "next_decision": None,
        }))
        role = {"harness": "claude", "model": "sonnet", "effort": "high", "output_limit_chars": 8000, "write": False}

        summary = finalize_run(run, "reviewer", role, "review", self.repo, 0, 1)

        self.assertEqual("success", summary["status"])
        self.assertIsNone(summary["error"])

    def test_detached_supervisor_receives_package_path_for_clean_interpreter(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_claude = fake_bin / "claude"
        fake_claude.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' '{\"type\":\"result\",\"session_id\":\"00000000-0000-0000-0000-000000000001\",\"is_error\":false,\"usage\":{},\"result\":\"{\\\"status\\\":\\\"success\\\",\\\"work_completed\\\":\\\"finished\\\",\\\"changed_files\\\":[],\\\"tests\\\":[],\\\"error\\\":null,\\\"next_decision\\\":null}\"}'\n"
        )
        fake_claude.chmod(0o755)
        clean_interpreter = self.root / "clean-python"
        clean_interpreter.write_text(
            "#!/bin/sh\n"
            "exec \"$CROSS_HARNESS_TEST_PYTHON\" -S -c '\n"
            "import os, runpy, sys\n"
            "from pathlib import Path\n"
            "import cross_harness.runner\n"
            "cross_harness.runner.verify_claude_subscription = lambda *args: (Path(os.environ[\"TEST_FAKE_CLAUDE\"]), False)\n"
            "cross_harness.runner.verify_claude_config_ownership = lambda *args: None\n"
            "sys.argv = [\"cross-harness\", *sys.argv[3:]]\n"
            "runpy.run_module(\"cross_harness.cli\", run_name=\"__main__\")\n"
            "' \"$@\"\n"
        )
        clean_interpreter.chmod(0o755)
        environment = {
            "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CROSS_HARNESS_TEST_PYTHON": sys.executable,
            "TEST_FAKE_CLAUDE": str(fake_claude),
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "cross_harness.runner.sys.executable", str(clean_interpreter)
        ):
            run_dir = start_detached_delegate("tester", "test", self.task, self.repo, home=self.home)
        summary = wait_for_run(run_dir, 5)
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual("partial", summary["status"])
        self.assertTrue((run_dir / "events.jsonl").is_file())
        self.assertTrue((run_dir / "state.json").is_file())
        self.assertNotIn("ModuleNotFoundError", (run_dir / "supervisor.stderr.log").read_text())

    def test_wait_reports_and_reaps_a_zombie_supervisor(self):
        run = self.root / "zombie-supervisor"
        run.mkdir()
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        (run / "supervisor.pid").write_text(f"{pid}\n")
        started = time.monotonic()
        with self.assertRaisesRegex(SupervisorDiedError, "exited without producing a summary"):
            wait_for_run(run, 10, poll_seconds=0.01)
        self.assertLess(time.monotonic() - started, 1)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_wait_returns_summary_finalized_after_supervisor_exit(self):
        run = self.root / "finalized-after-exit"
        run.mkdir()
        (run / "supervisor.pid").write_text("12345\n")
        expected = {"status": "success", "work_completed": "finished"}

        def finalize_after_liveness_check(_run_dir):
            (run / "summary.json").write_text(json.dumps(expected))
            (run / "summary.txt").write_text("success summary\n")
            return False

        with patch("cross_harness.runner._supervisor_alive", side_effect=finalize_after_liveness_check):
            self.assertEqual(expected, wait_for_run(run, 1))

    def test_detached_supervisor_finalizes_after_foreground_is_killed(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_claude = fake_bin / "claude"
        fake_claude.write_text(
            "#!/bin/sh\n"
            "touch \"$CROSS_HARNESS_RUN_DIR/fake-running\"\n"
            "sleep 1\n"
            "printf '%s\\n' '{\"type\":\"result\",\"session_id\":\"00000000-0000-0000-0000-000000000001\",\"is_error\":false,\"usage\":{},\"result\":\"{\\\"status\\\":\\\"success\\\",\\\"work_completed\\\":\\\"finished after detach\\\",\\\"changed_files\\\":[],\\\"tests\\\":[],\\\"error\\\":null,\\\"next_decision\\\":null}\"}'\n"
        )
        fake_claude.chmod(0o755)
        injector = self.root / "injector"
        injector.mkdir()
        (injector / "sitecustomize.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "import cross_harness.runner\n"
            "cross_harness.runner.verify_claude_subscription = lambda *args: (Path(os.environ['TEST_FAKE_CLAUDE']), False)\n"
            "cross_harness.runner.verify_claude_config_ownership = lambda *args: None\n"
        )
        environment = os.environ.copy()
        environment.pop("CROSS_HARNESS_ACTIVE", None)
        environment.update({
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": os.pathsep.join([str(injector), str(Path(__file__).resolve().parents[1] / "src")]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEST_FAKE_CLAUDE": str(fake_claude),
        })
        command = [
            sys.executable, "-m", "cross_harness.cli", "--home", str(self.home),
            "delegate", "--role", "tester", "--kind", "test", "--task-file", str(self.task),
            "--cwd", str(self.repo), "--timeout-seconds", "10",
        ]
        foreground = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        assert foreground.stdout is not None
        run_dir = Path(foreground.stdout.readline().strip())
        self.assertTrue((run_dir / "supervisor.pid").is_file())
        deadline = time.monotonic() + 5
        while not (run_dir / "fake-running").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(
            (run_dir / "fake-running").exists(),
            "files=" + repr([path.name for path in run_dir.iterdir()]) + " stderr=" + (run_dir / "stderr.log").read_text(errors="replace"),
        )
        foreground.kill()
        foreground.wait(timeout=5)
        foreground.stdout.close()
        assert foreground.stderr is not None
        foreground.stderr.close()
        summary = wait_for_run(run_dir, 5)
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual("partial", summary["status"])
        self.assertTrue((run_dir / "final.json").is_file())

    @patch("cross_harness.runner.failure_signature", return_value="same-signature")
    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner.verify_claude_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_identical_failure_triggers_exactly_one_escalation(self, invoke, ownership, verify, signature):
        config = self.root / "codex-parent-retry.toml"
        default = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(default.replace('parent_harness = "claude"', 'parent_harness = "codex"', 1))
        previous = self.root / "previous"
        previous.mkdir()
        (previous / "state.json").write_text(json.dumps({
            "role": "tester", "kind": "test", "cwd": str(self.repo),
            "thread_id": "00000000-0000-0000-0000-000000000001",
            "attempts": 1, "signatures": ["same-signature"], "escalated": False,
            "status": "failed", "model": "haiku", "effort": "medium",
        }))
        verify.return_value = (Path("/usr/bin/true"), False)

        def fail(command, task, env, cwd, run_dir, timeout):
            self.assertEqual("1", env["CROSS_HARNESS_ACTIVE"])
            self.assertEqual("claude", env["CROSS_HARNESS_EXECUTOR"])
            self.assertEqual("codex", env["CROSS_HARNESS_PARENT"])
            self.assertEqual(
                env["CROSS_HARNESS_PARENT"],
                json.loads((run_dir / "execution.json").read_text())["parent_harness"],
            )
            self.assertNotIn("CROSS_HARNESS_WRITE", env)
            (run_dir / "events.jsonl").write_text(
                '{"type":"thread.started","thread_id":"00000000-0000-0000-0000-000000000002"}\n'
                '{"type":"turn.failed","error":{"message":"same test failed"}}\n'
            )
            (run_dir / "stderr.log").write_text("same test failed")
            (run_dir / "final.json").write_text(json.dumps({
                "status": "failed", "work_completed": "", "changed_files": [],
                "tests": ["fixture: failed"], "error": "same test failed", "next_decision": None,
            }))
            return 1

        invoke.side_effect = fail
        result = retry(previous, self.task, config_path=config, home=self.home)
        self.assertEqual(2, invoke.call_count)
        self.assertEqual("sonnet", result["model"])
        escalated = json.loads((Path(result["run_dir"]) / "state.json").read_text())
        self.assertTrue(escalated["escalated"])

    @patch("cross_harness.runner.verify_claude_subscription", side_effect=AuthError("expired"))
    @patch("cross_harness.runner.verify_claude_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_retry_auth_failure_preserves_attempt_history(self, invoke, ownership, verify):
        previous = self.root / "retry-auth-previous"
        previous.mkdir()
        (previous / "state.json").write_text(json.dumps({
            "role": "tester", "kind": "test", "cwd": str(self.repo),
            "thread_id": "00000000-0000-0000-0000-000000000001",
            "attempts": 1, "signatures": ["prior"], "escalated": False,
            "status": "failed", "model": "haiku", "effort": "medium",
        }))
        summary = retry(previous, self.task, home=self.home)
        state = json.loads((Path(summary["run_dir"]) / "state.json").read_text())
        self.assertEqual("blocked", state["status"])
        self.assertEqual(1, state["attempts"])
        self.assertEqual(["prior"], state["signatures"])
        invoke.assert_not_called()



if __name__ == "__main__":
    unittest.main()
