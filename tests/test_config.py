from pathlib import Path
import copy
import tempfile
import unittest

from cross_harness.config import (
    DELEGATE_KINDS,
    ROLE_DELEGATE_KINDS,
    defaulted_config_paths,
    defaulted_paths,
    default_config,
    effective_mode,
    load_config,
    merge_defaults,
    project_config,
    validate,
    warnings,
)
from cross_harness.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def test_defaults_match_required_roles_and_validate(self):
        config = default_config()
        self.assertEqual([], validate(config))
        self.assertEqual("gpt-5.6-terra", config["roles"]["implementer"]["model"])
        self.assertEqual("claude", config["roles"]["tester"]["harness"])
        self.assertEqual("haiku", config["roles"]["tester"]["model"])
        self.assertEqual("opus", config["roles"]["reviewer"]["model"])
        self.assertNotIn("planner", config["roles"])
        self.assertEqual(DELEGATE_KINDS, ROLE_DELEGATE_KINDS)
        self.assertEqual(70, config["context_threshold_percent"])
        self.assertEqual("allow_delegated", config["dirty_worktree_policy"])
        default_warnings = "\n".join(warnings(config))
        self.assertIn("roles.explorer.effort: has no effect for the haiku model", default_warnings)
        self.assertIn("roles.tester.effort: has no effect for the haiku model", default_warnings)
        self.assertEqual(
            ["review", "security_review"],
            config["roles"]["security_reviewer"]["delegate_kinds"],
        )

    def test_retrying_roles_use_models_in_their_harness_fallback_chain(self):
        config = default_config()
        for role_name, role in config["roles"].items():
            if role["retries"] > 0:
                self.assertIn(
                    role["model"],
                    config["fallback"][role["harness"]],
                    role_name,
                )

    def test_planning_is_not_a_supported_role_delegate_kind(self):
        config = copy.deepcopy(default_config())
        config["roles"]["explorer"]["delegate_kinds"] = ["planning"]
        self.assertIn(
            "roles.explorer.delegate_kinds: unsupported values planning",
            validate(config),
        )

    def test_unknown_missing_and_invalid_values_are_rejected(self):
        config = copy.deepcopy(default_config())
        config["surprise"] = True
        del config["roles"]["tester"]["timeout_seconds"]
        config["max_parallel"] = 9
        config["delegate_kinds"].append("invented")
        config["roles"]["invented"] = copy.deepcopy(config["roles"]["tester"])
        errors = "\n".join(validate(config))
        self.assertIn("unknown key 'surprise'", errors)
        self.assertIn("missing key 'timeout_seconds'", errors)
        self.assertIn("range 1..2", errors)
        self.assertIn("unsupported values invented", errors)
        self.assertIn("roles: unknown key 'invented'", errors)

    def test_project_override_cannot_own_models(self):
        config = default_config()
        config["projects"] = {
            "/tmp/project": {"checks": ["npm run verify:web"], "dirty_worktree_policy": "isolate"}
        }
        self.assertEqual(["npm run verify:web"], project_config(config, Path("/tmp/project/nested"))["checks"])
        config["projects"]["/tmp/project"]["model"] = "external"
        self.assertIn("unknown key 'model'", "\n".join(validate(config)))

    def test_allow_dirty_worktree_policy_is_valid_globally_and_per_project(self):
        config = copy.deepcopy(default_config())
        config["dirty_worktree_policy"] = "allow"
        config["projects"] = {"/tmp/project": {"dirty_worktree_policy": "allow"}}
        self.assertEqual([], validate(config))

        config["dirty_worktree_policy"] = "invalid"
        config["projects"]["/tmp/project"]["dirty_worktree_policy"] = "invalid"
        errors = "\n".join(validate(config))
        self.assertIn("dirty_worktree_policy: expected 'stop', 'isolate', 'allow', or 'allow_delegated'", errors)
        self.assertIn(
            "projects./tmp/project.dirty_worktree_policy: expected 'stop', 'isolate', 'allow', or 'allow_delegated'",
            errors,
        )

    def test_project_key_must_be_absolute(self):
        config = default_config()
        config["projects"] = {"relative/repo": {"checks": ["test"]}}
        self.assertIn("absolute path", "\n".join(validate(config)))

    def test_mode_is_optional_and_defaults_to_on(self):
        config = copy.deepcopy(default_config())
        del config["mode"]
        self.assertEqual([], validate(config))
        self.assertEqual("on", effective_mode(config, Path("/tmp/project")))

    def test_mode_uses_the_closest_project_override(self):
        config = copy.deepcopy(default_config())
        config["mode"] = "off"
        config["projects"] = {
            "/tmp/project": {"mode": "on"},
            "/tmp/project/disabled": {"mode": "off"},
        }
        self.assertEqual("on", effective_mode(config, Path("/tmp/project/work")))
        self.assertEqual("off", effective_mode(config, Path("/tmp/project/disabled/work")))
        self.assertEqual("off", effective_mode(config, Path("/tmp/other")))

    def test_invalid_modes_are_rejected(self):
        config = copy.deepcopy(default_config())
        config["mode"] = "sometimes"
        config["projects"] = {"/tmp/project": {"mode": True}}
        errors = "\n".join(validate(config))
        self.assertIn("mode: expected 'on' or 'off'", errors)
        self.assertIn("projects./tmp/project.mode: expected 'on' or 'off'", errors)

    def test_unknown_efforts_are_warnings_but_empty_efforts_are_errors(self):
        config = copy.deepcopy(default_config())
        config["roles"]["explorer"]["effort"] = "future-effort"
        config["roles"]["reviewer"]["model"] = "any-model-string"
        self.assertEqual([], validate(config))
        self.assertIn("roles.explorer.effort", "\n".join(warnings(config)))

        config["roles"]["explorer"]["effort"] = ""
        self.assertIn("expected non-empty string", "\n".join(validate(config)))

    def test_claude_haiku_effort_is_a_warning_not_an_error(self):
        config = copy.deepcopy(default_config())
        self.assertEqual([], validate(config))
        self.assertIn(
            "roles.explorer.effort: has no effect for the haiku model",
            warnings(config),
        )

    def test_merge_defaults_recursively_overlays_personal_values(self):
        defaults = default_config()
        overrides = {
            "retention_days": 14,
            "delegate_kinds": ["test"],
            "roles": {"tester": {"timeout_seconds": 321}},
        }

        merged = merge_defaults(overrides)

        self.assertEqual(14, merged["retention_days"])
        self.assertEqual(["test"], merged["delegate_kinds"])
        self.assertEqual(321, merged["roles"]["tester"]["timeout_seconds"])
        self.assertEqual(defaults["roles"]["tester"]["model"], merged["roles"]["tester"]["model"])
        self.assertEqual(defaults["fallback"], merged["fallback"])
        self.assertEqual(defaults["roles"]["reviewer"], merged["roles"]["reviewer"])

    def test_defaulted_paths_lists_only_leaves_added_from_defaults(self):
        overrides = {
            "retention_days": 14,
            "delegate_kinds": ["test"],
            "roles": {"tester": {"timeout_seconds": 321}},
            "projects": {"/tmp/personal-only": {"checks": ["test"]}},
        }

        paths = defaulted_paths(overrides)

        self.assertNotIn("retention_days", paths)
        self.assertNotIn("delegate_kinds", paths)
        self.assertNotIn("roles.tester.timeout_seconds", paths)
        self.assertIn("roles.tester.model", paths)
        self.assertIn("fallback.codex", paths)
        self.assertIn("fallback.claude", paths)
        self.assertNotIn("projects./tmp/personal-only.checks", paths)

    def test_partial_personal_config_loads_and_unknown_keys_remain_invalid_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            config = home / ".config/cross-harness/config.toml"
            config.parent.mkdir(parents=True)
            contents = 'retention_days = 14\n[roles.tester]\ntimeout_seconds = 321\n'
            config.write_text(contents, encoding="utf-8")

            loaded = load_config(config, home)

            self.assertEqual(14, loaded["retention_days"])
            self.assertEqual(321, loaded["roles"]["tester"]["timeout_seconds"])
            self.assertEqual([], validate(loaded))
            self.assertEqual(contents, config.read_text(encoding="utf-8"))

            unknown_contents = contents + 'retentoin_days = 21\n'
            config.write_text(unknown_contents, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "unknown key 'retentoin_days'"):
                load_config(config, home)
            self.assertEqual(unknown_contents, config.read_text(encoding="utf-8"))

    def test_defaulted_config_paths_is_empty_when_personal_config_is_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            config = home / ".config/cross-harness/config.toml"
            self.assertEqual([], defaulted_config_paths(config, home))


if __name__ == "__main__":
    unittest.main()
