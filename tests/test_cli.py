from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import unittest

from cross_harness.cli import main
from cross_harness.errors import SupervisorDiedError


class CliWaitTests(unittest.TestCase):
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
