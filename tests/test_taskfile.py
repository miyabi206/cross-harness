from pathlib import Path
import tempfile
import unittest

from cross_harness.errors import HarnessError
from cross_harness.taskfile import create_task_file


class TaskFileTests(unittest.TestCase):
    def test_wrapper_creates_bounded_task_in_runtime_inbox(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            repo = Path(folder) / "repo"
            home.mkdir()
            repo.mkdir()
            path = create_task_file(
                "implementer", "implementation", repo, "Add a greeting",
                ["Greeting is visible", "Focused tests pass"],
                scope=["README.md"], checks=["unit test"], home=home,
            )
            self.assertEqual(home.resolve() / ".local/state/cross-harness/inbox", path.parent)
            text = path.read_text()
            self.assertIn("# Goal\nAdd a greeting", text)
            self.assertIn("- Focused tests pass", text)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_task_creation_rejects_missing_done_condition_and_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            repo = Path(folder) / "repo"
            home.mkdir()
            repo.mkdir()
            with self.assertRaises(HarnessError):
                create_task_file("tester", "test", repo, "Run tests", [], home=home)
            with self.assertRaises(HarnessError):
                create_task_file(
                    "tester", "test", repo,
                    "Use OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456",
                    ["done"], home=home,
                )


if __name__ == "__main__":
    unittest.main()
