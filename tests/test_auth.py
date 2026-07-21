from pathlib import Path
from unittest.mock import patch
import json
import subprocess
import tempfile
import unittest

from cross_harness.auth import (
    detected_api_keys,
    sanitized_environment,
    verify_claude_config_ownership,
    verify_claude_subscription,
    verify_codex_config_ownership,
)
from cross_harness.errors import AuthError


class AuthTests(unittest.TestCase):
    def test_all_api_key_variables_are_detected(self):
        env = {"PATH": "/bin", "OPENAI_API_KEY": "x", "VENDOR_API_KEY": "y", "EMPTY_API_KEY": ""}
        self.assertEqual(["OPENAI_API_KEY", "VENDOR_API_KEY"], detected_api_keys(env))

    def test_child_environment_is_allowlisted(self):
        env = sanitized_environment(Path("/tmp/home"), {"CROSS_HARNESS_RUN_DIR": "/tmp/run"})
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CROSS_HARNESS_ACTIVE", env)
        self.assertEqual("/tmp/home", env["HOME"])

    def test_child_environment_always_disables_bytecode_writes(self):
        with patch.dict("os.environ", {"PATH": "/bin", "PYTHONDONTWRITEBYTECODE": "0"}, clear=True):
            env = sanitized_environment(Path("/tmp/home"), {"PYTHONDONTWRITEBYTECODE": "0"})
        self.assertEqual("1", env["PYTHONDONTWRITEBYTECODE"])

    def test_project_provider_override_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            (repo / ".codex").mkdir(parents=True)
            home.mkdir()
            (repo / ".codex/config.toml").write_text('model_provider = "proxy"\n')
            with self.assertRaises(AuthError):
                verify_codex_config_ownership(home, repo, repo)

    def test_claude_config_rejects_api_billing_helper_without_exposing_command(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            (home / ".claude").mkdir(parents=True)
            repo.mkdir()
            (home / ".claude/settings.json").write_text(
                '{"apiKeyHelper":"secret-command ANTHROPIC_API_KEY"}\n'
            )
            with self.assertRaisesRegex(AuthError, "apiKeyHelper supplies ANTHROPIC_API_KEY") as error:
                verify_claude_config_ownership(home, repo)
            self.assertNotIn("secret-command", str(error.exception))

    def test_claude_config_rejects_router_and_provider_settings_in_all_scopes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            (repo / ".claude").mkdir(parents=True)
            for filename, setting in (
                ("settings.json", "ANTHROPIC_BASE_URL"),
                ("settings.local.json", "CLAUDE_CODE_USE_BEDROCK"),
            ):
                (repo / ".claude" / filename).write_text(json.dumps({"env": {setting: "redacted"}}))
                with self.assertRaises(AuthError):
                    verify_claude_config_ownership(home, repo)
                (repo / ".claude" / filename).unlink()

    @patch("cross_harness.auth.resolve_claude", return_value=Path("/usr/local/bin/claude"))
    @patch("cross_harness.auth.subprocess.run")
    def test_claude_subscription_requires_logged_in_status(self, run, resolve):
        run.return_value = subprocess.CompletedProcess([], 0, '{"loggedIn": true}', "")
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / "runtime"
            cli, cached = verify_claude_subscription(runtime, Path(folder) / "home", 24, environment={"PATH": "/bin"})
        self.assertEqual(Path("/usr/local/bin/claude"), cli)
        self.assertFalse(cached)
        self.assertEqual(["/usr/local/bin/claude", "auth", "status"], run.call_args.args[0])

    @patch("cross_harness.auth.resolve_claude")
    def test_claude_subscription_rejects_api_key_environment(self, resolve):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(AuthError, "API-key environment"):
                verify_claude_subscription(Path(folder) / "runtime", Path(folder) / "home", 24, environment={"ANTHROPIC_API_KEY": "x"})
        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
