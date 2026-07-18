from pathlib import Path
import tempfile
import unittest

from cross_harness.auth import detected_api_keys, sanitized_environment, verify_codex_config_ownership
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


if __name__ == "__main__":
    unittest.main()
