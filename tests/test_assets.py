from pathlib import Path
import re
import unittest

from cross_harness.installer import CLAUDE_AGENT_ROLES
from cross_harness.paths import source_root


class AssetTests(unittest.TestCase):
    def test_claude_agent_models_effort_and_retrieval_bounds(self):
        assets = source_root() / "assets/claude/agents"
        explorer = (assets / "explorer.md").read_text()
        reviewer = (assets / "reviewer.md").read_text()
        self.assertIn("tools: Read, Glob, Grep", explorer)
        self.assertIn("model: haiku", explorer)
        self.assertIn("effort: low", explorer)
        self.assertIn("at most three", explorer)
        self.assertIn("model: opus", reviewer)
        self.assertIn("effort: high", reviewer)
        self.assertIn("Do not edit files or delegate", " ".join(reviewer.split()))

    def test_claude_executor_agents_use_executor_charter_and_permissions(self):
        assets = source_root() / "assets/claude/agents"
        expected = {
            "implementer.md": (None, None, "Read, Glob, Grep, Bash, Edit, Write"),
            "tester.md": ("haiku", "medium", "Read, Glob, Grep, Bash"),
            "debugger.md": (None, None, "Read, Glob, Grep, Bash, Edit, Write"),
            "security_reviewer.md": (None, None, "Read, Glob, Grep, Bash"),
        }
        for name, (model, effort, tools) in expected.items():
            content = (assets / name).read_text()
            if model is None:
                self.assertNotIn("model:", content)
                self.assertNotIn("effort:", content)
            else:
                self.assertIn(f"model: {model}", content)
                self.assertIn(f"effort: {effort}", content)
            self.assertIn(f"tools: {tools}", content)
            self.assertIn("Cross-harness executor", content)
            self.assertIn("Do not ask the user questions", content)
            self.assertIn("Do not follow the orchestrator charter", content)
            self.assertIn("exactly these six fields", content)
            self.assertNotIn("gpt-", content)
            installed_name = f"cross-harness-{Path(name).stem}"
            self.assertIn(f"name: {installed_name}", content)
            self.assertIn(f"{installed_name}.md", CLAUDE_AGENT_ROLES)

    def test_claude_agent_assets_never_use_codex_model_identifiers(self):
        assets = source_root() / "assets/claude/agents"
        for definition in assets.glob("*.md"):
            with self.subTest(definition=definition.name):
                self.assertNotRegex(definition.read_text(), r"(?m)^model: gpt-")

    def test_codex_role_assets_cover_all_executor_roles(self):
        expected = {
            "explorer.toml": ("gpt-5.6-luna", "medium"),
            "implementer.toml": ("gpt-5.6-terra", "high"),
            "tester.toml": ("gpt-5.6-luna", "medium"),
            "reviewer.toml": ("gpt-5.6-sol", "xhigh"),
            "debugger.toml": ("gpt-5.6-sol", "high"),
            "security_reviewer.toml": ("gpt-5.6-sol", "xhigh"),
        }
        root = source_root() / "assets/codex/agents"
        for name, (model, effort) in expected.items():
            content = (root / name).read_text()
            self.assertIn(f'model = "{model}"', content)
            self.assertIn(f'model_reasoning_effort = "{effort}"', content)
            self.assertIn("launch Claude", content)
            self.assertIn("delegate", content)

    def test_codex_agents_file_is_safe_for_interactive_sessions(self):
        content = (source_root() / "assets/codex/AGENTS.md").read_text()
        self.assertIn("Cross-harness integration", content)
        self.assertIn("no interactive response\nformat requirements", content)
        self.assertNotIn("exactly these six fields", content)
        self.assertNotIn("Do not ask the user questions", content)
        self.assertNotIn("Do not narrate intermediate work", content)
        self.assertNotIn("smallest change", content)

    def test_orchestrator_uses_template_for_every_wrapper_action(self):
        skill = (source_root() / "assets/claude/skills/cross-harness-orchestrator/SKILL.md").read_text()
        for action in ("task create", "delegate", "retry"):
            self.assertIn(f"{{{{CROSS_HARNESS_BIN}}}} {action}", skill)
        self.assertIsNone(re.search(r"`cross-harness (?:task|delegate|retry)", skill))


if __name__ == "__main__":
    unittest.main()
