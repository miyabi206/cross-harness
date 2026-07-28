from pathlib import Path
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

from cross_harness.files import MARKER_START, sha256
from cross_harness.errors import ConfigError, HarnessError
from cross_harness.config import load_config
from cross_harness.installer import install, synchronize_claude_agent_roles, uninstall
from cross_harness.paths import source_root, user_paths


class InstallerTests(unittest.TestCase):
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
            for installed in (home / ".claude").rglob("*"):
                if installed.is_file():
                    self.assertNotIn("{{CONTEXT_THRESHOLD_PERCENT}}", installed.read_text(encoding="utf-8"))
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
            config.write_text(contents.replace("context_threshold_percent = 70", "context_threshold_percent = 63"), encoding="utf-8")
            for source in (
                repo / "assets/shared/safety.md",
                repo / "assets/claude/skills/cross-harness-orchestrator/SKILL.md",
                repo / "assets/claude/agents/explorer.md",
            ):
                source.write_text(source.read_text(encoding="utf-8") + "\nThreshold: {{CONTEXT_THRESHOLD_PERCENT}}\n", encoding="utf-8")

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

    def test_dry_run_does_not_touch_home(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            home.mkdir()
            actions = install(home, source_root(), dry_run=True)
            self.assertEqual(5, len(actions))
            self.assertEqual([], list(home.iterdir()))

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
