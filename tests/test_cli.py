from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import unittest

from cross_harness.cli import main
from cross_harness.paths import source_root
from cross_harness.errors import SupervisorDiedError


class CliWaitTests(unittest.TestCase):
    def test_validate_reports_defaulted_settings_for_partial_personal_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            home.mkdir()
            config = root / "config.toml"
            config.write_text(
                '[roles.explorer]\neffort = "future-effort"\n', encoding="utf-8"
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(
                    0,
                    main(["--home", str(home), "validate", "--config", str(config)]),
                )

            self.assertEqual("configuration valid\n", stdout.getvalue())
            self.assertIn("warning: roles.explorer.effort", stderr.getvalue())
            self.assertIn("default: roles.tester.model", stderr.getvalue())
            self.assertNotIn("default: roles.explorer.effort", stderr.getvalue())

    def test_validate_reports_unknown_effort_as_warning_without_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            contents = (source_root() / "config/default.toml").read_text(encoding="utf-8")
            config.write_text(contents.replace('effort = "low"', 'effort = "future-effort"'), encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(0, main(["validate", "--config", str(config)]))
            self.assertEqual("configuration valid\n", stdout.getvalue())
            self.assertIn("warning: roles.explorer.effort", stderr.getvalue())

    def test_wait_exit_codes_and_summary_output(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "run"
            run.mkdir()
            (run / "summary.json").write_text(json.dumps({"status": "success"}))
            (run / "summary.txt").write_text("success summary\n")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, main(["wait", "--run", str(run), "--timeout-seconds", "0"]))
            self.assertEqual("success summary\n", output.getvalue())

            (run / "summary.json").write_text(json.dumps({"status": "blocked"}))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(3, main(["wait", "--run", str(run), "--timeout-seconds", "0"]))
            self.assertEqual("success summary\n", output.getvalue())

            (run / "summary.json").unlink()
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(4, main(["wait", "--run", str(run), "--timeout-seconds", "0"]))
            self.assertEqual("", output.getvalue())

    def test_wait_returns_distinct_exit_code_when_supervisor_died(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "run"
            run.mkdir()
            error = StringIO()
            with (
                redirect_stderr(error),
                patch("cross_harness.cli.wait_for_run", side_effect=SupervisorDiedError("supervisor died")),
            ):
                self.assertEqual(5, main(["wait", "--run", str(run), "--timeout-seconds", "1"]))
            self.assertIn("supervisor died", error.getvalue())


if __name__ == "__main__":
    unittest.main()
