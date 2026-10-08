from pathlib import Path
from unittest.mock import patch
import json
import os
import subprocess
import tempfile
import unittest

import cross_harness.runner as runner
from cross_harness.config import default_config
from cross_harness.errors import AuthError, DirtyWorktreeError, HarnessError
from cross_harness.summarize import render_summary


THREAD = "00000000-0000-0000-0000-000000000001"
POINTS = ["Concern: shared logic. Evidence: runner.py:1. Proposal: reuse retry."]


class DiscussionTests(unittest.TestCase):
    def test_executor_charters_state_scope_rule_once(self):
        for charter in (runner.CLAUDE_EXECUTOR_CHARTER, runner.CODEX_EXECUTOR_CHARTER):
            with self.subTest(charter=charter):
                self.assertEqual(1, charter.count("broaden scope on your own"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (
            ("init",), ("config", "user.email", "test@example.com"),
            ("config", "user.name", "Test"),
        ):
            self._git(*args)
        (self.repo / "README.md").write_text("before\n")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self.task = self.root / "reply.md"
        self.task.write_text("Accept the proposal; implement the shared path.\n")
        self.auth = {}
        for name in (
            "verify_codex_config_ownership", "verify_claude_config_ownership",
            "verify_codex_chatgpt", "verify_claude_subscription",
        ):
            mock = patch("cross_harness.runner." + name)
            self.auth[name] = mock.start()
            self.addCleanup(mock.stop)
        self.auth["verify_codex_chatgpt"].return_value = (Path("/fixture/codex"), False)
        self.auth["verify_claude_subscription"].return_value = (Path("/fixture/claude"), False)
        self.invoke_patch = patch("cross_harness.runner._invoke_safe", side_effect=self._discuss)
        self.invoke = self.invoke_patch.start()
        self.addCleanup(self.invoke_patch.stop)

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, check=True)

    def _config(self, limit=3, policy="allow_delegated"):
        path = self.root / "config.toml"
        path.write_text(f'max_discussion_rounds = {limit}\ndirty_worktree_policy = "{policy}"\n')
        return path

    @staticmethod
    def _artifacts(run, status="discussion", *, events=True):
        (run / "events.jsonl").write_text(
            json.dumps({"type": "thread.started", "thread_id": THREAD}) + "\n" if events else ""
        )
        (run / "stderr.log").write_text("")
        (run / "final.json").write_text(json.dumps({
            "status": status, "work_completed": "inspected", "changed_files": [],
            "tests": [], "error": None, "next_decision": None, "discussion_points": POINTS,
        }))

    def _discuss(self, command, task, env, cwd, run, timeout):
        self._artifacts(run)
        return 0

    def _run(self, *, role="implementer", rounds=0, attempts=1, limit=3):
        run = self.root / "previous"
        run.mkdir()
        runner._write_baseline(run, self.repo)
        self._artifacts(run)
        settings = default_config()["roles"][role]
        runner.finalize_run(
            run, role, settings, "implementation" if settings["write"] else "review",
            self.repo, 0, attempts, discussion_rounds=rounds, max_discussion_rounds=limit,
        )
        return run

    @staticmethod
    def _state(run):
        return json.loads((run / "state.json").read_text())

    def test_delegate_discussion_finalizes_and_wait_returns_points_and_configured_limit(self):
        config = self._config(2)
        summary = runner.delegate(
            "implementer", "implementation", self.task, self.repo, config, self.home,
        )
        run = Path(summary["run_dir"])
        self.assertEqual("discussion", summary["status"])
        self.assertEqual(POINTS, summary["discussion_points"])
        self.assertEqual(0, summary["discussion_rounds"])
        self.assertEqual(2, summary["max_discussion_rounds"])
        self.assertEqual(summary, runner.wait_for_run(run, 0))
        state = self._state(run)
        self.assertEqual(POINTS, state["discussion_points"])
        self.assertEqual(0, state["discussion_rounds"])
        self.assertEqual(1, state["attempts"])
        self.assertNotIn("blocked_category", state)
        self.assertFalse((run / "BLOCKED").exists())
        self.assertIn("discussion_rounds: 0/2", (run / "summary.txt").read_text())

    def test_discussion_without_a_nonempty_point_finalizes_as_failed(self):
        run = self._run()
        final = json.loads((run / "final.json").read_text())
        for points in ([], [""], [" \n\t"], [None, 1], None, "concern"):
            with self.subTest(points=points):
                (run / "final.json").write_text(json.dumps({**final, "discussion_points": points}))
                summary = runner.finalize_run(
                    run, "implementer", default_config()["roles"]["implementer"],
                    "implementation", self.repo, 0, 1,
                )
                self.assertEqual("failed", summary["status"])
                self.assertIn("a discussion needs at least one point", summary["error"])
                self.assertEqual("failed", self._state(run)["status"])
                self.assertNotIn("blocked_category", self._state(run))
        del final["discussion_points"]
        (run / "final.json").write_text(json.dumps(final))
        summary = runner.finalize_run(
            run, "implementer", default_config()["roles"]["implementer"],
            "implementation", self.repo, 0, 1,
        )
        self.assertEqual("failed", summary["status"])
        self.assertIn("a discussion needs at least one point", summary["error"])

    def test_readonly_discussion_after_modifying_worktree_is_failed(self):
        run = self._run(role="reviewer")
        (self.repo / "README.md").write_text("unauthorized change\n")
        summary = runner.finalize_run(
            run, "reviewer", default_config()["roles"]["reviewer"], "review", self.repo, 0, 1,
        )
        self.assertEqual("failed", summary["status"])
        self.assertIn("read-only role modified the worktree", summary["error"])
        self.assertEqual(POINTS, summary["discussion_points"])
        self.assertNotIn("blocked_category", self._state(run))

    def test_reply_stores_effective_task_and_inherits_checks_across_multiple_replies(self):
        run = self._run()
        checks = ["fixture-original", "fixture-second"]
        original_task = "# Goal\nImplement.\n\n# Checks\n- `fixture-original`\n- fixture-second\n"
        (run / "task.md").write_text(original_task)
        reply_text = self.task.read_text()

        def complete_or_discuss(command, task, env, cwd, new_run, timeout):
            effective = (new_run / "task.md").read_text()
            self.assertEqual(runner._executor_task(effective, env["CROSS_HARNESS_EXECUTOR"]), task)
            self.assertEqual(checks, runner._declared_checks(new_run))
            self.assertIn(reply_text, effective)
            round_number = self.invoke.call_count
            self.assertTrue(effective.startswith(f"Discussion round {round_number} of 3.\n\n"))
            self._artifacts(new_run, "success" if round_number == 3 else "discussion")
            if round_number == 3:
                with (new_run / "events.jsonl").open("a") as events:
                    for check in checks:
                        events.write(json.dumps({"type": "item.completed", "item": {
                            "type": "command_execution", "command": check,
                            "status": "completed", "exit_code": 0,
                        }}) + "\n")
            return 0

        self.invoke.side_effect = complete_or_discuss
        initial_run = run
        for round_number in (1, 2, 3):
            summary = runner.reply(run, self.task, home=self.home)
            run = Path(summary["run_dir"])
            self.assertEqual("success" if round_number == 3 else "discussion", summary["status"])
            self.assertEqual(round_number, summary["discussion_rounds"])
            self.assertEqual(1, summary["attempt"])
        self.assertEqual([{"check": check, "status": "passed", "exit_code": 0} for check in checks], summary["checks"])
        self.assertIsNone(summary["error"])
        self.assertEqual(original_task, (initial_run / "task.md").read_text())
        self.assertEqual(reply_text, self.task.read_text())

    def test_reply_with_own_checks_keeps_them_and_inherits_nothing(self):
        run = self._run()
        (run / "task.md").write_text("# Checks\n- fixture-original\n")
        reply_text = "# Goal\nUse the replacement check.\n\n# Checks\n- `fixture-replacement`\n"
        self.task.write_text(reply_text)

        def complete(command, task, env, cwd, new_run, timeout):
            effective = (new_run / "task.md").read_text()
            self.assertEqual("Discussion round 1 of 3.\n\n" + reply_text, effective)
            self.assertEqual(runner._executor_task(effective, env["CROSS_HARNESS_EXECUTOR"]), task)
            self.assertNotIn("fixture-original", effective)
            self._artifacts(new_run, "success")
            with (new_run / "events.jsonl").open("a") as events:
                events.write(json.dumps({"type": "item.completed", "item": {
                    "type": "command_execution", "command": "fixture-replacement",
                    "status": "completed", "exit_code": 0,
                }}) + "\n")
            return 0

        self.invoke.side_effect = complete
        summary = runner.reply(run, self.task, home=self.home)
        self.assertEqual("success", summary["status"])
        self.assertEqual([{"check": "fixture-replacement", "status": "passed", "exit_code": 0}], summary["checks"])
        self.assertEqual(reply_text, self.task.read_text())

    def test_reply_inherits_checks_into_an_empty_checks_section(self):
        run = self._run()
        (run / "task.md").write_text("# Checks\n- fixture-original\n")
        for reply_text in (
            "# Goal\nProceed.\n\n# Checks\n\n# References\nrunner.py\n",
            "# Goal\nProceed.\n\n## Checks",
        ):
            with self.subTest(reply_text=reply_text):
                self.task.write_text(reply_text)
                summary = runner.reply(run, self.task, home=self.home)
                new_run = Path(summary["run_dir"])
                self.assertEqual(["fixture-original"], runner._declared_checks(new_run))
                self.assertEqual(
                    runner._executor_task((new_run / "task.md").read_text(), "codex"),
                    self.invoke.call_args.args[1],
                )
                self.assertEqual("discussion", summary["status"])

    def test_reply_completion_is_still_verified_against_unrun_inherited_checks(self):
        run = self._run()
        (run / "task.md").write_text("# Checks\n- fixture-original\n")

        def complete(command, task, env, cwd, new_run, timeout):
            self._artifacts(new_run, "success")
            return 0

        self.invoke.side_effect = complete
        summary = runner.reply(run, self.task, home=self.home)
        self.assertEqual("partial", summary["status"])
        self.assertIn("declared check not run: fixture-original", summary["error"])
        self.assertNotIn("no checks declared", summary["error"])

    def test_retry_keeps_raw_task_storage_and_does_not_inherit_checks(self):
        run = self._run()
        (run / "task.md").write_text("# Checks\n- fixture-original\n")
        self._artifacts(run, "failed")
        runner.finalize_run(
            run, "implementer", default_config()["roles"]["implementer"],
            "implementation", self.repo, 1, 1,
        )
        raw_task = self.task.read_bytes()
        summary = runner.retry(run, self.task, home=self.home)
        new_run = Path(summary["run_dir"])
        self.assertEqual(raw_task, (new_run / "task.md").read_bytes())
        self.assertEqual([], runner._declared_checks(new_run))
        self.assertEqual(runner._executor_task(raw_task.decode(), "codex"), self.invoke.call_args.args[1])

    def test_reply_codex_resumes_recorded_model_and_thread_without_spending_retry_budget(self):
        run = self._run(rounds=1, attempts=3)
        state = self._state(run)
        state.update(model="recorded-model", effort="xhigh", escalated=True, signatures=["old"])
        (run / "state.json").write_text(json.dumps(state))
        summary = runner.reply(run, self.task, home=self.home)
        command, task, env, cwd, new_run, timeout = self.invoke.call_args.args
        self.assertIn("resume", command)
        self.assertIn(THREAD, command)
        self.assertEqual("recorded-model", command[command.index("-m") + 1])
        self.assertIn('sandbox_mode="workspace-write"', command)
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertEqual("1", env["CROSS_HARNESS_ACTIVE"])
        self.assertIn("# Delegated task\n\nDiscussion round 2 of 3.\n\n" + self.task.read_text(), task)
        self.assertEqual(3, summary["attempt"])
        self.assertEqual(2, summary["discussion_rounds"])
        self.assertEqual(THREAD, summary["thread_id"])
        new_state = self._state(new_run)
        self.assertEqual(3, new_state["attempts"])
        self.assertEqual(2, new_state["discussion_rounds"])
        self.assertEqual(["old"], new_state["signatures"])
        self.assertTrue(new_state["escalated"])
        self.assertEqual(state, self._state(run))
        command_record = json.loads((new_run / "command.json").read_text())
        self.assertEqual(THREAD, command_record["resume"])
        self.assertFalse(command_record["user_decided"])
        self.assertFalse(summary["user_decided"])
        following = runner.reply(new_run, self.task, home=self.home)
        self.assertEqual(3, following["attempt"])
        self.assertEqual(3, following["discussion_rounds"])

    def test_reply_claude_resumes_session_and_preserves_thread_when_events_omit_it(self):
        run = self._run(role="reviewer")

        def complete(command, task, env, cwd, new_run, timeout):
            self._artifacts(new_run, "success", events=False)
            return 0

        self.invoke.side_effect = complete
        summary = runner.reply(run, self.task, home=self.home)
        command, task, env, *_ = self.invoke.call_args.args
        self.assertEqual(THREAD, command[command.index("--resume") + 1])
        self.assertEqual("manual", command[command.index("--permission-mode") + 1])
        self.assertEqual("Discussion round 1 of 3.\n\n" + self.task.read_text(), task)
        self.assertEqual("claude", env["CROSS_HARNESS_EXECUTOR"])
        self.assertEqual("success", summary["status"])
        self.assertEqual(POINTS, summary["discussion_points"])
        self.assertEqual(THREAD, summary["thread_id"])
        self.assertEqual(1, self._state(Path(summary["run_dir"]))["attempts"])

    def test_reply_uses_recorded_harness_when_role_configuration_changes(self):
        run = self._run()
        config = self._config()
        with config.open("a") as file:
            file.write('[roles.implementer]\nharness = "claude"\nmodel = "haiku"\n')
        runner.reply(run, self.task, config, self.home)
        self.assertEqual("/fixture/codex", self.invoke.call_args.args[0][0])
        self.auth["verify_codex_chatgpt"].assert_called_once()
        self.auth["verify_claude_subscription"].assert_not_called()

    def test_reply_requires_discussion_and_recorded_thread_even_with_user_decision(self):
        run = self._run()
        state = self._state(run)
        for status in ("success", "failed", "blocked", "partial", "running"):
            with self.subTest(status=status):
                (run / "state.json").write_text(json.dumps({**state, "status": status}))
                with self.assertRaisesRegex(HarnessError, "run status must be discussion"):
                    runner.reply(run, self.task, home=self.home, user_decided=True)
        (run / "state.json").write_text(json.dumps({**state, "thread_id": None}))
        with self.assertRaisesRegex(HarnessError, "no recorded thread; start a new delegation"):
            runner.reply(run, self.task, home=self.home, user_decided=True)
        self.invoke.assert_not_called()
        self.auth["verify_codex_chatgpt"].assert_not_called()

    def test_retry_refuses_discussion_and_names_reply(self):
        run = self._run(attempts=3)
        with self.assertRaisesRegex(HarnessError, "discussion run requires reply"):
            runner.retry(run, self.task, home=self.home)
        self.invoke.assert_not_called()

    def test_reply_limit_refuses_before_launch_and_user_decision_only_bypasses_limit(self):
        run = self._run(rounds=3)
        for limit, rounds in ((3, 3), (0, 0)):
            with self.subTest(limit=limit):
                state = self._state(run)
                state["discussion_rounds"] = rounds
                (run / "state.json").write_text(json.dumps(state))
                config = self._config(limit)
                self.invoke.reset_mock()
                with self.assertRaisesRegex(HarnessError, "max_discussion_rounds reached.*reply --user-decided"):
                    runner.reply(run, self.task, config, self.home)
                self.invoke.assert_not_called()
                self.assertEqual(state, self._state(run))
                summary = runner.reply(run, self.task, config, self.home, user_decided=True)
                new_run = Path(summary["run_dir"])
                task = self.invoke.call_args.args[1]
                header = task.split("# Delegated task\n\n", 1)[1].splitlines()[0]
                self.assertEqual(
                    f"Discussion round {rounds + 1} of {limit}. "
                    "This decision comes from the user and is final.", header,
                )
                self.assertEqual(rounds + 1, summary["discussion_rounds"])
                self.assertEqual(limit, summary["max_discussion_rounds"])
                self.assertTrue(summary["user_decided"])
                self.assertTrue(json.loads((new_run / "command.json").read_text())["user_decided"])
                self.assertEqual(state["attempts"], self._state(new_run)["attempts"])

    def test_reply_blocks_active_executor_even_with_user_decision(self):
        with patch.dict(os.environ, {"CROSS_HARNESS_ACTIVE": "1"}), patch(
            "cross_harness.runner.load_config"
        ) as config:
            with self.assertRaisesRegex(HarnessError, "nested cross-harness reply"):
                runner.reply(self.root / "missing", self.task, user_decided=True)
        config.assert_not_called()
        self.invoke.assert_not_called()

    def test_reply_screens_missing_empty_credential_and_secret_files_with_user_decision(self):
        run = self._run()
        for name, contents, error in (
            ("missing.md", None, "task file not found"),
            ("empty.md", " \n", "task file is empty"),
            ("AUTH.JSON", "fixture", "credential or environment files"),
            (".env", "fixture", "credential or environment files"),
            ("credentials.json", "fixture", "credential or environment files"),
            ("secret.md", "OPENAI_API_KEY=dummy-secret-value-for-fixture", "credential material"),
        ):
            with self.subTest(name=name):
                task = self.root / name
                if contents is not None:
                    task.write_text(contents)
                with self.assertRaisesRegex(HarnessError, error):
                    runner.reply(run, task, home=self.home, user_decided=True)
        self.invoke.assert_not_called()
        self.auth["verify_codex_chatgpt"].assert_not_called()
        self.assertFalse((self.home / ".local/state/cross-harness/runs").exists())

    def test_reply_does_not_escalate_repeated_failures_and_retry_carries_rounds(self):
        run = self._run(rounds=1)
        state = self._state(run)
        state["signatures"] = ["same"]
        (run / "state.json").write_text(json.dumps(state))

        def fail(command, task, env, cwd, new_run, timeout):
            self._artifacts(new_run, "failed")
            return 1

        self.invoke.side_effect = fail
        with patch("cross_harness.runner.failure_signature", return_value="same"), patch(
            "cross_harness.runner._escalated_role", wraps=runner._escalated_role
        ) as escalate:
            summary = runner.reply(run, self.task, home=self.home)
            escalate.assert_not_called()
            self.assertEqual(1, self.invoke.call_count)
            new_run = Path(summary["run_dir"])
            self.assertEqual(["same", "same"], self._state(new_run)["signatures"])
            self.assertEqual(1, summary["attempt"])
            self.assertEqual(2, summary["discussion_rounds"])
            retried = runner.retry(new_run, self.task, home=self.home)
            self.assertEqual(3, retried["attempt"])
            self.assertEqual(2, retried["discussion_rounds"])
            self.assertEqual(2, self._state(Path(retried["run_dir"]))["discussion_rounds"])
            escalate.assert_called_once()

    def test_reply_reuses_isolated_worktree_and_discussion_adoption_is_refused(self):
        config = self._config(policy="isolate")

        def edit(command, task, env, cwd, new_run, timeout):
            (cwd / "README.md").write_text("delegated\n")
            return self._discuss(command, task, env, cwd, new_run, timeout)

        self.invoke.side_effect = edit
        initial = runner.delegate("implementer", "implementation", self.task, self.repo, config, self.home)
        previous = Path(initial["run_dir"])
        worktree = (previous / "ISOLATED_WORKTREE").read_text()
        self.invoke.side_effect = self._discuss
        answered = runner.reply(previous, self.task, config, self.home)
        new_run = Path(answered["run_dir"])
        self.assertEqual(worktree, (new_run / "ISOLATED_WORKTREE").read_text())
        self.assertEqual(Path(worktree.strip()), self.invoke.call_args.args[3])
        for run in (previous, new_run):
            with self.subTest(run=run), self.assertRaisesRegex(HarnessError, "discussion run awaits a reply"):
                runner.adopt(run, config, self.home)
        self.assertEqual("before\n", (self.repo / "README.md").read_text())
        self.assertEqual("delegated\n", (Path(worktree.strip()) / "README.md").read_text())

    def test_reply_parallel_limit_preserves_attempts_and_discussion_rounds(self):
        run = self._run(rounds=1, attempts=2)
        config = self._config()
        with config.open("a") as file:
            file.write("max_parallel = 1\n")
        runtime = self.home / ".local/state/cross-harness"
        live = runtime / "runs/live"
        live.mkdir(parents=True)
        (live / "role").write_text("implementer\n")
        (live / "supervisor.pid").write_text("123\n")
        with patch.object(runner, "_supervisor_alive", return_value=True):
            summary = runner.reply(run, self.task, config, self.home)
        state = self._state(Path(summary["run_dir"]))
        self.assertEqual("blocked", summary["status"])
        self.assertIn("global max_parallel limit 1", summary["error"])
        self.assertEqual("parallel_limit", state["blocked_category"])
        self.assertEqual(2, summary["attempt"])
        self.assertEqual(2, state["attempts"])
        self.assertEqual(2, summary["discussion_rounds"])
        self.assertEqual(2, state["discussion_rounds"])
        self.assertEqual(1, self._state(run)["discussion_rounds"])
        self.invoke.assert_not_called()

    def test_reply_reuses_root_delta_and_releases_lock_on_exception(self):
        run = self._run()
        (self.repo / "README.md").write_text("previous change\n")
        runner.finalize_run(
            run, "implementer", default_config()["roles"]["implementer"],
            "implementation", self.repo, 0, 1,
        )
        self.invoke.side_effect = RuntimeError("fixture launch failure")
        with self.assertRaisesRegex(RuntimeError, "fixture launch failure"):
            runner.reply(run, self.task, home=self.home)
        runtime = self.home / ".local/state/cross-harness"
        self.assertNotIn(runner._root_lock_path(runtime, self.repo), runner._HELD_ROOT_LOCKS)
        descriptor = runner._try_lock(runner._root_lock_path(runtime, self.repo))
        self.assertIsNotNone(descriptor)
        runner._release_lock(descriptor)

    def test_reply_preserves_dirty_worktree_guard_and_records_rounds_when_blocked(self):
        run = self._run(rounds=1)
        (self.repo / "unrelated.txt").write_text("user change\n")
        with self.assertRaisesRegex(DirtyWorktreeError, "outside the previous run's recorded diff"):
            runner.reply(run, self.task, home=self.home, user_decided=True)
        self.invoke.assert_not_called()
        new_run = next((self.home / ".local/state/cross-harness/runs").iterdir())
        state = self._state(new_run)
        self.assertEqual(1, state["attempts"])
        self.assertEqual(2, state["discussion_rounds"])
        self.assertEqual("dirty_worktree", state["blocked_category"])
        summary = json.loads((new_run / "summary.json").read_text())
        self.assertTrue(summary["user_decided"])
        self.assertEqual([], summary["discussion_points"])
        self.assertEqual("user change\n", (self.repo / "unrelated.txt").read_text())

    def test_reply_auth_failure_preserves_attempts_rounds_and_safety_stop(self):
        run = self._run(rounds=2, attempts=2)
        self.auth["verify_codex_chatgpt"].side_effect = AuthError("fixture authentication failure")
        summary = runner.reply(run, self.task, home=self.home)
        state = self._state(Path(summary["run_dir"]))
        self.assertEqual("authentication", state["blocked_category"])
        self.assertEqual(2, state["attempts"])
        self.assertEqual(3, state["discussion_rounds"])
        self.invoke.assert_not_called()

    def test_summary_keeps_all_points_and_round_count_when_body_is_truncated(self):
        run = self._run()
        summary = json.loads((run / "summary.json").read_text())
        points = ["First concern " + "x" * 1200, "Last concern with evidence and proposal."]
        summary.update(discussion_points=points, discussion_rounds=4, max_discussion_rounds=3)
        rendered = render_summary(summary, 1000)
        self.assertIn("discussion_rounds: 4/3", rendered)
        for point in points:
            self.assertIn(point, rendered)
        self.assertLess(rendered.index(points[-1]), rendered.index("[summary truncated"))

    def test_summary_omits_discussion_lines_for_ordinary_runs(self):
        run = self._run()
        summary = json.loads((run / "summary.json").read_text())
        summary.update(discussion_points=[], discussion_rounds=0)
        for limit in (1000, 10_000):
            with self.subTest(limit=limit):
                rendered = render_summary(summary, limit)
                self.assertNotIn("discussion_points:", rendered)
                self.assertNotIn("discussion_rounds:", rendered)
                self.assertTrue(rendered.startswith(
                    f"status: discussion\nrun_dir: {run}\nexit_code: 0\n"
                ))
                self.assertLessEqual(len(rendered), limit)

    def test_summary_retains_discussion_lines_for_points_or_completed_rounds(self):
        run = self._run()
        summary = json.loads((run / "summary.json").read_text())
        for points, rounds in (([], 1), (POINTS, 0)):
            with self.subTest(points=points, rounds=rounds):
                summary.update(discussion_points=points, discussion_rounds=rounds)
                rendered = render_summary(summary, 10_000)
                self.assertIn(f"discussion_rounds: {rounds}/3", rendered)
                self.assertIn("discussion_points:\n", rendered)
                self.assertLess(rendered.index("discussion_points:"), rendered.index("exit_code:"))
                for point in points:
                    self.assertIn(point, rendered)


if __name__ == "__main__":
    unittest.main()
