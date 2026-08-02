from pathlib import Path
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cross_harness.files import MARKER_START, marker_block, sha256
from cross_harness.errors import ConfigError, HarnessError
from cross_harness.config import load_config
from cross_harness.installer import (
    _installed_drift,
    _reject_install_root_repo,
    _remove_codex_agent_role_keys,
    _remove_codex_config,
    _render_codex_agent_role,
    _git_hooks_path,
    install,
    synchronize_claude_agent_roles,
    synchronize_codex_agent_roles,
    uninstall,
)
from cross_harness.paths import source_root, user_paths


class InstallerTests(unittest.TestCase):
    def test_unreadable_generic_drift_is_reported_without_propagating_oserror(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            home.mkdir()
            path = home / "managed"
            path.write_text("content\n", encoding="utf-8")
            with patch("cross_harness.installer.sha256", side_effect=PermissionError("denied")):
                drift = _installed_drift(
                    [{"path": str(path), "installed_hash": "expected"}],
                    user_paths(home),
                )
            self.assertEqual([str(path)], drift)

    def test_unreadable_installed_symlink_is_reported_as_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "managed"
            path.symlink_to("target")
            with patch("cross_harness.installer.os.readlink", side_effect=PermissionError("denied")):
                drift = _installed_drift(
                    [{"path": str(path), "installed_symlink": "target"}],
                    user_paths(Path(folder)),
                )
            self.assertEqual([str(path)], drift)

    def test_unreadable_codex_config_hash_is_reported_as_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            home.mkdir()
            path = home / ".codex/config.toml"
            path.parent.mkdir()
            path.write_text("forced_login_method = \"chatgpt\"\n", encoding="utf-8")
            with patch("cross_harness.installer.sha256", side_effect=PermissionError("denied")):
                drift = _installed_drift(
                    [{"path": str(path), "management": "codex_config", "installed_hash": "expected"}],
                    user_paths(home),
                )
            self.assertEqual([str(path)], drift)

    def test_install_root_rejection_removes_terminal_controls_from_message(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            home.mkdir()
            repo = home / ".local/share/cross-harness/current/bad\x1b[31mrepo"
            repo.mkdir(parents=True)
            with self.assertRaises(HarnessError) as raised:
                _reject_install_root_repo(repo, user_paths(home))
            self.assertNotIn("\x1b", str(raised.exception))
            self.assertNotIn("\x07", str(raised.exception))

    def test_global_core_hooks_path_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            global_config = root / "gitconfig"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            shared_hooks = root / "shared-hooks"
            env = {**os.environ, "GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_NOSYSTEM": "1"}
            subprocess.run(
                ["git", "config", "--global", "core.hooksPath", str(shared_hooks)],
                cwd=repo,
                env=env,
                check=True,
            )

            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_NOSYSTEM": "1"}):
                install(home, repo)

            self.assertTrue((repo / ".git/hooks/post-commit").is_file())
            self.assertFalse((shared_hooks / "post-commit").exists())

    def test_global_core_hooks_path_is_ignored_for_git_file_worktree_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "repo"
            worktree = root / "worktree"
            global_config = root / "gitconfig"
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            subprocess.run(["git", "worktree", "add", "-q", str(worktree)], cwd=repo, check=True)
            shared_hooks = root / "shared-hooks"
            env = {"GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_NOSYSTEM": "1"}
            subprocess.run(
                ["git", "config", "--global", "core.hooksPath", str(shared_hooks)],
                cwd=repo,
                env={**os.environ, **env},
                check=True,
            )

            with patch.dict(os.environ, env):
                hooks_path = _git_hooks_path(worktree)

            self.assertEqual((repo / ".git/hooks").resolve(), hooks_path)

    def test_install_uses_core_hooks_path_and_uninstall_restores_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            hooks = repo / "custom-hooks"
            hooks.mkdir()
            existing = hooks / "post-commit"
            existing.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            existing.chmod(0o755)
            subprocess.run(["git", "config", "core.hooksPath", "custom-hooks"], cwd=repo, check=True)

            install(home, repo)

            installed = hooks / "post-commit"
            self.assertNotIn("exit 7", installed.read_text(encoding="utf-8"))
            manifest = json.loads((home / ".local/state/cross-harness/install-manifest.json").read_text())
            self.assertTrue(any(record["path"] == str(installed) for record in manifest["records"]))

            uninstall(home)
            self.assertEqual("#!/bin/sh\nexit 7\n", existing.read_text(encoding="utf-8"))

    def test_install_uninstall_restores_exact_user_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            (home / ".claude").mkdir()
            (home / ".codex").mkdir()
            originals = {
                home / ".claude/CLAUDE.md": "personal claude rule\n",
                home / ".claude/settings.json": json.dumps({"model": "fable", "permissions": {"deny": ["WebFetch"]}}) + "\n",
                home / ".codex/AGENTS.md": "personal codex rule\n",
                home / ".codex/config.toml": 'model = "gpt-5.6-sol"\n',
            }
            for path, content in originals.items():
                path.write_text(content, encoding="utf-8")

            actions = install(home, repo)
            self.assertEqual(5, len(actions))
            self.assertIn(MARKER_START, (home / ".claude/CLAUDE.md").read_text())
            installed_claude = (home / ".claude/CLAUDE.md").read_text()
            self.assertIn("## Communication", installed_claude)
            self.assertIn("or above 70 percent.", installed_claude)
            self.assertNotIn("{{CONTEXT_THRESHOLD_PERCENT}}", installed_claude)
            settings = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual("fable", settings["model"])
            self.assertIn("PreToolUse", settings["hooks"])
            self.assertIn("Bash(cross-harness task:*)", settings["permissions"]["allow"])
            self.assertIn('forced_login_method = "chatgpt"', (home / ".codex/config.toml").read_text())
            self.assertTrue((home / ".local/bin/cross-harness").is_symlink())
            installed_skill = (home / ".claude/skills/cross-harness-orchestrator/SKILL.md").read_text()
            self.assertIn(str(home.resolve() / ".local/bin/cross-harness"), installed_skill)
            self.assertNotIn("{{CROSS_HARNESS_BIN}}", installed_skill)
            self.assertIn("configured limit of 2", installed_skill)
            self.assertNotIn("{{MAX_PARALLEL}}", installed_skill)
            for installed in (home / ".claude").rglob("*"):
                if installed.is_file():
                    self.assertNotIn("{{CONTEXT_THRESHOLD_PERCENT}}", installed.read_text(encoding="utf-8"))
                    self.assertNotIn("{{MAX_PARALLEL}}", installed.read_text(encoding="utf-8"))
            explorer = (home / ".claude/agents/cross-harness-explorer.md").read_text()
            reviewer = (home / ".claude/agents/cross-harness-reviewer.md").read_text()
            self.assertIn("model: haiku", explorer)
            self.assertIn("effort: low", explorer)
            self.assertIn("model: opus", reviewer)
            self.assertIn("effort: high", reviewer)
            for agent in ("implementer", "tester", "debugger", "security_reviewer"):
                self.assertTrue((home / f".claude/agents/cross-harness-{agent}.md").is_file())

            uninstall(home)
            for path, content in originals.items():
                self.assertEqual(content, path.read_text(encoding="utf-8"))
            self.assertFalse((home / ".local/bin/cross-harness").exists())

    def test_first_install_records_post_sync_agent_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".config/cross-harness/config.toml"
            config.parent.mkdir(parents=True)
            contents = (repo / "config/default.toml").read_text(encoding="utf-8")
            contents = contents.replace(
                '[roles.implementer]\nharness = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"',
                '[roles.implementer]\nharness = "claude"\nmodel = "sonnet"\neffort = "medium"',
            )
            config.write_text(contents, encoding="utf-8")

            install(home, repo)
            implementer = home / ".claude/agents/cross-harness-implementer.md"
            self.assertIn("model: sonnet", implementer.read_text(encoding="utf-8"))

            manifest = json.loads((home / ".local/state/cross-harness/install-manifest.json").read_text(encoding="utf-8"))
            for record in manifest["records"]:
                if "installed_hash" in record:
                    path = Path(record["path"])
                    self.assertEqual(sha256(path), record["installed_hash"])

            install(home, repo)

    def test_reinstall_allows_codex_config_user_sections(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))

            install(home, repo)
            config = home / ".codex/config.toml"
            with config.open("a", encoding="utf-8") as handle:
                handle.write('\n[projects."/some/path"]\ntrust_level = "trusted"\n\n[hooks.state]\nenabled = true\n')

            install(home, repo)

    def test_default_uninstall_after_reinstall_preserves_codex_config_user_sections(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))

            install(home, repo)
            config = home / ".codex/config.toml"
            with config.open("a", encoding="utf-8") as handle:
                handle.write('\n[projects."/work"]\ntrust_level = "trusted"\n')

            install(home, repo)
            uninstall(home)

            self.assertTrue(config.is_file())
            remaining = config.read_text(encoding="utf-8")
            self.assertIn('trust_level = "trusted"', remaining)
            self.assertNotIn(MARKER_START, remaining)
            self.assertNotIn("forced_login_method", remaining)

    def test_default_uninstall_removes_new_codex_config_without_user_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))

            install(home, repo)
            config = home / ".codex/config.toml"
            uninstall(home)

            self.assertFalse(config.exists())

    def test_default_uninstall_preserves_existing_codex_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".codex/config.toml"
            config.parent.mkdir()
            original = 'model = "gpt-5.6-sol"\n'
            config.write_text(original, encoding="utf-8")

            install(home, repo)
            uninstall(home)

            self.assertTrue(config.is_file())
            self.assertEqual(original, config.read_text(encoding="utf-8"))

    def test_uninstall_rejects_codex_config_user_sections_without_preserve_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))

            install(home, repo)
            config = home / ".codex/config.toml"
            addition = '\n[projects."/work"]\ntrust_level = "trusted"\n'
            with config.open("a", encoding="utf-8") as handle:
                handle.write(addition)

            with self.assertRaisesRegex(HarnessError, "installed files changed"):
                uninstall(home)
            self.assertTrue(config.is_file())
            self.assertIn(addition.strip(), config.read_text(encoding="utf-8"))

    def test_uninstall_rejects_codex_config_directory_in_all_modes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            config = home / ".codex/config.toml"
            config.unlink()
            config.mkdir()
            child = config / "must-remain"
            child.write_text("content\n", encoding="utf-8")

            for preserve in (False, True):
                with self.subTest(preserve_user_changes=preserve):
                    with self.assertRaisesRegex(HarnessError, "installed files changed"):
                        uninstall(home, preserve_user_changes=preserve)
                    self.assertTrue(config.is_dir())
                    self.assertEqual("content\n", child.read_text(encoding="utf-8"))

    def test_default_uninstall_handles_legacy_marker_codex_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            config = home / ".codex/config.toml"
            user_section = '\n[projects."/work"]\ntrust_level = "trusted"\n'
            with config.open("a", encoding="utf-8") as handle:
                handle.write(user_section)
            install(home, repo)

            manifest_path = home / ".local/state/cross-harness/install-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for record in manifest["records"]:
                if Path(record["path"]) == config:
                    record["management"] = "marker"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            uninstall(home)
            remaining = config.read_text(encoding="utf-8")
            self.assertIn(user_section.strip(), remaining)
            self.assertNotIn(MARKER_START, remaining)

    def test_remove_codex_config_preserves_surrounding_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            user_content = b'model = "gpt-5.6-sol"  \n\n\n'
            config.write_bytes(marker_block('forced_login_method = "chatgpt"').encode() + b"\n\n" + user_content)

            _remove_codex_config(config)
            self.assertEqual(user_content, config.read_bytes())

    def test_reinstall_accepts_crlf_codex_config_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            config = home / ".codex/config.toml"
            config.write_bytes(config.read_bytes().replace(b"\n", b"\r\n"))

            install(home, repo)

    def test_reinstall_reports_non_utf8_codex_config_as_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))

            install(home, repo)
            config = home / ".codex/config.toml"
            config.write_bytes(b'forced_login_method = "chatgpt"\n\xff')

            with self.assertRaisesRegex(HarnessError, "installed files changed"):
                install(home, repo)

    def test_reinstall_reports_fifo_codex_config_as_drift_without_reading_it(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO creation is unavailable on this platform")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            config = home / ".codex/config.toml"
            config.unlink()
            try:
                os.mkfifo(config)
            except OSError as exc:
                self.skipTest(f"cannot create FIFO: {exc}")

            with self.assertRaisesRegex(HarnessError, "installed files changed"):
                install(home, repo)

    def test_first_install_rejects_fifo_codex_config_without_reading_it(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO creation is unavailable on this platform")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".codex/config.toml"
            config.parent.mkdir()
            try:
                os.mkfifo(config)
            except OSError as exc:
                self.skipTest(f"cannot create FIFO: {exc}")

            with self.assertRaisesRegex(HarnessError, "cannot manage Codex config"):
                install(home, repo)

    def test_reinstall_reports_oversized_codex_config_as_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            config = home / ".codex/config.toml"
            config.write_bytes(b"#" * (1024 * 1024 + 1))

            with self.assertRaisesRegex(HarnessError, "installed files changed"):
                install(home, repo)

    def test_reinstall_rejects_codex_config_marker_inside_multiline_string(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".codex/config.toml"
            config.parent.mkdir()
            config.write_text(
                'instructions = """\n'
                '# >>> cross-harness managed >>>\n'
                'forced_login_method = "chatgpt"\n'
                '# <<< cross-harness managed <<<\n'
                '"""\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(HarnessError, "not in the root scope"):
                install(home, repo)

    def test_reinstall_accepts_root_marker_after_multiline_table_like_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".codex/config.toml"
            config.parent.mkdir()
            config.write_text(
                'instructions = """\n'
                '[not-a-table]\n'
                '"""\n'
                '# >>> cross-harness managed >>>\n'
                'forced_login_method = "chatgpt"\n'
                '# <<< cross-harness managed <<<\n',
                encoding="utf-8",
            )

            install(home, repo)

    def test_reinstall_allows_codex_config_user_content_over_64_kib(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            config = home / ".codex/config.toml"
            with config.open("a", encoding="utf-8") as handle:
                handle.write("# user content\n" * (64 * 1024 // len("# user content\n") + 1))

            install(home, repo)

    def test_reinstall_rejects_codex_config_marker_after_table_header(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            config = home / ".codex/config.toml"
            config.write_text(
                '[projects."/work"]\n' + config.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(HarnessError, "installed files changed"):
                install(home, repo)
            self.assertEqual(1, config.read_text(encoding="utf-8").count(MARKER_START))

    def test_reinstall_rejects_modified_or_removed_codex_config_marker(self):
        for modification in ("replace", "remove"):
            with self.subTest(modification=modification), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                home = root / "home"
                repo = root / "repo"
                home.mkdir()
                shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))

                install(home, repo)
                config = home / ".codex/config.toml"
                content = config.read_text(encoding="utf-8")
                if modification == "replace":
                    content = content.replace('forced_login_method = "chatgpt"', 'forced_login_method = "api"')
                else:
                    content = content.replace(MARKER_START + '\nforced_login_method = "chatgpt"\n# <<< cross-harness managed <<<\n', "")
                config.write_text(content, encoding="utf-8")

                with self.assertRaisesRegex(HarnessError, "installed files changed"):
                    install(home, repo)

    def test_install_materializes_context_threshold_in_all_template_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".config/cross-harness/config.toml"
            config.parent.mkdir(parents=True)
            contents = (repo / "config/default.toml").read_text(encoding="utf-8")
            config.write_text(
                contents
                .replace("context_threshold_percent = 70", "context_threshold_percent = 63")
                .replace("max_parallel = 2", "max_parallel = 5"),
                encoding="utf-8",
            )
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    '[roles.implementer]\nharness = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"',
                    '[roles.implementer]\nharness = "codex"\nmodel = "gpt-5.6-terra"\neffort = "custom-effort"',
                ),
                encoding="utf-8",
            )
            for source in (
                repo / "assets/shared/safety.md",
                repo / "assets/codex/AGENTS.md",
                repo / "assets/claude/skills/cross-harness-orchestrator/SKILL.md",
                repo / "assets/claude/agents/explorer.md",
            ):
                source.write_text(source.read_text(encoding="utf-8") + "\nThreshold: {{CONTEXT_THRESHOLD_PERCENT}}\n", encoding="utf-8")
            codex_agents = repo / "assets/codex/AGENTS.md"
            codex_agents.write_text(codex_agents.read_text(encoding="utf-8") + "\nParallel limit: {{MAX_PARALLEL}}\n", encoding="utf-8")
            skill = repo / "assets/claude/skills/cross-harness-orchestrator/SKILL.md"
            skill.write_text(skill.read_text(encoding="utf-8") + "\nParallel limit: {{MAX_PARALLEL}}\n", encoding="utf-8")

            install(home, repo)

            for installed in (
                home / ".claude/CLAUDE.md",
                home / ".codex/AGENTS.md",
                home / ".claude/skills/cross-harness-orchestrator/SKILL.md",
                home / ".claude/agents/cross-harness-explorer.md",
            ):
                content = installed.read_text(encoding="utf-8")
                self.assertIn("Threshold: 63", content)
                self.assertNotIn("{{CONTEXT_THRESHOLD_PERCENT}}", content)
            codex_content = (home / ".codex/AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("Parallel limit: 5", codex_content)
            self.assertNotIn("{{MAX_PARALLEL}}", codex_content)
            self.assertIn("Parallel limit: 5", (home / ".claude/skills/cross-harness-orchestrator/SKILL.md").read_text(encoding="utf-8"))
            installed_skill = (home / ".claude/skills/cross-harness-orchestrator/SKILL.md").read_text(encoding="utf-8")
            self.assertIn("effort custom-effort", installed_skill)
            self.assertNotIn("{{IMPLEMENTER_EFFORT}}", installed_skill)

    def test_install_materializes_claude_agent_role_models_and_efforts_from_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".config/cross-harness/config.toml"
            config.parent.mkdir(parents=True)
            contents = (repo / "config/default.toml").read_text(encoding="utf-8")
            contents = contents.replace(
                '[roles.explorer]\nharness = "claude"\nmodel = "haiku"',
                '[roles.explorer]\nharness = "claude"\nmodel = "custom explorer"',
            )
            contents = contents.replace('effort = "low"', 'effort = "future-effort"')
            contents = contents.replace('model = "opus"', 'model = "custom-reviewer"')
            contents = contents.replace(
                'model = "custom-reviewer"\neffort = "high"',
                'model = "custom-reviewer"\neffort = "review-effort"',
            )
            contents = contents.replace(
                '[roles.implementer]\nharness = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"',
                '[roles.implementer]\nharness = "claude"\nmodel = "custom-implementer"\neffort = "implement-effort"',
            )
            contents = contents.replace(
                'harness = "claude"\nmodel = "haiku"\neffort = "medium"',
                'harness = "claude"\nmodel = "custom-tester"\neffort = "test-effort"',
            )
            contents = contents.replace(
                '[roles.debugger]\nharness = "codex"\nmodel = "gpt-5.6-sol"\neffort = "high"',
                '[roles.debugger]\nharness = "claude"\nmodel = "custom-debugger"\neffort = "debug-effort"',
            )
            contents = contents.replace(
                '[roles.security_reviewer]\nharness = "codex"\nmodel = "gpt-5.6-sol"\neffort = "xhigh"',
                '[roles.security_reviewer]\nharness = "claude"\nmodel = "custom-security"\neffort = "security-effort"',
            )
            config.write_text(contents, encoding="utf-8")

            install(home, repo)

            explorer = (home / ".claude/agents/cross-harness-explorer.md").read_text()
            reviewer = (home / ".claude/agents/cross-harness-reviewer.md").read_text()
            implementer = (home / ".claude/agents/cross-harness-implementer.md").read_text()
            tester = (home / ".claude/agents/cross-harness-tester.md").read_text()
            debugger = (home / ".claude/agents/cross-harness-debugger.md").read_text()
            security_reviewer = (home / ".claude/agents/cross-harness-security_reviewer.md").read_text()
            self.assertIn('model: "custom explorer"', explorer)
            self.assertIn("effort: future-effort", explorer)
            self.assertIn("model: custom-reviewer", reviewer)
            self.assertIn("effort: review-effort", reviewer)
            self.assertIn("model: custom-implementer", implementer)
            self.assertIn("effort: implement-effort", implementer)
            self.assertIn("model: custom-tester", tester)
            self.assertIn("effort: test-effort", tester)
            self.assertIn("model: custom-debugger", debugger)
            self.assertIn("effort: debug-effort", debugger)
            self.assertIn("model: custom-security", security_reviewer)
            self.assertIn("effort: security-effort", security_reviewer)

    def test_codex_routed_role_loses_frontmatter_and_claude_sync_restores_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".config/cross-harness/config.toml"
            config.parent.mkdir(parents=True)
            contents = (repo / "config/default.toml").read_text(encoding="utf-8")
            contents = contents.replace(
                '[roles.explorer]\nharness = "claude"\nmodel = "haiku"\neffort = "low"',
                '[roles.explorer]\nharness = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"',
            )
            config.write_text(contents, encoding="utf-8")

            install(home, repo)

            explorer = (home / ".claude/agents/cross-harness-explorer.md").read_text()
            self.assertNotIn("model:", explorer)
            self.assertNotIn("effort:", explorer)
            frontmatter = explorer.split("---\n", 2)[1]
            self.assertNotIn("\n\n", frontmatter)
            with patch("cross_harness.installer.atomic_write") as atomic_write:
                synchronize_claude_agent_roles(user_paths(home), load_config(config, home))
            atomic_write.assert_not_called()
            self.assertEqual(explorer, (home / ".claude/agents/cross-harness-explorer.md").read_text())

            restored = contents.replace(
                '[roles.explorer]\nharness = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"',
                '[roles.explorer]\nharness = "claude"\nmodel = "sonnet"\neffort = "medium"',
            )
            config.write_text(restored, encoding="utf-8")
            synchronize_claude_agent_roles(user_paths(home), load_config(config, home))

            explorer = (home / ".claude/agents/cross-harness-explorer.md").read_text()
            self.assertIn("model: sonnet", explorer)
            self.assertIn("effort: medium", explorer)

    def test_codex_agent_role_sync_updates_removes_and_warns(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".config/cross-harness/config.toml"
            config.parent.mkdir(parents=True)
            contents = (repo / "config/default.toml").read_text(encoding="utf-8")
            contents = contents.replace(
                '[roles.explorer]\nharness = "claude"\nmodel = "haiku"\neffort = "low"',
                '[roles.explorer]\nharness = "codex"\nmodel = "custom-codex"\neffort = "custom-effort"',
            )
            contents = contents.replace(
                '[roles.implementer]\nharness = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"',
                '[roles.implementer]\nharness = "claude"\nmodel = "sonnet"\neffort = "medium"',
            )
            config.write_text(contents, encoding="utf-8")

            install(home, repo)
            explorer = home / ".codex/agents/cross-harness-explorer.toml"
            implementer = home / ".codex/agents/cross-harness-implementer.toml"
            explorer_text = explorer.read_text(encoding="utf-8")
            self.assertIn('model = "custom-codex"', explorer_text)
            self.assertIn('model_reasoning_effort = "custom-effort"', explorer_text)
            implementer_text = implementer.read_text(encoding="utf-8")
            self.assertNotIn("model =", implementer_text)
            self.assertNotIn("model_reasoning_effort =", implementer_text)

            warnings = synchronize_codex_agent_roles(user_paths(home), load_config(config, home))
            self.assertIn("roles 'implementer'", " ".join(warnings))

            updated = contents.replace('model = "custom-codex"', 'model = "new-codex"').replace(
                'effort = "custom-effort"', 'effort = "new-effort"'
            )
            config.write_text(updated, encoding="utf-8")
            synchronize_codex_agent_roles(user_paths(home), load_config(config, home))
            self.assertIn('model = "new-codex"', explorer.read_text(encoding="utf-8"))
            self.assertIn('model_reasoning_effort = "new-effort"', explorer.read_text(encoding="utf-8"))

    def test_codex_agent_role_sync_preserves_unmanaged_agent_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            config = home / ".config/cross-harness/config.toml"
            before = (home / ".codex/agents/cross-harness-implementer.toml").read_text(encoding="utf-8")

            synchronize_codex_agent_roles(user_paths(home), load_config(config, home))

            after = (home / ".codex/agents/cross-harness-implementer.toml").read_text(encoding="utf-8")
            for key in ("name", "sandbox_mode", "developer_instructions"):
                before_line = next(line for line in before.splitlines() if line.startswith(f"{key} ="))
                self.assertIn(before_line, after)

    def test_codex_agent_role_sync_skips_missing_definition_and_continues(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".config/cross-harness/config.toml"
            config.parent.mkdir(parents=True)
            contents = (repo / "config/default.toml").read_text(encoding="utf-8").replace(
                '[roles.explorer]\nharness = "claude"\nmodel = "haiku"\neffort = "low"',
                '[roles.explorer]\nharness = "codex"\nmodel = "custom-explorer"\neffort = "custom-effort"',
            ).replace('model = "gpt-5.6-terra"\neffort = "high"', 'model = "updated-implementer"\neffort = "updated-effort"', 1)
            config.write_text(contents, encoding="utf-8")
            install(home, repo)
            missing = home / ".codex/agents/cross-harness-explorer.toml"
            missing.unlink()

            warnings = synchronize_codex_agent_roles(user_paths(home), load_config(config, home))

            self.assertTrue(any("explorer" in warning and "Missing Codex agent definition" in warning for warning in warnings))
            implementer = (home / ".codex/agents/cross-harness-implementer.toml").read_text(encoding="utf-8")
            self.assertIn('model = "updated-implementer"', implementer)
            self.assertIn('model_reasoning_effort = "updated-effort"', implementer)

    def test_codex_agent_role_sync_limits_managed_keys_to_before_table_header(self):
        text = (
            'name = "agent"\n'
            'model = "before"\n'
            'model_reasoning_effort = "before-effort"\n'
            'sandbox_mode = "workspace-write"\n'
            'developer_instructions = """\n'
            'Keep this instruction.\n'
            '"""\n'
            '[metadata]\n'
            'model = "after"\n'
            'model_reasoning_effort = "after-effort"\n'
        )
        rendered = _render_codex_agent_role(text, {"model": "new", "effort": "new-effort"})
        self.assertIn('model = "new"\nmodel_reasoning_effort = "new-effort"', rendered)
        self.assertIn('[metadata]\nmodel = "after"\nmodel_reasoning_effort = "after-effort"', rendered)

        removed = _remove_codex_agent_role_keys(text)
        self.assertNotIn('model = "before"', removed)
        self.assertNotIn('model_reasoning_effort = "before-effort"', removed)
        self.assertIn('[metadata]\nmodel = "after"\nmodel_reasoning_effort = "after-effort"', removed)

    def test_codex_agent_role_sync_does_not_update_indented_table_fields(self):
        text = 'name = "a"\n  [metadata]\n  model = "inner"\n'

        rendered = _render_codex_agent_role(text, {"model": "new", "effort": "new-effort"}, "implementer")

        self.assertIn('model = "new"\nmodel_reasoning_effort = "new-effort"\n  [metadata]', rendered)
        self.assertIn('  [metadata]\n  model = "inner"\n', rendered)
        self.assertNotIn('  model_reasoning_effort = "new-effort"', rendered)

    def test_claude_agent_role_sync_skips_missing_definition_and_continues(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".config/cross-harness/config.toml"
            config.parent.mkdir(parents=True)
            contents = (repo / "config/default.toml").read_text(encoding="utf-8").replace(
                '[roles.explorer]\nharness = "claude"\nmodel = "haiku"\neffort = "low"',
                '[roles.explorer]\nharness = "claude"\nmodel = "custom-explorer"\neffort = "custom-effort"',
            ).replace(
                '[roles.implementer]\nharness = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"',
                '[roles.implementer]\nharness = "claude"\nmodel = "custom-implementer"\neffort = "custom-effort"',
            )
            config.write_text(contents, encoding="utf-8")
            install(home, repo)
            missing = home / ".claude/agents/cross-harness-explorer.md"
            missing.unlink()

            warnings = synchronize_claude_agent_roles(user_paths(home), load_config(config, home))

            self.assertTrue(any("explorer" in warning and "Missing Claude agent definition" in warning for warning in warnings))
            implementer = (home / ".claude/agents/cross-harness-implementer.md").read_text(encoding="utf-8")
            self.assertIn("model: custom-implementer", implementer)
            self.assertIn("effort: custom-effort", implementer)

    def test_codex_agent_role_sync_ignores_table_like_lines_inside_multiline_strings(self):
        text = (
            'name = "a"\n'
            'developer_instructions = """\n'
            '[note] keep\n'
            '"""\n'
            'model = "old"\n'
            'model_reasoning_effort = "old-effort"\n'
        )
        rendered = _render_codex_agent_role(text, {"model": "new", "effort": "new-effort"}, "implementer")
        self.assertIn('model = "new"\nmodel_reasoning_effort = "new-effort"', rendered)
        self.assertIn("[note] keep", rendered)

    def test_dry_run_does_not_touch_home(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            home.mkdir()
            actions = install(home, source_root(), dry_run=True)
            self.assertEqual(5, len(actions))
            self.assertEqual([], list(home.iterdir()))

    def test_interrupted_runtime_copy_preserves_current_and_retry_cleans_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)

            runtime = home / ".local/share/cross-harness/current"
            installed_cli = runtime / "src/cross_harness/cli.py"
            original = installed_cli.read_text(encoding="utf-8")
            source_cli = repo / "src/cross_harness/cli.py"
            source_cli.write_text(source_cli.read_text(encoding="utf-8") + "\n# updated\n", encoding="utf-8")

            original_copytree = shutil.copytree

            def interrupt_runtime_copy(source, destination, *args, **kwargs):
                if Path(destination).parent.name.startswith(".current.tmp-"):
                    raise KeyboardInterrupt
                return original_copytree(source, destination, *args, **kwargs)

            with patch("cross_harness.installer.shutil.copytree", side_effect=interrupt_runtime_copy):
                with self.assertRaises(KeyboardInterrupt):
                    install(home, repo)

            self.assertEqual(original, installed_cli.read_text(encoding="utf-8"))
            staging = list(runtime.parent.glob(".current.tmp-*"))
            self.assertTrue(staging)

            install(home, repo)

            self.assertIn("# updated", installed_cli.read_text(encoding="utf-8"))
            self.assertEqual([], list(runtime.parent.glob(".current.tmp-*")))

    def test_install_reports_defaulted_settings_for_existing_partial_config_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".config/cross-harness/config.toml"
            config.parent.mkdir(parents=True)
            contents = 'retention_days = 14\n[roles.tester]\ntimeout_seconds = 321\n'
            config.write_text(contents, encoding="utf-8")
            # The partial config supplies two of 84 default leaf settings.
            expected_defaulted_count = 82
            expected_default_action = "default: roles.tester.model"

            dry_run_actions = install(home, repo, dry_run=True)

            self.assertIn(f"defaulted settings: {expected_defaulted_count}", dry_run_actions)
            self.assertEqual(
                expected_defaulted_count,
                len([action for action in dry_run_actions if action.startswith("default: ")]),
            )
            self.assertIn(expected_default_action, dry_run_actions)
            self.assertEqual(contents, config.read_text(encoding="utf-8"))

            install_actions = install(home, repo)

            self.assertIn(f"defaulted settings: {expected_defaulted_count}", install_actions)
            self.assertEqual(
                expected_defaulted_count,
                len([action for action in install_actions if action.startswith("default: ")]),
            )
            self.assertIn(expected_default_action, install_actions)
            self.assertEqual(contents, config.read_text(encoding="utf-8"))

            first_home = root / "first-home"
            first_home.mkdir()
            first_actions = install(first_home, repo, dry_run=True)
            self.assertFalse(any(action.startswith("defaulted settings:") or action.startswith("default: ") for action in first_actions))

    def test_install_updates_existing_installation_and_preserves_personal_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            config = home / ".config/cross-harness/config.toml"
            config.write_text("# personal setting\n" + config.read_text(encoding="utf-8"), encoding="utf-8")
            source = repo / "assets/claude/agents/explorer.md"
            source.write_text(source.read_text(encoding="utf-8") + "\nupdated agent\n", encoding="utf-8")

            actions = install(home, repo)

            installed = home / ".claude/agents/cross-harness-explorer.md"
            self.assertEqual(5, len(actions))
            self.assertTrue(actions[1].startswith("update runtime"))
            self.assertIn("updated agent", installed.read_text(encoding="utf-8"))
            self.assertTrue(config.read_text(encoding="utf-8").startswith("# personal setting\n"))
            manifest = json.loads((home / ".local/state/cross-harness/install-manifest.json").read_text(encoding="utf-8"))
            record = next(record for record in manifest["records"] if record["path"] == str(installed.resolve()))
            self.assertIn("installed_hash", record)
            uninstall(home)
            self.assertFalse((home / ".local/share/cross-harness/current").exists())
            self.assertTrue(config.read_text(encoding="utf-8").startswith("# personal setting\n"))

    def test_install_rejects_install_root_without_changing_it_and_hints_recorded_repo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            install_root = home / ".local/share/cross-harness/current"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install_root.mkdir(parents=True)
            sentinel = install_root / "keep.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            manifest_path = home / ".local/state/cross-harness/install-manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(json.dumps({"repo": str(repo)}) + "\n", encoding="utf-8")

            for dry_run in (False, True):
                with self.subTest(dry_run=dry_run), self.assertRaises(HarnessError) as raised:
                    install(home, install_root, dry_run=dry_run)
                message = str(raised.exception)
                self.assertIn("installation destination", message)
                self.assertIn("--repo", message)
                self.assertIn(str(repo.resolve()), message)
                self.assertEqual("unchanged\n", sentinel.read_text(encoding="utf-8"))

            manifest_path.write_text(json.dumps({"repo": str(install_root)}) + "\n", encoding="utf-8")
            with self.assertRaises(HarnessError) as raised:
                install(home, install_root)
            self.assertNotIn("Try:", str(raised.exception))

    def test_install_reject_hint_quotes_repository_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo with space"
            install_root = home / ".local/share/cross-harness/current"
            home.mkdir()
            repo.mkdir(parents=True)
            manifest_path = home / ".local/state/cross-harness/install-manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(json.dumps({"repo": str(repo)}) + "\n", encoding="utf-8")

            with self.assertRaises(HarnessError) as raised:
                install(home, install_root)
            self.assertIn(f"--repo {shlex.quote(str(repo.resolve()))}", str(raised.exception))

    def test_install_allows_explicit_repository_outside_install_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))

            install(home, repo)

            self.assertTrue((home / ".local/share/cross-harness/current/bin/cross-harness").is_file())

    def test_install_rejects_drift_without_writing_unless_forced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            installed = home / ".claude/agents/cross-harness-explorer.md"
            installed.write_text("user drift\n", encoding="utf-8")
            manifest_path = home / ".local/state/cross-harness/install-manifest.json"
            manifest_before = manifest_path.read_text(encoding="utf-8")

            with self.assertRaises(HarnessError) as raised:
                install(home, repo)

            self.assertIn(str(installed.resolve()), str(raised.exception))
            self.assertEqual("user drift\n", installed.read_text(encoding="utf-8"))
            self.assertEqual(manifest_before, manifest_path.read_text(encoding="utf-8"))
            install(home, repo, force=True)
            self.assertNotEqual("user drift\n", installed.read_text(encoding="utf-8"))

    def test_install_dry_run_reports_update_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            installed = home / ".claude/agents/cross-harness-explorer.md"
            before = installed.read_text(encoding="utf-8")
            (repo / "assets/claude/agents/explorer.md").write_text("new agent\n", encoding="utf-8")

            actions = install(home, repo, dry_run=True)

            self.assertEqual(5, len(actions))
            self.assertTrue(actions[1].startswith("update runtime"))
            self.assertEqual(before, installed.read_text(encoding="utf-8"))

    def test_uninstall_preserves_and_backs_up_generated_personal_config_with_force_or_surgical_mode(self):
        for options in ({"force": True}, {"preserve_user_changes": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                home = root / "home"
                repo = root / "repo"
                home.mkdir()
                shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))

                install(home, repo)
                config = home / ".config/cross-harness/config.toml"
                current = "# My documented preferences\n" + config.read_text(encoding="utf-8")
                config.write_text(current, encoding="utf-8")
                manifest = json.loads((home / ".local/state/cross-harness/install-manifest.json").read_text())
                config_record = next(record for record in manifest["records"] if record["path"] == str(config.resolve()))
                self.assertEqual("personal_config", config_record["management"])

                restored = uninstall(home, **options)

                backup = Path(manifest["backup_root"]) / ".config/cross-harness/config.toml"
                self.assertEqual(current, config.read_text(encoding="utf-8"))
                self.assertEqual(current, backup.read_text(encoding="utf-8"))
                self.assertIn(f"preserved personal config {config.resolve()} (backup: {backup})", restored)

    def test_install_rejects_invalid_existing_personal_config_before_changes_including_dry_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            config = home / ".config/cross-harness/config.toml"
            config.parent.mkdir(parents=True)
            invalid = (repo / "config/default.toml").read_text(encoding="utf-8") + "\n[roles.planner]\nharness = \"codex\"\n"
            config.write_text(invalid, encoding="utf-8")

            for dry_run in (False, True):
                with self.subTest(dry_run=dry_run), self.assertRaises(ConfigError) as raised:
                    install(home, repo, dry_run=dry_run)
                message = str(raised.exception)
                self.assertIn(f"existing personal configuration is invalid: {config.resolve()}", message)
                self.assertIn("roles: unknown key 'planner'", message)
                self.assertIn("No files were changed.", message)
                self.assertIn("Fix the configuration and run install again.", message)
                self.assertNotIn("Backup destination:", message)
                self.assertNotIn(str(repo / ".local/backups"), message)
                self.assertEqual(invalid, config.read_text(encoding="utf-8"))
                self.assertFalse((home / ".local/share/cross-harness/current").exists())
                self.assertFalse((home / ".local/bin/cross-harness").exists())
                self.assertFalse((home / ".local/state/cross-harness/install-manifest.json").exists())

    def test_surgical_uninstall_preserves_post_install_user_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            (home / ".claude").mkdir()
            (home / ".codex").mkdir()
            (home / ".claude/CLAUDE.md").write_text("original\n")
            (home / ".claude/settings.json").write_text('{"permissions":{"allow":["Bash(git status)"]}}\n')
            (home / ".codex/AGENTS.md").write_text("codex original\n")
            (home / ".codex/config.toml").write_text('model = "gpt-5.6-sol"\n')
            install(home, repo)
            with (home / ".claude/CLAUDE.md").open("a") as handle:
                handle.write("user change after install\n")
            settings = json.loads((home / ".claude/settings.json").read_text())
            settings["permissions"]["allow"].append("Bash(git diff)")
            (home / ".claude/settings.json").write_text(json.dumps(settings) + "\n")

            uninstall(home, preserve_user_changes=True)
            charter = (home / ".claude/CLAUDE.md").read_text()
            self.assertIn("original", charter)
            self.assertIn("user change after install", charter)
            self.assertNotIn("cross-harness managed", charter)
            settings = json.loads((home / ".claude/settings.json").read_text())
            self.assertIn("Bash(git status)", settings["permissions"]["allow"])
            self.assertIn("Bash(git diff)", settings["permissions"]["allow"])
            self.assertFalse(any("cross-harness" in str(item) for item in settings.get("hooks", {}).values()))

    def test_surgical_uninstall_preserves_changed_git_hook(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

            install(home, repo)
            hook = repo / ".git/hooks/post-commit"
            changed = "#!/bin/sh\n# user change\nexit 0\n"
            hook.write_text(changed, encoding="utf-8")

            uninstall(home, preserve_user_changes=True)

            self.assertEqual(changed, hook.read_text(encoding="utf-8"))

    def test_surgical_uninstall_preserves_codex_settings_inserted_inside_legacy_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            (home / ".codex").mkdir()
            config = home / ".codex/config.toml"
            config.write_text('model = "gpt-5.6-sol"\n')
            install(home, repo)

            installed = config.read_text()
            marker_end = "# <<< cross-harness managed <<<"
            trust = '[projects."/private/tmp/example"]\ntrust_level = "trusted"\n'
            config.write_text(installed.replace(marker_end, trust + marker_end))

            uninstall(home, preserve_user_changes=True)
            remaining = config.read_text()
            self.assertIn('model = "gpt-5.6-sol"', remaining)
            self.assertIn('[projects."/private/tmp/example"]', remaining)
            self.assertIn('trust_level = "trusted"', remaining)
            self.assertNotIn("forced_login_method", remaining)
            self.assertNotIn("cross-harness managed", remaining)

    def test_purge_runtime_backs_up_then_removes_owned_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            shutil.copytree(source_root(), repo, ignore=shutil.ignore_patterns(".git", ".local", "__pycache__"))
            install(home, repo)
            runtime = home.resolve() / ".local/state/cross-harness"
            (runtime / "runs/example").mkdir(parents=True)
            (runtime / "runs/example/summary.txt").write_text("evidence\n")
            backup_root = Path(json.loads((runtime / "install-manifest.json").read_text())["backup_root"])
            uninstall(home, purge_runtime=True)
            self.assertFalse(runtime.exists())
            self.assertEqual("evidence\n", (backup_root / "runtime-state-at-uninstall/runs/example/summary.txt").read_text())


if __name__ == "__main__":
    unittest.main()
