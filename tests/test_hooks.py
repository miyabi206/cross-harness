from io import StringIO
from unittest.mock import patch
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest

from cross_harness.hooks import claude_pre_tool_use, claude_session_start, codex_pre_tool_use
from cross_harness.installer import install
from cross_harness.paths import source_root


class HookTests(unittest.TestCase):
    def _run(self, function, payload):
        with patch("sys.stdin", StringIO(payload)), patch("sys.stderr", new_callable=StringIO) as stderr:
            code = function()
            return code, stderr.getvalue()

    def test_claude_direct_edit_and_direct_codex_are_blocked(self):
        code, message = self._run(claude_pre_tool_use, '{"tool_name":"Edit","tool_input":{}}')
        self.assertEqual(2, code)
        self.assertIn("orchestrator", message)
        code, _ = self._run(claude_pre_tool_use, '{"tool_name":"Bash","tool_input":{"command":"/opt/bin/codex exec task"}}')
        self.assertEqual(2, code)

    def test_delegated_claude_read_only_blocks_edits_and_nested_executors(self):
        environment = {"CROSS_HARNESS_ACTIVE": "1", "CROSS_HARNESS_EXECUTOR": "claude"}
        with patch.dict("os.environ", environment, clear=True):
            code, message = self._run(claude_pre_tool_use, '{"tool_name":"Edit","tool_input":{}}')
            self.assertEqual(2, code)
            self.assertIn("read-only", message)
            for command in (
                "cross-harness task create --role tester",
                "/Users/example/.local/bin/cross-harness delegate --role tester",
                "codex exec nested",
                "claude -p nested",
            ):
                payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
                code, message = self._run(claude_pre_tool_use, payload)
                self.assertEqual(2, code)
                self.assertIn("nested executor", message)

    def test_delegated_claude_write_access_allows_edits_but_not_nested_executors(self):
        environment = {
            "CROSS_HARNESS_ACTIVE": "1",
            "CROSS_HARNESS_EXECUTOR": "claude",
            "CROSS_HARNESS_WRITE": "1",
        }
        with patch.dict("os.environ", environment, clear=True):
            self.assertEqual(0, self._run(claude_pre_tool_use, '{"tool_name":"Edit","tool_input":{}}')[0])
            self.assertEqual(0, self._run(claude_pre_tool_use, '{"tool_name":"Write","tool_input":{}}')[0])
            code, _ = self._run(
                claude_pre_tool_use,
                '{"tool_name":"Bash","tool_input":{"command":"claude -p nested"}}',
            )
            self.assertEqual(2, code)

    def test_codex_nested_claude_is_blocked(self):
        code, message = self._run(codex_pre_tool_use, '{"tool_name":"Bash","tool_input":{"command":"env /opt/homebrew/bin/claude -p hi"}}')
        self.assertEqual(2, code)
        self.assertIn("nested", message)
        code, _ = self._run(
            codex_pre_tool_use,
            '''{"tool_name":"Bash","tool_input":{"command":"bash -lc 'claude -p nested'"}}''',
        )
        self.assertEqual(2, code)

    def test_codex_nested_executor_is_blocked(self):
        for command in (
            "/opt/homebrew/bin/codex exec nested",
            "/Users/example/.local/bin/cross-harness delegate --role tester",
            "bash -lc 'cross-harness retry --run-dir /tmp/run'",
        ):
            payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
            code, message = self._run(codex_pre_tool_use, payload)
            self.assertEqual(2, code)
            self.assertIn("nested executor", message)

    def test_unrelated_commands_pass(self):
        code, _ = self._run(codex_pre_tool_use, '{"tool_name":"Bash","tool_input":{"command":"git status"}}')
        self.assertEqual(0, code)

    @patch("cross_harness.hooks.shutil.which", return_value="/tmp/not-the-installed-wrapper")
    def test_shadowed_bare_wrapper_is_blocked(self, which):
        code, message = self._run(
            claude_pre_tool_use,
            '{"tool_name":"Bash","tool_input":{"command":"cross-harness task create --role tester"}}',
        )
        self.assertEqual(2, code)
        self.assertIn("does not resolve", message)

    def test_environment_marker_blocks_nested_claude_session(self):
        with patch.dict("os.environ", {"CROSS_HARNESS_ACTIVE": "1"}, clear=True), patch("sys.stderr", new_callable=StringIO) as stderr:
            self.assertEqual(2, claude_session_start(Path("/tmp/nonexistent-home")))
            self.assertIn("nested Claude", stderr.getvalue())

    def test_codex_executor_blocks_nested_claude_session(self):
        environment = {"CROSS_HARNESS_ACTIVE": "1", "CROSS_HARNESS_EXECUTOR": "codex"}
        with patch.dict("os.environ", environment, clear=True), patch("sys.stderr", new_callable=StringIO) as stderr:
            self.assertEqual(2, claude_session_start(Path("/tmp/nonexistent-home")))
            self.assertIn("nested Claude", stderr.getvalue())

    @patch("cross_harness.hooks.cleanup")
    @patch("cross_harness.hooks.verify_codex_chatgpt")
    @patch("cross_harness.hooks.detected_api_keys")
    @patch("cross_harness.hooks.subprocess.run")
    @patch("cross_harness.hooks.synchronize_claude_agent_roles")
    def test_delegated_claude_session_skips_orchestrator_maintenance(self, sync, run, keys, verify, cleanup):
        environment = {"CROSS_HARNESS_ACTIVE": "1", "CROSS_HARNESS_EXECUTOR": "claude"}
        with patch.dict("os.environ", environment, clear=True), patch("sys.stdout", new_callable=StringIO) as stdout:
            self.assertEqual(0, claude_session_start(Path("/tmp/cross-harness-hook-home")))
        sync.assert_not_called()
        run.assert_not_called()
        keys.assert_not_called()
        verify.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual("", stdout.getvalue())

    @patch("cross_harness.hooks.cleanup")
    @patch("cross_harness.hooks.verify_codex_chatgpt")
    @patch("cross_harness.hooks.detected_api_keys", return_value=[])
    @patch("cross_harness.hooks.subprocess.run")
    def test_session_start_checks_both_harness_auth_states(self, run, keys, verify, cleanup):
        run.return_value = subprocess.CompletedProcess([], 0, '{"loggedIn": true}', "")
        with patch.dict("os.environ", {}, clear=True), patch("sys.stdout", new_callable=StringIO) as stdout:
            self.assertEqual(0, claude_session_start(Path("/tmp/cross-harness-hook-home")))
        verify.assert_called_once()
        self.assertIn("Subscription checks passed", stdout.getvalue())

    @patch("cross_harness.hooks.cleanup")
    @patch("cross_harness.hooks.verify_codex_chatgpt")
    @patch("cross_harness.hooks.detected_api_keys", return_value=[])
    @patch("cross_harness.hooks.subprocess.run")
    def test_session_start_synchronizes_claude_agents_from_updated_config(self, run, keys, verify, cleanup):
        run.return_value = subprocess.CompletedProcess([], 0, '{"loggedIn": true}', "")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            config = home / ".config/cross-harness/config.toml"
            contents = config.read_text(encoding="utf-8").replace('model = "haiku"', 'model = "next-explorer"')
            contents = contents.replace('effort = "low"', 'effort = "next-effort"')
            config.write_text(contents, encoding="utf-8")

            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(0, claude_session_start(home))

            explorer = (home / ".claude/agents/cross-harness-explorer.md").read_text()
            self.assertIn("model: next-explorer", explorer)
            self.assertIn("effort: next-effort", explorer)

    @patch("cross_harness.hooks.cleanup")
    @patch("cross_harness.hooks.verify_codex_chatgpt")
    @patch("cross_harness.hooks.detected_api_keys", return_value=[])
    @patch("cross_harness.hooks.subprocess.run")
    def test_session_start_warns_when_claude_agent_role_uses_codex_harness(self, run, keys, verify, cleanup):
        run.return_value = subprocess.CompletedProcess([], 0, '{"loggedIn": true}', "")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            config = home / ".config/cross-harness/config.toml"
            contents = config.read_text(encoding="utf-8").replace(
                'harness = "claude"\nmodel = "haiku"\neffort = "low"',
                'harness = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"',
            )
            config.write_text(contents, encoding="utf-8")

            with patch.dict("os.environ", {}, clear=True), patch("sys.stdout", new_callable=StringIO) as stdout:
                self.assertEqual(0, claude_session_start(home))

            explorer = (home / ".claude/agents/cross-harness-explorer.md").read_text()
            self.assertIn("model: haiku", explorer)
            self.assertIn("harness is 'codex', not 'claude'", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
