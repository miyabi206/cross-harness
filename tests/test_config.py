from pathlib import Path
import copy
import unittest

from cross_harness.config import default_config, project_config, validate


class ConfigTests(unittest.TestCase):
    def test_defaults_match_required_roles_and_validate(self):
        config = default_config()
        self.assertEqual([], validate(config))
        self.assertEqual("gpt-5.6-terra", config["roles"]["implementer"]["model"])
        self.assertEqual("gpt-5.6-luna", config["roles"]["tester"]["model"])
        self.assertEqual(70, config["context_threshold_percent"])

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

    def test_project_key_must_be_absolute(self):
        config = default_config()
        config["projects"] = {"relative/repo": {"checks": ["test"]}}
        self.assertIn("absolute path", "\n".join(validate(config)))


if __name__ == "__main__":
    unittest.main()
