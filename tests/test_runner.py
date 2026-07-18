from pathlib import Path
from unittest.mock import patch
import json
import os
import subprocess
import sys
import tempfile
import unittest

from cross_harness.errors import AuthError, DirtyWorktreeError, HarnessError
from cross_harness.runner import _invoke_safe, delegate, retry
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

    def test_task_file_with_credential_material_is_rejected(self):
        self.task.write_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n")
        with self.assertRaises(HarnessError):
            delegate("tester", "test", self.task, self.repo, home=self.home)

    def test_active_executor_cannot_nest_wrapper_delegation(self):
        with patch.dict(os.environ, {"CROSS_HARNESS_ACTIVE": "1"}):
            with self.assertRaisesRegex(HarnessError, "nested cross-harness"):
                delegate("tester", "test", self.task, self.repo, home=self.home)

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
