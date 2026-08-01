from __future__ import annotations

from pathlib import Path
import os
import shlex
import subprocess
import tempfile
import unittest

from cross_harness.paths import source_root


class GitHookTests(unittest.TestCase):
    hooks = ("post-merge", "post-commit", "post-checkout", "post-rewrite")

    def _render_hook(self, root: Path, name: str, executable: Path) -> Path:
        hook = root / name
        template = (source_root() / "assets/git" / name).read_text(encoding="utf-8")
        hook.write_text(template.replace("{{CROSS_HARNESS_BIN}}", shlex.quote(str(executable))), encoding="utf-8")
        hook.chmod(0o755)
        return hook

    def _fake_git(self, root: Path) -> Path:
        git = root / "git"
        git.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = rev-parse ] && [ \"$2\" = --show-toplevel ]; then\n"
            "    printf '%s\\n' \"$HOOK_REPO\"\n"
            "    exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        git.chmod(0o755)
        return git

    def test_hooks_fail_open_without_wrapper_non_executable_or_on_self_update_failure(self):
        for hook_name in self.hooks:
            for case in ("missing", "non-executable", "failure"):
                with self.subTest(hook=hook_name, case=case), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    repo = root / "repo"
                    repo.mkdir()
                    self._fake_git(root)
                    executable = root / "cross-harness"
                    if case != "missing":
                        executable.write_text("#!/bin/sh\nexit 19\n", encoding="utf-8")
                        executable.chmod(0o755 if case == "failure" else 0o644)
                    hook = self._render_hook(root, hook_name, executable)

                    environment = {
                        "PATH": str(root),
                        "HOOK_REPO": str(repo),
                    }
                    result = subprocess.run(
                        [str(hook)],
                        cwd=repo,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                    self.assertEqual(0, result.returncode)

    def test_hooks_use_timeout_when_available(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "repo"
            repo.mkdir()
            self._fake_git(root)
            executable = root / "cross-harness"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            timeout = root / "timeout"
            timeout.write_text(
                "#!/bin/sh\n"
                "[ \"$1\" = 300 ] || exit 21\n"
                "shift\n"
                "\"$@\"\n",
                encoding="utf-8",
            )
            timeout.chmod(0o755)
            hook = self._render_hook(root, "post-commit", executable)

            result = subprocess.run(
                [str(hook)],
                cwd=repo,
                env={"PATH": str(root), "HOOK_REPO": str(repo)},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
