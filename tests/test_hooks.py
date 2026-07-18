from io import StringIO
from unittest.mock import patch
from pathlib import Path
import json
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
            home = Path(folder) / "home"
            home.mkdir()
            install(home, source_root())
            config = home / ".config/cross-harness/config.toml"
            contents = config.read_text(encoding="utf-8").replace('model = "haiku"', 'model = "next-explorer"')
            contents = contents.replace('effort = "low"', 'effort = "next-effort"')
            config.write_text(contents, encoding="utf-8")

            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(0, claude_session_start(home))

            explorer = (home / ".claude/agents/cross-harness-explorer.md").read_text()
            self.assertIn("model: next-explorer", explorer)
            self.assertIn("effort: next-effort", explorer)


if __name__ == "__main__":
    unittest.main()
