import ast
import copy
import json
from pathlib import Path
import re
import tomllib
import unittest

import cross_harness.config as config_module
from cross_harness.paths import source_root


class SchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = source_root()
        cls.schema = json.loads((root / "schema/harness.schema.json").read_text())
        with (root / "config/default.toml").open("rb") as handle:
            cls.default_config = tomllib.load(handle)

    def test_default_config_conforms_to_schema(self):
        self.assertEqual([], self._validate(self.default_config, self.schema))

        invalid = copy.deepcopy(self.default_config)
        invalid["roles"]["orchestrator"]["effort"] = "ZZZ_NOT_AN_EFFORT"
        self.assertIn(
            "$.roles.orchestrator.effort: expected one of",
            "\n".join(self._validate(invalid, self.schema)),
        )

    def test_schema_keys_match_config_constants(self):
        properties = self.schema["properties"]
        self.assertEqual(config_module.TOP_KEYS, set(properties))
        self.assertEqual(config_module.TOP_KEYS - {"projects", "mode"}, set(self.schema["required"]))

        roles = properties["roles"]
        self.assertEqual(config_module.REQUIRED_ROLES, set(roles["properties"]))
        self.assertEqual(config_module.REQUIRED_ROLES, set(roles["required"]))
        self.assertEqual(config_module.ROLE_KEYS, set(self.schema["$defs"]["role"]["properties"]))
        self.assertEqual(config_module.PROJECT_KEYS, set(self.schema["$defs"]["project"]["properties"]))

    def test_schema_enums_match_config_validation(self):
        definitions = self.schema["$defs"]
        self.assertEqual(
            self._validation_values("dirty_worktree_policy"),
            set(definitions["dirtyPolicy"]["enum"]),
        )
        self.assertEqual(
            self._validation_values("mode"),
            set(self.schema["properties"]["mode"]["enum"]),
        )
        self.assertEqual(
            set(self.schema["properties"]["mode"]["enum"]),
            set(definitions["project"]["properties"]["mode"]["enum"]),
        )
        self.assertEqual(config_module.DELEGATE_KINDS, set(definitions["globalDelegateKinds"]["items"]["enum"]))
        self.assertEqual(
            config_module.ROLE_DELEGATE_KINDS,
            set(definitions["roleDelegateKinds"]["items"]["enum"]),
        )
        self.assertEqual(set(config_module.CLAUDE_EFFORTS), self._role_efforts("claude"))
        self.assertEqual(set(config_module.CODEX_EFFORTS), self._role_efforts("codex"))

    def _role_efforts(self, harness):
        for condition in self.schema["$defs"]["role"]["allOf"]:
            if condition["if"]["properties"]["harness"].get("const") == harness:
                return set(condition["then"]["properties"]["effort"]["enum"])
        self.fail(f"no effort enum found for {harness}")

    @staticmethod
    def _validation_values(key):
        tree = ast.parse(Path(config_module.__file__).read_text())
        values = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or not isinstance(node.ops[0], ast.NotIn):
                continue
            if isinstance(node.left, ast.Call) and isinstance(node.left.func, ast.Attribute):
                if node.left.func.attr != "get" or not node.left.args:
                    continue
                argument = node.left.args[0]
            elif isinstance(node.left, ast.Subscript):
                argument = node.left.slice
            else:
                continue
            if not isinstance(argument, ast.Constant) or argument.value != key:
                continue
            if not isinstance(node.comparators[0], ast.Set):
                continue
            values.append({item.value for item in node.comparators[0].elts if isinstance(item, ast.Constant)})
        if not values:
            raise AssertionError(f"no validation enum found for {key}")
        if len({frozenset(value) for value in values}) != 1:
            raise AssertionError(f"inconsistent validation enums found for {key}")
        return values[0]

    def _validate(self, value, schema, path="$"):
        if "$ref" in schema:
            return self._validate(value, self.schema["$defs"][schema["$ref"].rsplit("/", 1)[-1]], path)

        errors = []
        if "const" in schema and value != schema["const"]:
            errors.append(f"{path}: expected {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: expected one of {schema['enum']!r}")
        object_keywords = {"properties", "required", "additionalProperties", "propertyNames"}
        if schema.get("type") == "object" or object_keywords & schema.keys():
            if not isinstance(value, dict):
                return errors + [f"{path}: expected object"]
            properties = schema.get("properties", {})
            for key in schema.get("required", []):
                if key not in value:
                    errors.append(f"{path}: missing {key!r}")
            if schema.get("additionalProperties") is False:
                errors.extend(f"{path}: unknown {key!r}" for key in value.keys() - properties.keys())
            for key, item in value.items():
                if key in properties:
                    errors.extend(self._validate(item, properties[key], f"{path}.{key}"))
                elif isinstance(schema.get("additionalProperties"), dict):
                    errors.extend(self._validate(item, schema["additionalProperties"], f"{path}.{key}"))
            if "propertyNames" in schema:
                pattern = schema["propertyNames"].get("pattern")
                if pattern:
                    errors.extend(f"{path}: invalid property name {key!r}" for key in value if not re.search(pattern, key))
        elif schema.get("type") == "array":
            if not isinstance(value, list):
                return errors + [f"{path}: expected array"]
            if len(value) < schema.get("minItems", 0):
                errors.append(f"{path}: too few items")
            if schema.get("uniqueItems") and len(value) != len({json.dumps(item, sort_keys=True) for item in value}):
                errors.append(f"{path}: duplicate items")
            if "items" in schema:
                for index, item in enumerate(value):
                    errors.extend(self._validate(item, schema["items"], f"{path}[{index}]"))
        elif schema.get("type") == "string":
            if not isinstance(value, str):
                return errors + [f"{path}: expected string"]
            if len(value) < schema.get("minLength", 0):
                errors.append(f"{path}: string too short")
        elif schema.get("type") == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return errors + [f"{path}: expected integer"]
            if value < schema.get("minimum", value) or value > schema.get("maximum", value):
                errors.append(f"{path}: integer out of range")
        elif schema.get("type") == "boolean" and not isinstance(value, bool):
            errors.append(f"{path}: expected boolean")

        for condition in schema.get("allOf", []):
            if not self._validate(value, condition["if"], path):
                errors.extend(self._validate(value, condition["then"], path))
        return errors


if __name__ == "__main__":
    unittest.main()
