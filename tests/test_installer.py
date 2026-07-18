from pathlib import Path
import json
import shutil
import tempfile
import unittest

from cross_harness.files import MARKER_START
from cross_harness.installer import install, uninstall
from cross_harness.paths import source_root


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
            settings = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual("fable", settings["model"])
            self.assertIn("PreToolUse", settings["hooks"])
            self.assertIn("Bash(cross-harness task:*)", settings["permissions"]["allow"])
            self.assertIn('forced_login_method = "chatgpt"', (home / ".codex/config.toml").read_text())
            self.assertTrue((home / ".local/bin/cross-harness").is_symlink())
            installed_skill = (home / ".claude/skills/cross-harness-orchestrator/SKILL.md").read_text()
            self.assertIn(str(home.resolve() / ".local/bin/cross-harness"), installed_skill)
            self.assertNotIn("{{CROSS_HARNESS_BIN}}", installed_skill)
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
            contents = contents.replace('model = "haiku"', 'model = "custom explorer"')
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
                '[roles.tester]\nharness = "codex"\nmodel = "gpt-5.6-luna"\neffort = "medium"',
                '[roles.tester]\nharness = "claude"\nmodel = "custom-tester"\neffort = "test-effort"',
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

    def test_install_does_not_write_codex_role_settings_to_claude_agents(self):
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
                'harness = "claude"\nmodel = "haiku"\neffort = "low"',
                'harness = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"',
            )
            config.write_text(contents, encoding="utf-8")

            install(home, repo)

            explorer = (home / ".claude/agents/cross-harness-explorer.md").read_text()
            self.assertIn("model: haiku", explorer)
            self.assertIn("effort: low", explorer)

    def test_dry_run_does_not_touch_home(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            home.mkdir()
            actions = install(home, source_root(), dry_run=True)
            self.assertEqual(5, len(actions))
            self.assertEqual([], list(home.iterdir()))

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
