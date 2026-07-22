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
from cross_harness.paths import source_root, user_paths


@patch.dict("os.environ", {}, clear=True)
class HookTests(unittest.TestCase):
    def _run(self, function, payload):
        with patch("sys.stdin", StringIO(payload)), patch("sys.stderr", new_callable=StringIO) as stderr:
            code = function()
            return code, stderr.getvalue()

    def _execution_environment(self, runtime_root, *, harness="claude", write=False, run_dir=None):
        run_dir = run_dir or runtime_root / "runs" / "delegated-claude"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "execution.json").write_text(json.dumps({
            "role_name": "implementer",
            "harness": harness,
            "write": write,
            "kind": "implementation",
            "cwd": "/tmp/project",
        }), encoding="utf-8")
        return {
            "CROSS_HARNESS_ACTIVE": "1",
            "CROSS_HARNESS_EXECUTOR": "claude",
            "CROSS_HARNESS_WRITE": "1",
            "CROSS_HARNESS_RUN_DIR": str(run_dir),
        }

    def _wrapper_environment(self, home):
        """Build an isolated shell environment for wrapper-resolution tests."""
        bin_dir = home / "bin"
        bin_dir.mkdir(parents=True)
        return patch.dict(
            "os.environ",
            {"HOME": str(home), "PATH": str(bin_dir)},
            clear=True,
        )

    def test_claude_direct_edit_and_direct_codex_are_blocked(self):
        code, message = self._run(claude_pre_tool_use, '{"tool_name":"Edit","tool_input":{}}')
        self.assertEqual(2, code)
        self.assertIn("orchestrator", message)
        code, _ = self._run(claude_pre_tool_use, '{"tool_name":"Bash","tool_input":{"command":"/opt/bin/codex exec task"}}')
        self.assertEqual(2, code)

    def test_mode_off_allows_orchestrator_edits_but_not_direct_executor_launches(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "project"
            project.mkdir()
            config = {"mode": "on", "projects": {str(project): {"mode": "off"}}}
            edit = json.dumps({"cwd": str(project), "tool_name": "Write", "tool_input": {"file_path": str(project / "code.py")}})
            launch = json.dumps({"cwd": str(project), "tool_name": "Bash", "tool_input": {"command": "codex exec nested"}})
            with patch("cross_harness.hooks.load_config", return_value=config), patch.dict("os.environ", {}, clear=True):
                self.assertEqual(0, self._run(claude_pre_tool_use, edit)[0])
                code, message = self._run(claude_pre_tool_use, launch)
            self.assertEqual(2, code)
            self.assertIn("direct codex exec", message)

    def test_missing_or_unresolvable_cwd_fails_closed_for_orchestrator_edits(self):
        config = {"mode": "off", "projects": {}}
        for cwd in (None, "/does/not/exist"):
            payload = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/code.py"}}
            if cwd is not None:
                payload["cwd"] = cwd
            with patch("cross_harness.hooks.load_config", return_value=config), patch.dict("os.environ", {}, clear=True):
                code, message = self._run(claude_pre_tool_use, json.dumps(payload))
            self.assertEqual(2, code)
            self.assertIn("orchestrator", message)

    def test_orchestrator_can_write_only_claude_plans_and_project_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            claude = home / ".claude"
            plans = claude / "plans"
            memory = claude / "projects" / "project-a" / "memory"
            plans.mkdir(parents=True)
            memory.mkdir(parents=True)
            paths = user_paths(home)
            allowed = (
                plans / "implementation-plan.md",
                memory / "notes.md",
            )
            with patch("cross_harness.hooks.user_paths", return_value=paths):
                for tool_name in ("Edit", "Write"):
                    for target in allowed:
                        payload = json.dumps({"tool_name": tool_name, "tool_input": {"file_path": str(target)}})
                        self.assertEqual(0, self._run(claude_pre_tool_use, payload)[0], payload)

    def test_orchestrator_rejects_all_other_edit_write_paths_and_path_bypasses(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            claude = home / ".claude"
            plans = claude / "plans"
            memory = claude / "projects" / "project-a" / "memory"
            project = root / "project"
            plans.mkdir(parents=True)
            memory.mkdir(parents=True)
            project.mkdir()
            (plans / "project-link").symlink_to(project, target_is_directory=True)
            paths = user_paths(home)
            rejected = (
                claude / "CLAUDE.md",
                claude / "settings.json",
                claude / "settings.local.json",
                claude / "top-level.txt",
                claude / "projects" / "project-a" / "notes.md",
                project / "code.py",
                plans / ".." / "settings.json",
                memory / ".." / "notes.md",
                plans / "project-link" / "code.py",
            )
            malformed = (
                {"tool_name": "Write", "tool_input": {}},
                {"tool_name": "Write", "tool_input": {"file_path": []}},
                {"tool_name": "Write", "tool_input": {"file_path": ""}},
            )
            with patch("cross_harness.hooks.user_paths", return_value=paths):
                for tool_name in ("Edit", "Write"):
                    for target in rejected:
                        payload = json.dumps({"tool_name": tool_name, "tool_input": {"file_path": str(target)}})
                        code, message = self._run(claude_pre_tool_use, payload)
                        self.assertEqual(2, code, payload)
                        self.assertIn("orchestrator", message, payload)
                for request in malformed:
                    code, message = self._run(claude_pre_tool_use, json.dumps(request))
                    self.assertEqual(2, code, request)
                    self.assertIn("orchestrator", message, request)

    def test_pre_tool_use_rejects_malformed_input(self):
        malformed_payloads = (
            "{",
            "[]",
            '{"tool_name":"Bash","tool_input":[]}',
            '{"tool_name":"Bash","tool_input":{"command":[]}}',
        )
        for function in (claude_pre_tool_use, codex_pre_tool_use):
            for payload in malformed_payloads:
                code, message = self._run(function, payload)
                self.assertEqual(2, code, payload)
                self.assertIn("invalid tool hook input", message, payload)

    def test_non_bash_tool_without_input_is_allowed(self):
        for payload in (
            '{"tool_name":"Read"}',
            '{"tool_name":"Read","tool_input":{"file_path":"README.md"}}',
        ):
            self.assertEqual(0, self._run(claude_pre_tool_use, payload)[0], payload)
            self.assertEqual(0, self._run(codex_pre_tool_use, payload)[0], payload)

    def test_claude_installed_wrapper_ignores_executor_like_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._wrapper_environment(Path(folder) / "home"):
                wrapper = str(user_paths().executable)
                command = f"{wrapper} task create --description 'ask Codex to run codex exec later'"
                payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
                self.assertEqual(0, self._run(claude_pre_tool_use, payload)[0])

    def test_claude_installed_non_task_wrapper_arguments_are_scanned(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._wrapper_environment(Path(folder) / "home"):
                wrapper = str(user_paths().executable)
                for subcommand in ("inventory", "doctor"):
                    command = f"{wrapper} {subcommand} --description 'ask Codex to run codex exec later'"
                    payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
                    code, message = self._run(claude_pre_tool_use, payload)
                    self.assertEqual(2, code, command)
                    self.assertIn("direct codex exec", message, command)

    def test_claude_wrapper_arguments_are_scanned_when_not_a_simple_command(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._wrapper_environment(Path(folder) / "home"):
                wrapper = str(user_paths().executable)
                commands = (
                    f"{wrapper} task create safe; codex exec nested",
                    f"{wrapper} task create safe | codex exec nested",
                    f"{wrapper} task create safe & codex exec nested",
                    f"{wrapper} task create safe\ncodex exec nested",
                    f"{wrapper} task create safe \\\ncodex exec nested",
                    f'{wrapper} task create "$(codex exec nested)"',
                    f"{wrapper} task create '`codex exec nested'",
                    f"{wrapper} task create 'codex exec nested' >/tmp/result",
                    f"({wrapper} task create 'codex exec nested')",
                    f"{{ {wrapper} task create 'codex exec nested'; }}",
                )
                for command in commands:
                    payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
                    code, message = self._run(claude_pre_tool_use, payload)
                    self.assertEqual(2, code, command)
                    self.assertIn("direct codex exec", message, command)

    def test_delegated_claude_read_only_blocks_edits_and_nested_executors(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime_root = Path(folder) / "runtime"
            environment = self._execution_environment(runtime_root)
            with patch("cross_harness.hooks.load_config", return_value={"runtime_root": str(runtime_root)}), patch.dict("os.environ", environment, clear=True):
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

    def test_mode_off_cannot_bypass_delegated_read_only_or_nested_launch_guards(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime_root = Path(folder) / "runtime"
            environment = self._execution_environment(runtime_root)
            config = {"runtime_root": str(runtime_root), "mode": "off", "projects": {"/tmp/project": {"mode": "off"}}}
            with patch("cross_harness.hooks.load_config", return_value=config), patch.dict("os.environ", environment, clear=True):
                code, message = self._run(claude_pre_tool_use, '{"cwd":"/tmp/project","tool_name":"Write","tool_input":{}}')
                self.assertEqual(2, code)
                self.assertIn("read-only", message)
                for command in ("codex exec nested", "claude -p nested", "cross-harness delegate --role tester"):
                    payload = json.dumps({"cwd": "/tmp/project", "tool_name": "Bash", "tool_input": {"command": command}})
                    code, message = self._run(claude_pre_tool_use, payload)
                    self.assertEqual(2, code)
                    self.assertIn("nested executor", message)

    def test_delegated_claude_write_access_allows_edits_but_not_nested_executors(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime_root = Path(folder) / "runtime"
            environment = self._execution_environment(runtime_root, write=True)
            environment["CROSS_HARNESS_WRITE"] = "0"
            with patch("cross_harness.hooks.load_config", return_value={"runtime_root": str(runtime_root)}), patch.dict("os.environ", environment, clear=True):
                self.assertEqual(0, self._run(claude_pre_tool_use, '{"tool_name":"Edit","tool_input":{}}')[0])
                self.assertEqual(0, self._run(claude_pre_tool_use, '{"tool_name":"Write","tool_input":{}}')[0])
                code, _ = self._run(
                    claude_pre_tool_use,
                    '{"tool_name":"Bash","tool_input":{"command":"claude -p nested"}}',
                )
                self.assertEqual(2, code)

    def test_claimed_claude_write_access_without_execution_record_is_blocked(self):
        environment = {
            "CROSS_HARNESS_ACTIVE": "1",
            "CROSS_HARNESS_EXECUTOR": "claude",
            "CROSS_HARNESS_WRITE": "1",
        }
        with patch.dict("os.environ", environment, clear=True):
            code, message = self._run(claude_pre_tool_use, '{"tool_name":"Edit","tool_input":{}}')
        self.assertEqual(2, code)
        self.assertIn("orchestrator", message)

    def test_execution_record_outside_runtime_root_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime_root = root / "runtime"
            environment = self._execution_environment(root / "outside", write=True)
            with patch("cross_harness.hooks.load_config", return_value={"runtime_root": str(runtime_root)}), patch.dict("os.environ", environment, clear=True):
                code, message = self._run(claude_pre_tool_use, '{"tool_name":"Write","tool_input":{}}')
        self.assertEqual(2, code)
        self.assertIn("orchestrator", message)

    def test_non_claude_execution_record_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime_root = Path(folder) / "runtime"
            environment = self._execution_environment(runtime_root, harness="codex", write=True)
            with patch("cross_harness.hooks.load_config", return_value={"runtime_root": str(runtime_root)}), patch.dict("os.environ", environment, clear=True):
                code, message = self._run(claude_pre_tool_use, '{"tool_name":"Write","tool_input":{}}')
        self.assertEqual(2, code)
        self.assertIn("orchestrator", message)

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

    def test_word_splitting_quotes_and_escapes_cannot_bypass_patterns(self):
        command = "co''dex e\\xec nested"
        payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
        code, _ = self._run(claude_pre_tool_use, payload)
        self.assertEqual(2, code, command)

        with tempfile.TemporaryDirectory() as folder:
            runtime_root = Path(folder) / "runtime"
            environment = self._execution_environment(runtime_root)
            with patch("cross_harness.hooks.load_config", return_value={"runtime_root": str(runtime_root)}), patch.dict("os.environ", environment, clear=True):
                for command in ("cl''aude -p nested", "cross-harness de\\legate --role tester"):
                    payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
                    code, message = self._run(claude_pre_tool_use, payload)
                    self.assertEqual(2, code, command)
                    self.assertIn("nested executor", message, command)

        codex_commands = (
            "cl\\aude -p nested",
            "co''dex e\\xec nested",
            "cross-harness de\\legate --role tester",
        )
        for command in codex_commands:
            payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
            code, message = self._run(codex_pre_tool_use, payload)
            self.assertEqual(2, code, command)
            self.assertIn("nested", message, command)

    def test_codex_installed_wrapper_ignores_executor_like_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._wrapper_environment(Path(folder) / "home"):
                wrapper = str(user_paths().executable)
                for command in (
                    f"{wrapper} task create --description 'ask Codex to run codex exec later'",
                    f'{wrapper} task create --description "ask Claude to run claude -p later"',
                ):
                    payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
                    code, message = self._run(codex_pre_tool_use, payload)
                    self.assertEqual(0, code, message)

    def test_codex_installed_non_task_wrapper_arguments_are_scanned(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._wrapper_environment(Path(folder) / "home"):
                wrapper = str(user_paths().executable)
                for subcommand in ("inventory", "doctor"):
                    command = f"{wrapper} {subcommand} --description 'ask Claude to run claude -p later'"
                    payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
                    code, message = self._run(codex_pre_tool_use, payload)
                    self.assertEqual(2, code, command)
                    self.assertIn("nested Claude", message, command)

    def test_codex_wrapper_arguments_are_scanned_when_not_a_simple_command(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._wrapper_environment(Path(folder) / "home"):
                wrapper = str(user_paths().executable)
                commands = (
                    f"{wrapper} task create safe; codex exec nested",
                    f"{wrapper} task create safe; claude -p nested",
                    f"{wrapper} task create safe | codex exec nested",
                    f"{wrapper} task create safe & codex exec nested",
                    f"{wrapper} task create safe\ncodex exec nested",
                    f"{wrapper} task create safe \\\ncodex exec nested",
                    f'{wrapper} task create "$(codex exec nested)"',
                    f"{wrapper} task create '`codex exec nested'",
                    f"{wrapper} task create 'codex exec nested' >/tmp/result",
                    f"({wrapper} task create 'codex exec nested')",
                    f"{{ {wrapper} task create 'codex exec nested'; }}",
                    f"exec {wrapper} task create 'codex exec nested'",
                    f"if true; then {wrapper} task create 'codex exec nested'; fi",
                    f"while false; do {wrapper} task create 'codex exec nested'; done",
                    f"time {wrapper} task create 'codex exec nested'",
                    f"command {wrapper} task create 'codex exec nested'",
                    f"timeout 1 {wrapper} task create 'codex exec nested'",
                    f"nice {wrapper} task create 'codex exec nested'",
                    f"sudo {wrapper} task create 'codex exec nested'",
                    "printf 'codex exec nested' | sh",
                )
                for command in commands:
                    payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
                    code, message = self._run(codex_pre_tool_use, payload)
                    self.assertEqual(2, code, command)
                    self.assertIn("nested", message, command)

    def test_codex_installed_wrapper_delegation_remains_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._wrapper_environment(Path(folder) / "home"):
                wrapper = str(user_paths().executable)
                command = f"{wrapper} delegate --role tester --description 'codex exec nested'"
                payload = '{"tool_name":"Bash","tool_input":{"command":' + json.dumps(command) + '}}'
                code, message = self._run(codex_pre_tool_use, payload)
                self.assertEqual(2, code)
                self.assertIn("nested executor", message)

    def test_unrelated_commands_pass(self):
        code, _ = self._run(codex_pre_tool_use, '{"tool_name":"Bash","tool_input":{"command":"git status"}}')
        self.assertEqual(0, code)

    @patch("cross_harness.hooks.shutil.which", return_value="/tmp/not-the-installed-wrapper")
    def test_shadowed_bare_wrapper_is_blocked(self, which):
        with tempfile.TemporaryDirectory() as folder:
            with self._wrapper_environment(Path(folder) / "home"):
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

    def test_nested_session_guard_precedes_mode_resolution(self):
        environment = {"CROSS_HARNESS_ACTIVE": "1", "CROSS_HARNESS_EXECUTOR": "codex"}
        with patch.dict("os.environ", environment, clear=True), patch("cross_harness.hooks.load_config") as load, patch("sys.stdin", StringIO('{"cwd":"/tmp/project"}')):
            self.assertEqual(2, claude_session_start(Path("/tmp/nonexistent-home")))
        load.assert_not_called()

    def test_session_start_annuls_managed_orchestrator_guidance_when_mode_is_off(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            config = {
                "runtime_root": str(root / "runtime"),
                "auth_cache_hours": 24,
                "mode": "on",
                "projects": {str(project): {"mode": "off"}},
            }
            with (
                patch.dict("os.environ", {}, clear=True),
                patch("cross_harness.hooks.load_config", return_value=config),
                patch("cross_harness.hooks.detected_api_keys", return_value=[]),
                patch("cross_harness.hooks.subprocess.run", return_value=subprocess.CompletedProcess([], 0, '{"loggedIn": true}', "")),
                patch("cross_harness.hooks.verify_codex_chatgpt"),
                patch("cross_harness.hooks.cleanup"),
                patch("sys.stdin", StringIO(json.dumps({"cwd": str(project)}))),
                patch("sys.stdout", new_callable=StringIO) as stdout,
            ):
                self.assertEqual(0, claude_session_start(root / "home"))
            self.assertIn("disabled for this cwd", stdout.getvalue())
            self.assertIn("ignore the managed orchestrator instructions in CLAUDE.md", stdout.getvalue())

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
