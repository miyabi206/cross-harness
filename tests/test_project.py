from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import tempfile
import unittest

from cross_harness.cli import main
from cross_harness.errors import HarnessError
from cross_harness.paths import source_root
from cross_harness.project import TASK_LABEL, remove, setup


class ProjectTaskTests(unittest.TestCase):
    def test_setup_merges_existing_tasks_and_backs_up_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            project = root / "project"
            tasks_path = project / ".vscode/tasks.json"
            tasks_path.parent.mkdir(parents=True)
            original = {
                "version": "2.0.0",
                "custom": {"preserved": True},
                "tasks": [{"label": "lint", "type": "shell", "command": "make lint"}],
            }
            tasks_path.write_text(json.dumps(original), encoding="utf-8")

            self.assertEqual([f"updated {tasks_path.resolve()}"], setup(project, home=home))

            updated = json.loads(tasks_path.read_text(encoding="utf-8"))
            self.assertEqual("2.0.0", updated["version"])
            self.assertEqual({"preserved": True}, updated["custom"])
            self.assertEqual(original["tasks"][0], updated["tasks"][0])
            task = updated["tasks"][1]
            self.assertEqual(TASK_LABEL, task["label"])
            self.assertEqual(str(home.resolve() / ".local/bin/cross-harness"), task["command"])
            self.assertEqual([], task["problemMatcher"])
            self.assertEqual("always", task["presentation"]["reveal"])
            self.assertFalse(task["presentation"]["focus"])
            backups = list((home / ".local/state/cross-harness/project-backups").glob("*-tasks.json"))
            self.assertEqual(1, len(backups))
            self.assertEqual(json.dumps(original), backups[0].read_text(encoding="utf-8"))

            self.assertEqual([f"removed {tasks_path.resolve()}"], remove(project, home=home))
            self.assertEqual(original, json.loads(tasks_path.read_text(encoding="utf-8")))

    def test_setup_created_file_is_removed_when_its_task_is_last_one(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            tasks_path = project / ".vscode/tasks.json"

            setup(project, home=home)
            self.assertTrue(tasks_path.exists())
            self.assertEqual(
                [f"removed {tasks_path.resolve()}", f"removed {tasks_path.parent.resolve()}"],
                remove(project, home=home),
            )
            self.assertFalse(tasks_path.exists())
            self.assertFalse(tasks_path.parent.exists())

    def test_dry_run_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            tasks_path = project / ".vscode/tasks.json"
            output = StringIO()

            with redirect_stdout(output):
                self.assertEqual(0, main(["--home", str(home), "project", "setup", "--cwd", str(project), "--dry-run"]))
            self.assertEqual(f"would create {tasks_path.resolve()}\n", output.getvalue())
            self.assertFalse(tasks_path.exists())
            self.assertFalse((home / ".local/state/cross-harness").exists())

    def test_invalid_existing_json_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "project"
            tasks_path = project / ".vscode/tasks.json"
            tasks_path.parent.mkdir(parents=True)
            invalid = "{ invalid"
            tasks_path.write_text(invalid, encoding="utf-8")

            with self.assertRaises(HarnessError) as raised:
                setup(project, home=root / "home")
            self.assertIn(str(tasks_path), str(raised.exception))
            self.assertEqual(invalid, tasks_path.read_text(encoding="utf-8"))

    def test_commented_tasks_json_explains_manual_setup_and_remove_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            project = root / "project"
            tasks_path = project / ".vscode/tasks.json"
            tasks_path.parent.mkdir(parents=True)
            commented = "{\n  // See https://go.microsoft.com/fwlink/?LinkId=733558\n  \"version\": \"2.0.0\",\n  \"tasks\": []\n}\n"
            tasks_path.write_text(commented, encoding="utf-8")
            template = json.loads((source_root() / "assets/shared/vscode-tasks.json").read_text(encoding="utf-8"))
            task = template["tasks"][0]
            task["command"] = str(home.resolve() / ".local/bin/cross-harness")
            rendered_task = json.dumps(task, ensure_ascii=False, indent=2)

            for command, manual_action in (("setup", 'Add this task manually to the "tasks" array:'), ("remove", "Remove the task with label")):
                with self.subTest(command=command):
                    stderr = StringIO()
                    with redirect_stderr(stderr):
                        self.assertEqual(2, main(["--home", str(home), "project", command, "--cwd", str(project)]))
                    message = stderr.getvalue()
                    self.assertIn(f"cross-harness: invalid JSON in {tasks_path.resolve()}", message)
                    self.assertIn("line 2 column 3 (char 4)", message)
                    self.assertIn("VS Code tasks.json supports comments (JSONC)", message)
                    self.assertIn("cannot rewrite a commented file without losing comments", message)
                    self.assertIn(manual_action, message)
                    self.assertIn(rendered_task, message)
                    self.assertEqual(commented, tasks_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
