from pathlib import Path
from unittest.mock import patch
import subprocess
import tempfile
import unittest

from cross_harness.inventory import _auth_status, create_backup
from cross_harness.paths import user_paths


class InventoryTests(unittest.TestCase):
    @patch("cross_harness.inventory.subprocess.run")
    def test_claude_auth_status_parser_accepts_compact_json(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, '{"loggedIn": true}', "")
        self.assertEqual("authenticated", _auth_status(["claude", "auth", "status"], "claude"))

    def test_backup_excludes_credentials_transcripts_and_key_material(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            destination = root / "backup"
            (home / ".claude/agents").mkdir(parents=True)
            (home / ".claude/projects/example").mkdir(parents=True)
            (home / ".codex").mkdir(parents=True)
            (home / ".claude/settings.json").write_text("{}\n")
            (home / ".claude/agents/reviewer.md").write_text("safe\n")
            (home / ".claude/projects/example/transcript.jsonl").write_text("secret\n")
            (home / ".codex/config.toml").write_text("model = 'safe'\n")
            (home / ".codex/auth.json").write_text("credential\n")
            (home / ".codex/private.pem").write_text("key\n")

            create_backup(destination, user_paths(home))
            manifest = (destination / "MANIFEST.txt").read_text()
            self.assertIn(".claude/settings.json", manifest)
            self.assertIn(".claude/agents/reviewer.md", manifest)
            self.assertIn(".codex/config.toml", manifest)
            self.assertNotIn("auth.json", manifest)
            self.assertNotIn("transcript", manifest)
            self.assertNotIn("private.pem", manifest)


if __name__ == "__main__":
    unittest.main()
