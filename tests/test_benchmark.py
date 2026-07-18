from pathlib import Path
import json
import tempfile
import unittest

from cross_harness.errors import HarnessError
from cross_harness.benchmark import load_records, load_task_plan, render, verify_task_commits
from cross_harness.paths import source_root


class BenchmarkTests(unittest.TestCase):
    def test_template_has_all_ten_pairs_and_renders(self):
        records = load_records(source_root() / "benchmarks/records.template.json", allow_template=True)
        self.assertEqual(10, len(records))
        report = render(records)
        self.assertIn("Mean terminal compression", report)
        self.assertIn("Claude usage", report)
        self.assertIn("Human corrections", report)
        self.assertIn("small_fix", report)

    def test_placeholder_and_missing_metric_are_rejected_for_measurement(self):
        template = json.loads((source_root() / "benchmarks/records.template.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.json"
            path.write_text(json.dumps(template))
            with self.assertRaises(HarnessError):
                load_records(path)

            for record in template:
                record["source"] = f"renai issue for {record['task_type']}"
            template[0].pop("files_read")
            path.write_text(json.dumps(template))
            with self.assertRaises(HarnessError):
                load_records(path)

    def test_complete_paired_measurements_are_accepted(self):
        records = json.loads((source_root() / "benchmarks/records.template.json").read_text())
        for record in records:
            record["source"] = f"renai#{record['task_type']}@same-commit"
            record["message_bytes"] = 100
            record["raw_terminal_bytes"] = 200
            record["summary_bytes"] = 20
            record["duration_seconds"] = 1.5
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.json"
            path.write_text(json.dumps(records))
            loaded = load_records(path)
            self.assertEqual(10, len(loaded))
            self.assertIn("Cross/baseline total duration ratio", render(loaded))

            records[0]["source"] = "different commit"
            path.write_text(json.dumps(records))
            with self.assertRaises(HarnessError):
                load_records(path)

    def test_task_plan_requires_five_real_issue_commit_pairs(self):
        template_path = source_root() / "benchmarks/tasks.template.json"
        self.assertEqual(5, len(load_task_plan(template_path, allow_template=True)))
        with self.assertRaises(HarnessError):
            load_task_plan(template_path)

    def test_task_plan_verifies_full_commits_in_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / "repo"
            repo.mkdir()
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
            tasks = json.loads((source_root() / "benchmarks/tasks.template.json").read_text())
            for task in tasks:
                task["issue"] = f"https://example.invalid/renai/issues/{task['task_type']}"
                task["start_commit"] = commit
                task["goal"] = f"reproduce {task['task_type']}"
                task["done_when"] = ["required behavior is reproduced"]
                task["checks"] = ["fixture check"]
            path = Path(folder) / "tasks.json"
            path.write_text(json.dumps(tasks))
            loaded = load_task_plan(path)
            self.assertEqual(5, len(verify_task_commits(loaded, repo)))

if __name__ == "__main__":
    unittest.main()
