from pathlib import Path
from unittest.mock import patch
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from cross_harness.errors import AuthError, DirtyWorktreeError, HarnessError, SupervisorDiedError
from cross_harness.config import default_config
from cross_harness.runner import _claude_command, _escalated_role, _invoke_safe, _write_baseline, _write_claude_final_from_events, delegate, retry, start_detached_delegate, wait_for_run
from cross_harness.runner import finalize_run


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

    def fake_invoke(self, command, task, env, cwd, run_dir, timeout):
        self.assertEqual("1", env["CROSS_HARNESS_ACTIVE"])
        self.assertEqual("codex", env["CROSS_HARNESS_EXECUTOR"])
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

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_read_only_delegation_generates_bounded_artifacts(self, invoke, ownership, verify):
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self.fake_invoke
        summary = delegate("tester", "test", self.task, self.repo, home=self.home)
        run = Path(summary["run_dir"])
        self.assertEqual("success", summary["status"])
        self.assertTrue((run / "task.md").exists())
        self.assertTrue((run / "events.jsonl").exists())
        self.assertTrue((run / "summary.txt").exists())
        self.assertTrue((run / "baseline.json").exists())
        self.assertEqual(1, invoke.call_count)

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_write_delegation_sets_write_executor_marker(self, invoke, ownership, verify):
        verify.return_value = (Path("/usr/bin/true"), False)
        invoke.side_effect = self.fake_invoke
        summary = delegate("implementer", "implementation", self.task, self.repo, home=self.home)
        environment = invoke.call_args.args[2]
        self.assertEqual("1", environment["CROSS_HARNESS_WRITE"])
        self.assertEqual("success", summary["status"])

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
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
        with self.assertRaises(DirtyWorktreeError):
            delegate("implementer", "implementation", self.task, self.repo, home=self.home)
        invoke.assert_not_called()
        verify.assert_not_called()
        ownership.assert_not_called()
        self.assertEqual("mine\n", (self.repo / "user-change.txt").read_text())
        runs = list((self.home.resolve() / ".local/state/cross-harness/runs").iterdir())
        self.assertEqual(1, len(runs))
        state = json.loads((runs[0] / "state.json").read_text())
        self.assertEqual("dirty_worktree", state["blocked_category"])
        self.assertTrue((runs[0] / "BLOCKED").exists())

    @patch("cross_harness.runner.verify_codex_chatgpt", side_effect=AuthError("not logged in"))
    @patch("cross_harness.runner.verify_codex_config_ownership")
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
            "status": "success", "work_completed": "changed", "changed_files": [],
            "tests": [], "error": None, "next_decision": None,
        }))
        role = {"model": "gpt-5.6-terra", "effort": "medium", "output_limit_chars": 8000}
        summary = finalize_run(run, "implementer", role, "implementation", self.repo, 0, 1)
        by_file = {item["file"]: item for item in summary["diff_summary"]}
        self.assertEqual("1", by_file["README.md"]["added"])
        self.assertTrue(by_file["new.txt"]["untracked"])
        self.assertIn("diff_stat", (run / "summary.txt").read_text())

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
        read_only = {
            "harness": "claude", "model": "sonnet", "effort": "high", "write": False,
        }
        command = _claude_command(Path("/usr/local/bin/claude"), read_only, run)
        self.assertEqual("-p", command[1])
        self.assertEqual("stream-json", command[command.index("--output-format") + 1])
        self.assertIn("--verbose", command)
        self.assertEqual("manual", command[command.index("--permission-mode") + 1])
        self.assertEqual("Bash,Read,Grep,Glob", command[command.index("--allowedTools") + 1])
        disallowed_index = command.index("--disallowed-tools")
        self.assertEqual(["Edit", "Write", "NotebookEdit"], command[disallowed_index + 1:disallowed_index + 4])
        self.assertEqual("sonnet", command[command.index("--model") + 1])
        self.assertNotIn("-C", command)
        self.assertNotIn("bypassPermissions", command)
        instruction = command[command.index("--append-system-prompt") + 1]
        self.assertIn("only a JSON object", instruction)
        self.assertIn("Do not write the result to a file", instruction)
        self.assertNotIn(str(run / "final.json"), instruction)

        resumed = _claude_command(Path("/usr/local/bin/claude"), read_only, run, "session-1")
        self.assertEqual("session-1", resumed[resumed.index("--resume") + 1])

        writable = dict(read_only, write=True)
        write_command = _claude_command(Path("/usr/local/bin/claude"), writable, run)
        self.assertEqual("acceptEdits", write_command[write_command.index("--permission-mode") + 1])
        self.assertEqual("Bash,Read,Grep,Glob", write_command[write_command.index("--allowedTools") + 1])
        self.assertNotIn("--disallowed-tools", write_command)

    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner._invoke_safe")
    def test_claude_delegation_selects_claude_auth_and_executor(self, invoke, verify_claude, ownership, verify_codex):
        verify_claude.return_value = (Path("/usr/local/bin/claude"), False)

        def complete(command, task, env, cwd, run_dir, timeout):
            self.assertEqual("claude", env["CROSS_HARNESS_EXECUTOR"])
            self.assertEqual("manual", command[command.index("--permission-mode") + 1])
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
        self.assertEqual(["README.md"], summary["changed_files"])
        self.assertEqual(["review"], summary["tests"])
        self.assertEqual("ship it", summary["next_decision"])
        self.assertTrue((run / "final.json").exists())
        self.assertEqual("claude", json.loads((run / "execution.json").read_text())["harness"])
        self.assertFalse(json.loads((run / "execution.json").read_text())["write"])
        verify_claude.assert_called_once()
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

    def test_unparseable_claude_result_does_not_create_final_json(self):
        run = self.root / "claude-invalid-result"
        run.mkdir()
        (run / "events.jsonl").write_text(
            '{"type":"result","is_error":false,"result":"not JSON"}\n'
        )
        (run / "stderr.log").write_text("")

        _write_claude_final_from_events(run)

        self.assertFalse((run / "final.json").exists())
        role = {"model": "sonnet", "effort": "high", "output_limit_chars": 8000, "write": False}
        summary = finalize_run(run, "reviewer", role, "review", self.repo, 0, 1)
        self.assertEqual("success", summary["status"])
        self.assertEqual("", summary["work_completed"])

    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner._invoke_safe")
    def test_claude_write_role_records_write_authorization(self, invoke, verify_claude):
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
            self.assertEqual("acceptEdits", command[command.index("--permission-mode") + 1])
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

    @patch("cross_harness.runner.verify_claude_subscription")
    @patch("cross_harness.runner._invoke_safe")
    def test_claude_retry_resumes_recorded_session(self, invoke, verify_claude):
        config = self.root / "claude-retry.toml"
        default = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
        config.write_text(default.replace(
            '[roles.reviewer]\nharness = "claude"\nmodel = "sonnet"\neffort = "high"\nmax_parallel = 1\nretries = 0',
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

    def test_escalation_uses_claude_fallback_and_effort_order(self):
        role = {"harness": "claude", "model": "sonnet", "effort": "xhigh"}
        escalated = _escalated_role(role, default_config())
        self.assertEqual("fable", escalated["model"])
        self.assertEqual("max", escalated["effort"])

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
            ("rate_limit", '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}\n'),
            ("authentication", '{"type":"result","is_error":true,"subtype":"error_during_execution","error":"authentication_failed","result":"redacted"}\n'),
        )
        role = {"harness": "claude", "model": "sonnet", "effort": "high", "output_limit_chars": 8000, "write": False}
        for category, events in cases:
            with self.subTest(category=category):
                run = self.root / f"claude-{category}-run"
                run.mkdir()
                (run / "events.jsonl").write_text(events)
                (run / "stderr.log").write_text("")

                summary = finalize_run(run, "reviewer", role, "review", self.repo, 1, 1)

                self.assertEqual("blocked", summary["status"])
                state = json.loads((run / "state.json").read_text())
                self.assertEqual(category, state["blocked_category"])
                with self.assertRaisesRegex(HarnessError, "blocked runs cannot be retried"):
                    retry(run, self.task, home=self.home)

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
        config.write_text(default.replace('dirty_worktree_policy = "stop"', 'dirty_worktree_policy = "isolate"', 1))
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
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = login ]; then echo 'Logged in using ChatGPT'; exit 0; fi\n"
            "output=''\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = -o ]; then shift; output=$1; fi\n"
            "  shift\n"
            "done\n"
            "printf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"00000000-0000-0000-0000-000000000001\"}' '{\"type\":\"turn.completed\",\"usage\":{}}'\n"
            "printf '%s\\n' '{\"status\":\"success\",\"work_completed\":\"finished\",\"changed_files\":[],\"tests\":[],\"error\":null,\"next_decision\":null}' > \"$output\"\n"
        )
        fake_codex.chmod(0o755)
        clean_interpreter = self.root / "clean-python"
        clean_interpreter.write_text(
            "#!/bin/sh\n"
            "exec \"$CROSS_HARNESS_TEST_PYTHON\" -S -c '\n"
            "import os, runpy, sys\n"
            "from pathlib import Path\n"
            "import cross_harness.runner\n"
            "cross_harness.runner.verify_codex_chatgpt = lambda *args: (Path(os.environ[\"TEST_FAKE_CODEX\"]), False)\n"
            "cross_harness.runner.verify_codex_config_ownership = lambda *args: None\n"
            "sys.argv = [\"cross-harness\", *sys.argv[3:]]\n"
            "runpy.run_module(\"cross_harness.cli\", run_name=\"__main__\")\n"
            "' \"$@\"\n"
        )
        clean_interpreter.chmod(0o755)
        environment = {
            "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
            "CROSS_HARNESS_TEST_PYTHON": sys.executable,
            "TEST_FAKE_CODEX": str(fake_codex),
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "cross_harness.runner.sys.executable", str(clean_interpreter)
        ):
            run_dir = start_detached_delegate("tester", "test", self.task, self.repo, home=self.home)
        summary = wait_for_run(run_dir, 5)
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual("success", summary["status"])
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
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = login ]; then echo 'Logged in using ChatGPT'; exit 0; fi\n"
            "touch \"$CROSS_HARNESS_RUN_DIR/fake-running\"\n"
            "output=''\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = -o ]; then shift; output=$1; fi\n"
            "  shift\n"
            "done\n"
            "sleep 1\n"
            "printf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"00000000-0000-0000-0000-000000000001\"}' '{\"type\":\"turn.completed\",\"usage\":{}}'\n"
            "printf '%s\\n' '{\"status\":\"success\",\"work_completed\":\"finished after detach\",\"changed_files\":[],\"tests\":[],\"error\":null,\"next_decision\":null}' > \"$output\"\n"
        )
        fake_codex.chmod(0o755)
        injector = self.root / "injector"
        injector.mkdir()
        (injector / "sitecustomize.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "import cross_harness.runner\n"
            "cross_harness.runner.verify_codex_chatgpt = lambda *args: (Path(os.environ['TEST_FAKE_CODEX']), False)\n"
            "cross_harness.runner.verify_codex_config_ownership = lambda *args: None\n"
        )
        environment = os.environ.copy()
        environment.pop("CROSS_HARNESS_ACTIVE", None)
        environment.update({
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": os.pathsep.join([str(injector), str(Path(__file__).resolve().parents[1] / "src")]),
            "TEST_FAKE_CODEX": str(fake_codex),
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
        self.assertEqual("success", summary["status"])
        self.assertTrue((run_dir / "final.json").is_file())

    @patch("cross_harness.runner.failure_signature", return_value="same-signature")
    @patch("cross_harness.runner.verify_codex_chatgpt")
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_identical_failure_triggers_exactly_one_escalation(self, invoke, ownership, verify, signature):
        previous = self.root / "previous"
        previous.mkdir()
        (previous / "state.json").write_text(json.dumps({
            "role": "tester", "kind": "test", "cwd": str(self.repo),
            "thread_id": "00000000-0000-0000-0000-000000000001",
            "attempts": 1, "signatures": ["same-signature"], "escalated": False,
            "status": "failed", "model": "gpt-5.6-luna", "effort": "medium",
        }))
        verify.return_value = (Path("/usr/bin/true"), False)

        def fail(command, task, env, cwd, run_dir, timeout):
            self.assertEqual("1", env["CROSS_HARNESS_ACTIVE"])
            self.assertEqual("codex", env["CROSS_HARNESS_EXECUTOR"])
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
        result = retry(previous, self.task, home=self.home)
        self.assertEqual(2, invoke.call_count)
        self.assertEqual("gpt-5.6-terra", result["model"])
        escalated = json.loads((Path(result["run_dir"]) / "state.json").read_text())
        self.assertTrue(escalated["escalated"])

    @patch("cross_harness.runner.verify_codex_chatgpt", side_effect=AuthError("expired"))
    @patch("cross_harness.runner.verify_codex_config_ownership")
    @patch("cross_harness.runner._invoke_safe")
    def test_retry_auth_failure_preserves_attempt_history(self, invoke, ownership, verify):
        previous = self.root / "retry-auth-previous"
        previous.mkdir()
        (previous / "state.json").write_text(json.dumps({
            "role": "tester", "kind": "test", "cwd": str(self.repo),
            "thread_id": "00000000-0000-0000-0000-000000000001",
            "attempts": 1, "signatures": ["prior"], "escalated": False,
            "status": "failed", "model": "gpt-5.6-luna", "effort": "medium",
        }))
        summary = retry(previous, self.task, home=self.home)
        state = json.loads((Path(summary["run_dir"]) / "state.json").read_text())
        self.assertEqual("blocked", state["status"])
        self.assertEqual(1, state["attempts"])
        self.assertEqual(["prior"], state["signatures"])
        invoke.assert_not_called()



if __name__ == "__main__":
    unittest.main()
