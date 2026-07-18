from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from cross_harness.maintenance import cleanup


class MaintenanceTests(unittest.TestCase):
    def test_old_runs_removed_and_recent_incomplete_run_marked_orphan(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            runs = home / ".local/state/cross-harness/runs"
            old = runs / "old"
            orphan = runs / "orphan"
            old.mkdir(parents=True)
            orphan.mkdir()
            now = datetime.now(timezone.utc)
            old_time = (now - timedelta(days=8)).timestamp()
            orphan_time = (now - timedelta(hours=2)).timestamp()
            os.utime(old, (old_time, old_time))
            os.utime(orphan, (orphan_time, orphan_time))
            result = cleanup(home=home, now=now)
            self.assertFalse(old.exists())
            self.assertTrue((orphan / "ORPHANED").exists())
            self.assertEqual(1, len(result["removed"]))

    def test_old_isolated_worktree_is_unregistered_before_run_removal(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            repo = Path(folder) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            run = home / ".local/state/cross-harness/runs/isolated"
            worktree = run / "worktree"
            run.mkdir(parents=True)
            subprocess.run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], cwd=repo, check=True, capture_output=True)
            (run / "ISOLATED_WORKTREE").write_text(str(worktree) + "\n")
            old_time = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
            os.utime(run, (old_time, old_time))

            cleanup(home=home)
            listing = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True).stdout
            self.assertNotIn(str(worktree), listing)
            self.assertFalse(run.exists())

    def test_live_supervisor_is_not_marked_orphaned(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            run = home / ".local/state/cross-harness/runs/live"
            run.mkdir(parents=True)
            (run / "supervisor.pid").write_text(f"{os.getpid()}\n")
            now = datetime.now(timezone.utc)
            old_time = (now - timedelta(hours=2)).timestamp()
            os.utime(run, (old_time, old_time))
            result = cleanup(home=home, now=now)
            self.assertFalse((run / "ORPHANED").exists())
            self.assertEqual([], result["orphaned"])


if __name__ == "__main__":
    unittest.main()
