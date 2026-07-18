from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import unittest

from cross_harness.watch import RunWatcher, format_event, watch


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runs = Path(self.temp.name) / "runtime" / "runs"
        self.runs.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_auto_switches_to_a_newer_run(self):
        first = self.runs / "20260718T174441-11111111"
        first.mkdir()
        (first / "events.jsonl").write_text('{"type":"turn.started"}\n')
        watcher = RunWatcher(self.runs)
        self.assertEqual([], watcher.poll())

        second = self.runs / "20260718T174442-22222222"
        second.mkdir()
        (second / "events.jsonl").write_text('{"type":"turn.completed"}\n')
        self.assertEqual(["[20260718T174442-22222222] turn.completed"], watcher.poll())

    def test_initial_attach_skips_finished_run_history_and_verdict(self):
        run = self.runs / "20260718T174442-2d5c2ebf"
        run.mkdir()
        (run / "events.jsonl").write_text('{"type":"thread.started"}\n{"type":"turn.completed"}\n')
        (run / "state.json").write_text(json.dumps({"status": "success"}))
        self.assertEqual([], RunWatcher(self.runs).poll())

    def test_orphan_marker_does_not_switch_to_an_older_run(self):
        older = self.runs / "20260718T174441-11111111"
        newer = self.runs / "20260718T174442-22222222"
        older.mkdir()
        newer.mkdir()
        watcher = RunWatcher(self.runs)
        self.assertEqual([], watcher.poll())
        (older / "ORPHANED").write_text("marked\n")
        self.assertEqual([], watcher.poll())
        self.assertEqual(newer, watcher.run_dir)

    def test_formats_only_safe_event_details(self):
        command = "python " + "x" * 200
        line = format_event(Path("run"), {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": command, "exit_code": 7, "aggregated_output": "secret"},
        })
        self.assertEqual("[run] item.completed command_execution: python " + "x" * 112 + "…, exit_code=7", line)
        change = format_event(Path("run"), {
            "type": "item.completed",
            "item": {"type": "file_change", "changes": [{"kind": "modified", "path": "src/a.py"}]},
        })
        self.assertEqual("[run] item.completed file_change: modified src/a.py", change)

    def test_formats_claude_tool_use_and_result_details(self):
        command = "uv run pytest " + "x" * 200
        tool_use = format_event(Path("run"), {
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}},
                {"type": "tool_use", "name": "Write", "input": {"file_path": "tests/test_a.py"}},
            ]},
        })
        self.assertEqual(
            "[run] assistant tool_use: Bash uv run pytest " + "x" * 105 + "…, Edit src/a.py, Write tests/test_a.py",
            tool_use,
        )
        tool_result = format_event(Path("run"), {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "is_error": True, "content": "secret"}]},
        })
        self.assertEqual("[run] user tool_result: failed=1", tool_result)

    def test_renders_final_verdict_once(self):
        watcher = RunWatcher(self.runs)
        self.assertEqual([], watcher.poll())
        run = self.runs / "20260718T174442-22222222"
        run.mkdir()
        (run / "state.json").write_text(json.dumps({"status": "partial", "error": "not printed"}))
        self.assertEqual(["[20260718T174442-22222222] verdict: partial"], watcher.poll())
        self.assertEqual([], watcher.poll())

    def test_missing_or_empty_runs_root_waits_quietly(self):
        missing = RunWatcher(self.runs.parent / "missing")
        self.assertEqual([], missing.poll())
        empty = RunWatcher(self.runs)
        self.assertEqual([], empty.poll())

    def test_watch_exits_cleanly_on_keyboard_interrupt(self):
        output = StringIO()
        with patch("cross_harness.watch.load_config", return_value={"runtime_root": str(self.runs.parent)}), patch(
            "cross_harness.watch.time.sleep", side_effect=KeyboardInterrupt
        ):
            with redirect_stdout(output):
                self.assertEqual(0, watch(output=output))
        self.assertEqual("", output.getvalue())


if __name__ == "__main__":
    unittest.main()
