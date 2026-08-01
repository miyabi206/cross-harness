from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cross_harness.cli import main
from cross_harness.config import load_config
from cross_harness.installer import install, uninstall
from cross_harness.paths import source_root
from cross_harness import runner
from cross_harness.selfupdate import detect_drift, self_update


class SelfUpdateTests(unittest.TestCase):
    def _git_repo(self, root: Path) -> Path:
        repo = root / "repo"
        shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "-qm", "initial"],
            cwd=repo,
            check=True,
        )
        return repo

    def test_detect_drift_ignores_python_cache_and_bytecode(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "repo/src"
            installed = root / "installed/src"
            (source / "__pycache__").mkdir(parents=True)
            (installed / "__pycache__").mkdir(parents=True)
            source.mkdir(exist_ok=True)
            installed.mkdir(exist_ok=True)
            (source / "module.py").write_text("same\n")
            (installed / "module.py").write_text("same\n")
            (source / "__pycache__/module.cpython-312.pyc").write_bytes(b"source")
            (installed / "__pycache__/module.cpython-312.pyc").write_bytes(b"installed")
            (source / "ignored.pyc").write_bytes(b"source")
            (installed / "ignored.pyc").write_bytes(b"installed")

            self.assertEqual([], detect_drift(root / "repo", root / "installed"))
            (source / "module.py").write_text("changed\n")
            self.assertEqual(["src/module.py"], detect_drift(root / "repo", root / "installed"))

    def test_check_reports_drift_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = self._git_repo(root)
            install(home, repo)
            manifest = home / ".local/state/cross-harness/install-manifest.json"
            before = manifest.read_text(encoding="utf-8")
            source = repo / "src/cross_harness/cli.py"
            source.write_text(source.read_text(encoding="utf-8") + "\n# drift\n")
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(1, main(["--home", str(home), "self-update", "--check", "--repo", str(repo)]))
            self.assertIn("drift detected", stdout.getvalue())
            self.assertEqual(before, manifest.read_text(encoding="utf-8"))

    def test_self_update_refreshes_runtime_and_records_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = self._git_repo(root)
            install(home, repo)
            source = repo / "src/cross_harness/cli.py"
            source.write_text(source.read_text(encoding="utf-8") + "\n# drift\n")
            result = self_update(home, repo)
            self.assertEqual("updated", result.state)
            self.assertIn("# drift", (home / ".local/share/cross-harness/current/src/cross_harness/cli.py").read_text())
            manifest = json.loads((home / ".local/state/cross-harness/install-manifest.json").read_text())
            self.assertTrue(manifest["repo_commit"])

    def test_active_root_lock_skips_update_and_install_failure_is_warning_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = self._git_repo(root)
            install(home, repo)
            source = repo / "src/cross_harness/cli.py"
            source.write_text(source.read_text(encoding="utf-8") + "\n# drift\n")
            runtime_root = Path(load_config(home=home)["runtime_root"])
            self.assertTrue(runner._acquire_root_lock(runtime_root, repo))
            try:
                with patch("cross_harness.selfupdate.install") as installer:
                    result = self_update(home, repo)
            finally:
                runner._release_root_lock(runtime_root, repo)
            self.assertEqual("skipped", result.state)
            self.assertTrue(any("in progress" in warning for warning in result.warnings))
            installer.assert_not_called()

            with patch("cross_harness.selfupdate.install", side_effect=OSError("unavailable")):
                result = self_update(home, repo)
            self.assertEqual("failed", result.state)
            self.assertTrue(any("install failed" in warning for warning in result.warnings))

    def test_check_reports_drift_without_delegation_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = self._git_repo(root)
            install(home, repo)
            source = repo / "src/cross_harness/cli.py"
            source.write_text(source.read_text(encoding="utf-8") + "\n# drift\n")
            with patch("cross_harness.selfupdate.runner._try_lock") as try_lock:
                result = self_update(home, repo, check=True)
            self.assertEqual("drift", result.state)
            self.assertIn("src/cross_harness/cli.py", result.drift)
            try_lock.assert_not_called()

    def test_hook_update_skips_non_default_and_detached_heads_without_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = self._git_repo(root)
            install(home, repo)
            source = repo / "src/cross_harness/cli.py"
            source.write_text(source.read_text(encoding="utf-8") + "\n# drift\n")

            for checkout in (
                ["checkout", "-qb", "feature"],
                ["checkout", "--detach", "-q", "HEAD"],
            ):
                subprocess.run(["git", *checkout], cwd=repo, check=True, capture_output=True)
                stdout = StringIO()
                stderr = StringIO()
                with patch("cross_harness.selfupdate.install") as installer, redirect_stdout(stdout):
                    with patch("sys.stderr", stderr):
                        self.assertEqual(
                            0,
                            main([
                                "--home", str(home), "self-update", "--from-hook", "--repo", str(repo)
                            ]),
                        )
                self.assertEqual("", stdout.getvalue())
                self.assertEqual("", stderr.getvalue())
                installer.assert_not_called()

            result = self_update(home, repo)
            self.assertEqual("updated", result.state)

    def test_unknown_delegation_status_skips_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = self._git_repo(root)
            install(home, repo)
            source = repo / "src/cross_harness/cli.py"
            source.write_text(source.read_text(encoding="utf-8") + "\n# drift\n")
            with patch("cross_harness.selfupdate.load_config", side_effect=OSError("unavailable")), patch(
                "cross_harness.selfupdate.install"
            ) as installer:
                result = self_update(home, repo)
            self.assertEqual("skipped", result.state)
            self.assertTrue(any("status unavailable" in warning for warning in result.warnings))
            installer.assert_not_called()

    def test_legacy_manifest_without_repo_commit_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = self._git_repo(root)
            install(home, repo)
            manifest_path = home / ".local/state/cross-harness/install-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest.pop("repo_commit")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self_update(home, repo, check=True)
            self.assertEqual("up-to-date", result.state)
            self.assertIsNone(result.recorded_commit)

    def test_git_hooks_are_installed_backed_up_and_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = self._git_repo(root)
            existing = repo / ".git/hooks/post-commit"
            existing.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            existing.chmod(0o755)
            install(home, repo)
            manifest = json.loads((home / ".local/state/cross-harness/install-manifest.json").read_text())
            record = next(item for item in manifest["records"] if item["path"] == str(existing))
            self.assertEqual("git_hook", record["management"])
            self.assertTrue(Path(record["backup"]).is_file())
            self.assertNotIn("exit 7", existing.read_text())
            self.assertTrue(os.access(existing, os.X_OK))
            uninstall(home)
            self.assertEqual("#!/bin/sh\nexit 7\n", existing.read_text())

    def test_modified_git_hook_is_repaired_by_self_update_and_uninstall_restores_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = self._git_repo(root)
            existing = repo / ".git/hooks/post-commit"
            original = "#!/bin/sh\nexit 9\n"
            existing.write_text(original, encoding="utf-8")
            existing.chmod(0o755)
            install(home, repo)

            existing.write_text("#!/bin/sh\n# externally modified\n", encoding="utf-8")
            install(home, repo)
            self.assertNotIn("externally modified", existing.read_text(encoding="utf-8"))

            existing.write_text("#!/bin/sh\n# externally modified again\n", encoding="utf-8")
            result = self_update(home, repo)
            self.assertEqual("updated", result.state)
            self.assertNotIn("externally modified again", existing.read_text(encoding="utf-8"))

            uninstall(home)
            self.assertEqual(original, existing.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
