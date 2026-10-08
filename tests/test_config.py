from pathlib import Path
import copy
import json
import tempfile
import unittest

from cross_harness.config import (
    DELEGATE_KINDS,
    ROLE_DELEGATE_KINDS,
    defaulted_config_paths,
    defaulted_paths,
    default_config,
    effective_auto_commit,
    effective_mode,
    effective_protected_branches,
    load_config,
    merge_defaults,
    project_config,
    validate,
    warnings,
)
from cross_harness.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def test_discussion_limit_range_and_legacy_default(self):
        self.assertEqual(3, default_config()["max_discussion_rounds"])
        for value in (0, 3, 10, -1, 11, True, 1.5, "3"):
            with self.subTest(value=value):
                config = default_config()
                config["max_discussion_rounds"] = value
                errors = validate(config)
                if type(value) is int and 0 <= value <= 10:
                    self.assertEqual([], errors)
                else:
                    self.assertIn("max_discussion_rounds: expected integer in range 0..10", errors)
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            path = home / "legacy.toml"
            contents = (Path(__file__).resolve().parents[1] / "config/default.toml").read_text()
            contents = contents.replace("max_discussion_rounds = 3\n", "")
            path.write_text(contents)
            self.assertEqual(3, load_config(path, home)["max_discussion_rounds"])
            self.assertIn("max_discussion_rounds", defaulted_config_paths(path, home))
            self.assertEqual(contents, path.read_text())

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
        self.assertTrue(config["project_auto_setup"])
        self.assertTrue(config["auto_commit"])
        self.assertEqual("cross-harness/", config["work_branch_prefix"])
        self.assertEqual(["main", "master"], config["protected_branches"])
        self.assertEqual(4, config["max_parallel"])
        self.assertEqual(
            {name: {"explorer": 3, "implementer": 3, "reviewer": 2}.get(name, 1) for name in config["roles"]},
            {name: role["max_parallel"] for name, role in config["roles"].items()},
        )
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
        self.assertIn("range 1..5", errors)
        self.assertIn("unsupported values invented", errors)
        self.assertIn("roles: unknown key 'invented'", errors)

    def test_max_parallel_accepts_one_through_five_and_rejects_six(self):
        for value in (1, 5):
            config = copy.deepcopy(default_config())
            config["max_parallel"] = value
            for role in config["roles"].values():
                role["max_parallel"] = value
            self.assertEqual([], validate(config), value)

        config = copy.deepcopy(default_config())
        config["max_parallel"] = 6
        config["roles"]["tester"]["max_parallel"] = 6
        errors = "\n".join(validate(config))
        self.assertIn("max_parallel: expected integer in range 1..5", errors)
        self.assertIn("roles.tester.max_parallel: expected integer in range 1..5", errors)

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

    def test_project_auto_setup_is_optional_boolean_globally_and_per_project(self):
        config = copy.deepcopy(default_config())
        self.assertTrue(config["project_auto_setup"])
        del config["project_auto_setup"]
        self.assertEqual([], validate(config))

        config["project_auto_setup"] = False
        config["projects"] = {"/tmp/project": {"project_auto_setup": False}}
        self.assertEqual([], validate(config))

        config["project_auto_setup"] = "no"
        config["projects"]["/tmp/project"]["project_auto_setup"] = 0
        errors = "\n".join(validate(config))
        self.assertIn("project_auto_setup: expected boolean", errors)
        self.assertIn("projects./tmp/project.project_auto_setup: expected boolean", errors)

    def test_commit_settings_load_and_resolve_closest_project_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text(
                'auto_commit = false\nprotected_branches = ["release"]\n'
                'work_branch_prefix = "team/work-1_/"\n'
                '[projects."/tmp/project"]\nauto_commit = true\nprotected_branches = ["main"]\n'
                '[projects."/tmp/project/nested"]\nauto_commit = false\nprotected_branches = []\n'
                '[projects."/tmp/project/inherit"]\nchecks = []\n'
            )
            config = load_config(path, Path(folder))
            self.assertEqual("team/work-1_/", config["work_branch_prefix"])
            for cwd, auto_commit, branches in (
                ("/tmp/other", False, ["release"]),
                ("/tmp/project/work", True, ["main"]),
                ("/tmp/project/nested/work", False, []),
                ("/tmp/project/inherit/work", False, ["release"]),
            ):
                with self.subTest(cwd=cwd):
                    self.assertEqual(auto_commit, effective_auto_commit(config, Path(cwd)))
                    self.assertEqual(branches, effective_protected_branches(config, Path(cwd)))

    def test_invalid_commit_settings_raise_path_specific_load_errors(self):
        values = {
            "auto_commit": ("1", '"true"', "[]"),
            "protected_branches": ('"main"', '["main", "main"]', '[""]', '[1]', '{}'),
            "work_branch_prefix": tuple(json.dumps(value) for value in (
                "", "branch", "/", "/branch/", "branch//", "branch//nested/",
                ".branch/", "-branch/", "branch/.nested/", "branch/-nested/",
                "branch./", "branch.lock/", "branch.lock/nested/", "branch/nested.lock/",
                "branch..name/", "branch/nested..name/", "branch name/", "café/", "branch/\n",
                "branch@/", "branch\\name/",
            )) + ("true", "1", "[]"),
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            for key, literals in values.items():
                locations = ("", '[projects."/tmp/project"]\n') if key != "work_branch_prefix" else ("",)
                for location in locations:
                    for literal in literals:
                        with self.subTest(key=key, location=location, literal=literal):
                            path.write_text(f"{location}{key} = {literal}\n")
                            expected_path = f"projects./tmp/project.{key}" if location else key
                            with self.assertRaises(ConfigError) as raised:
                                load_config(path, Path(folder))
                            self.assertIn(f"{expected_path}: expected", str(raised.exception))

    def test_safe_work_branch_prefixes_and_empty_protected_branches_are_valid(self):
        for prefix in ("a/", "_work/", "cross-harness/", "Team/branch-1.2_/", "a-/", "a.locked/"):
            with self.subTest(prefix=prefix):
                config = default_config()
                config["work_branch_prefix"] = prefix
                config["protected_branches"] = []
                self.assertEqual([], validate(config))
        config["projects"] = {"/tmp/project": {"work_branch_prefix": "local/"}}
        self.assertIn("projects./tmp/project: unknown key 'work_branch_prefix'", validate(config))

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
            for key in ("auto_commit", "work_branch_prefix", "protected_branches"):
                self.assertEqual(default_config()[key], loaded[key])
                self.assertIn(key, defaulted_config_paths(config, home))
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
