from pathlib import Path
import json
import tempfile
import unittest

from cross_harness.errors import HarnessError
from cross_harness.trust import confirm_codex_hook, verify_codex_hook_receipt


class TrustTests(unittest.TestCase):
    def test_review_receipt_must_be_explicit_and_match_current_hook(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            executable = home.resolve() / ".local/bin/cross-harness"
            hooks = home / ".codex/hooks.json"
            hooks.parent.mkdir(parents=True)
            hooks.write_text(json.dumps({
                "hooks": {"PreToolUse": [{
                    "matcher": "^Bash$",
                    "hooks": [{
                        "type": "command",
                        "command": f"{executable} hook codex-pre-tool-use",
                        "timeout": 20,
                    }],
                }]},
            }))

            self.assertFalse(verify_codex_hook_receipt(home)[0])
            with self.assertRaises(HarnessError):
                confirm_codex_hook(home)
            receipt = confirm_codex_hook(home, confirmed_after_review=True)
            self.assertTrue(receipt.exists())
            self.assertTrue(verify_codex_hook_receipt(home)[0])

            data = json.loads(hooks.read_text())
            data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 30
            hooks.write_text(json.dumps(data))
            self.assertFalse(verify_codex_hook_receipt(home)[0])


if __name__ == "__main__":
    unittest.main()
