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
            "implementer_complex.md": (None, None, "Read, Glob, Grep, Bash, Edit, Write"),
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
            self.assertIn(
                "Run each declared check exactly as written as its own command with nothing piped "
                "or appended, because the wrapper reads that command's exit status.",
                " ".join(content.split()),
            )
            self.assertIn("exactly these seven fields", content)
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
            "implementer_complex.toml": ("gpt-5.6-sol", "high"),
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

        implementer_description = re.search(
            r'^description = "(.*)"$', (root / "implementer.toml").read_text(), re.MULTILINE
        ).group(1)
        complex_description = re.search(
            r'^description = "(.*)"$', (root / "implementer_complex.toml").read_text(), re.MULTILINE
        ).group(1)
        self.assertNotEqual(implementer_description, complex_description)
        claude_root = source_root() / "assets/claude/agents"
        implementer_description = re.search(
            r"^description: (.*)$", (claude_root / "implementer.md").read_text(), re.MULTILINE
        ).group(1)
        complex_description = re.search(
            r"^description: (.*)$", (claude_root / "implementer_complex.md").read_text(), re.MULTILINE
        ).group(1)
        self.assertNotEqual(implementer_description, complex_description)

    def test_codex_agents_file_is_safe_for_interactive_sessions(self):
        content = (source_root() / "assets/codex/AGENTS.md").read_text()
        self.assertIn("Cross-harness integration", content)
        self.assertIn("no interactive response\nformat requirements", content)
        self.assertNotIn("exactly these seven fields", content)
        self.assertNotIn("Do not ask the user questions", content)
        self.assertNotIn("Do not narrate intermediate work", content)
        self.assertNotIn("smallest change", content)

    def test_orchestrator_uses_template_for_every_wrapper_action(self):
        skill = (source_root() / "assets/claude/skills/cross-harness-orchestrator/SKILL.md").read_text()
        for action in ("task create", "delegate", "retry", "reply", "wait", "adopt", "discard", "commit", "pending", "revival"):
            self.assertIn(f"{{{{CROSS_HARNESS_BIN}}}} {action}", skill)
        self.assertIsNone(re.search(r"`cross-harness (?:task|delegate|retry|reply|wait|adopt|discard|commit|pending|revival)", skill))

    def test_usage_limit_safety_scheduler_and_documentation_contract(self):
        root = source_root()
        safety = " ".join((root / "assets/shared/safety.md").read_text().split())
        for phrase in (
            "Stop on unknown authentication, recursion detection, or an exhausted retry budget",
            "stop delegating to the limited harness until the recorded reset time",
            "One scheduled continuation after that time is allowed",
            "revival line permits it", "Waiting in a loop is forbidden",
            "Never switch to API billing or an external router",
        ):
            self.assertIn(phrase, safety)
        self.assertNotIn("Stop on unknown authentication, rate limits", safety)
        charter = " ".join((root / "assets/claude/CLAUDE.md").read_text().split())
        self.assertIn("stop delegating to the limited harness until the recorded reset time", charter)
        self.assertIn("schedule one continuation after the reset", charter)
        self.assertIn("waiting in a loop, API billing and external routers are forbidden", charter)
        skill = (root / "assets/claude/skills/cross-harness-orchestrator/SKILL.md").read_text()
        verify = " ".join(skill.split("## Verify", 1)[1].split("## Discuss", 1)[0].split())
        for phrase in (
            "do not delegate to that harness again before", "finish work that does not need it",
            "exactly one one-shot continuation two to five minutes after the reset time",
            "`CronCreate`", "`recurring` to `false`", "`ToolSearch` when it is deferred",
            "repository, the blocked run directory, and the remaining units in order",
            "Tell the user what was scheduled, for when, and that the session must stay open, then end the turn",
            "When the continuation fires, or a later session shows the reminder, confirm eligibility",
            "then carry on with the remaining units", "If the work was continued another way",
            "scheduler tool is unavailable", "reset time is unknown", "revival is disabled",
            "report the reset time", "command to continue", "third consecutive revival is refused",
            "Claude Code itself waits and continues", "orchestrator's own claude.ai usage limit resets",
            "covers delegated runs only", "lost when the session closes", "session-start reminder takes over",
            "Usage-limit blocked isolated units awaiting revival are the exception",
            "are excluded from `pending`", "`waiting until <reset time>`", "`not revivable: <reason>`",
            "Retry is only for eligible runs", "continue the remaining work with a new delegation after any known reset",
            "then dismiss the blocked run", "Checks carry over", "across all repositories",
        ):
            self.assertIn(phrase, verify)
        for path in ("README.md", "docs/runbook.md", "docs/configuration.md"):
            text = " ".join((root / path).read_text().split())
            for phrase in (
                "CronCreate", "recurring=false", "ToolSearch", "two to five minutes",
                "Claude Code itself waits and continues", "claude.ai", "delegated runs only",
                "session", "reminder takes over", "API billing", "external routers",
            ):
                with self.subTest(path=path, phrase=phrase):
                    self.assertIn(phrase, text)
            self.assertNotIn("rate-limit blocks remain safety-policy stops and cannot be resumed", text)

    def test_orchestrator_unit_workflow_and_resolution_contract(self):
        skill = (source_root() / "assets/claude/skills/cross-harness-orchestrator/SKILL.md").read_text()
        normalized = " ".join(skill.split())
        for phrase in (
            "each become one commit", "list exact paths in Scope",
            "must touch disjoint paths", "dependent units run in order",
            "{{MAX_PARALLEL}}", "{{ROLE_PARALLEL_LIMITS}}",
            "explorer runs before planning", "failure that survived a retry",
            "reviewer runs per unit commit", "never while a writer is running",
            "tracked files only", "name the setup command",
            "--commit-message", "git show --stat <sha>", "git show <sha>",
            "Committed unit whose integration failed or is pending: remove the stated cause, then use `{{CROSS_HARNESS_BIN}} adopt --run <run_dir>`",
            "Committed unit whose integration conflicted: delegate that unit again sequentially in the root worktree, citing the kept unit commit sha so the executor can read it with `git show <sha>`, then use `{{CROSS_HARNESS_BIN}} discard --run <run_dir>` for the conflicted run",
            "Partial unit verified another way, root or isolated",
            "Failed isolated unit", "verified another way",
            "Nobody edits the root worktree while a writer is running",
            "including direct edits", "everything changed during a root run",
            "Abandoned isolated run", "pending line must show `none`",
            "sha and subject", "merging that branch is the only step left",
            "commit it yourself as one commit", "never leave it uncommitted",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized)
        self.assertNotIn("sequentially by default", skill)
        self.assertNotIn("git diff --stat", skill)

    def test_all_executor_assets_include_discussion_policy_and_contract(self):
        root = source_root() / "assets"
        definitions = [
            (root / "claude/agents" / name).read_text()
            for name in (
                "debugger.md", "implementer.md", "implementer_complex.md",
                "security_reviewer.md", "tester.md",
            )
        ]
        for content in definitions:
            normalized = " ".join(content.split())
            self.assertEqual(1, normalized.count("broaden scope on your own"))
            for phrase in (
                "Do not ask the user questions", "broaden scope on your own",
                "Never launch the wrapper or either harness", "materially better approach",
                "return `discussion` with `discussion_points`", "evidence with file and line",
                "before changing files whenever possible", "counter only with new evidence",
                "never repeat an answered point", "obstacles a reply cannot resolve",
                "non-blocking concerns", "exactly these seven fields",
            ):
                self.assertIn(phrase, normalized)

    def test_native_agent_assets_have_no_result_contract(self):
        root = source_root() / "assets"
        paths = list((root / "codex/agents").glob("*.toml"))
        paths.extend(root / "claude/agents" / name for name in ("explorer.md", "reviewer.md"))
        for path in paths:
            with self.subTest(path=path.name):
                content = path.read_text()
                self.assertNotIn("seven fields", content)
                self.assertNotIn("discussion_points", content)

    def test_orchestrator_discussion_and_shared_launch_policy(self):
        root = source_root() / "assets"
        skill = (root / "claude/skills/cross-harness-orchestrator/SKILL.md").read_text()
        normalized = " ".join(skill.split())
        self.assertLess(skill.index("## Verify"), skill.index("## Discuss"))
        self.assertLess(skill.index("## Discuss"), skill.index("## Report"))
        for phrase in (
            "answer every point yourself", "reject it with the reason and evidence",
            "Do not ask the user", "settle scope changes", "present both positions to the user",
            "--user-decided", "do not count against the two normal retries",
            "name any left unresolved", "explicit human confirmation and a security review",
        ):
            self.assertIn(phrase, normalized)
        safety = " ".join((root / "shared/safety.md").read_text().split())
        self.assertIn("Never launch Claude from a delegated Codex run or Codex from another Codex run.", safety)
        self.assertIn("Launching has exactly one direction and one level. Discussion flows both ways through discussion results and the reply command.", safety)

    def test_orchestrator_routes_complex_changes_and_templates_effort(self):
        skill = (source_root() / "assets/claude/skills/cross-harness-orchestrator/SKILL.md").read_text()
        normalized_skill = " ".join(skill.split())
        self.assertIn("{{IMPLEMENTER_EFFORT}}", skill)
        self.assertIn("implementer_complex", skill)
        self.assertIn("cannot be verified independently", normalized_skill)
        self.assertIn("turns on judgment or policy", normalized_skill)


if __name__ == "__main__":
    unittest.main()
