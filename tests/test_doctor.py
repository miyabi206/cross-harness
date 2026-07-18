from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from cross_harness.doctor import doctor


class DoctorTests(unittest.TestCase):
    def test_reports_claude_cli_and_subscription_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            home.mkdir()
            with (
                patch("cross_harness.doctor.detected_api_keys", return_value=[]),
                patch("cross_harness.doctor.resolve_codex", return_value=Path("/usr/local/bin/codex")),
                patch("cross_harness.doctor.verify_codex_chatgpt", return_value=(Path("/usr/local/bin/codex"), False)),
                patch("cross_harness.doctor.resolve_claude", return_value=Path("/usr/local/bin/claude")),
                patch("cross_harness.doctor.verify_claude_subscription", return_value=(Path("/usr/local/bin/claude"), False)) as verify_claude,
                patch("cross_harness.doctor.verify_codex_hook_receipt", return_value=(True, "ok")),
            ):
                report = doctor(home=home)

            checks = {check["name"]: check for check in report["checks"]}
            self.assertTrue(checks["independent Claude CLI"]["ok"])
            self.assertTrue(checks["Claude subscription auth"]["ok"])
            self.assertIn("fresh", checks["Claude subscription auth"]["detail"])
            self.assertTrue(verify_claude.call_args.kwargs["force"])


if __name__ == "__main__":
    unittest.main()
