from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json
import os
import re
import tempfile
import unittest

from cross_harness.watch import EventLine, RunWatcher, describe_event, format_event, render_lines, run_header, watch


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
        self.assertRegex(watcher.poll()[0], r"^── \d\d:\d\d:\d\d · 20260718T174441-11111111")

        second = self.runs / "20260718T174442-22222222"
        second.mkdir()
        (second / "events.jsonl").write_text('{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n')
        lines = watcher.poll()
        self.assertRegex(lines[0], r"^── \d\d:\d\d:\d\d · 20260718T174442-22222222")
        self.assertEqual("  ⎿ 10 in / 2 out", lines[1])

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
        watcher.poll()
        (older / "ORPHANED").write_text("marked\n")
        self.assertEqual([], watcher.poll())
        self.assertEqual(newer, watcher.run_dir)

    def test_formats_only_safe_event_details(self):
        command = "python " + "x" * 200
        started = format_event(Path("run"), {
            "type": "item.started",
            "item": {"type": "command_execution", "command": command},
        })
        self.assertNotIn("…", started)
        self.assertEqual(command, started.removeprefix("  ⏺ Bash   ").replace("\n" + " " * 11, ""))
        self.assertTrue(all(len(line) <= 100 for line in started.splitlines()))
        line = format_event(Path("run"), {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": command, "exit_code": 7, "aggregated_output": "command failed"},
        })
        self.assertEqual("  ⎿ command failed\n  ⎿ exit 7", line)
        change = format_event(Path("run"), {
            "type": "item.completed",
            "item": {"type": "file_change", "changes": [{"kind": "modified", "path": "src/a.py"}]},
        })
        self.assertEqual("  ⏺ Edit   src/a.py", change)

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
            "  ⏺ Bash   uv run pytest \n           " + "x" * 89 + "\n           " + "x" * 89 + "\n           " + "x" * 22
            + "\n  ⏺ Edit   src/a.py\n  ⏺ Write  tests/test_a.py",
            tool_use,
        )
        tool_result = format_event(Path("run"), {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "is_error": True, "content": "secret"}]},
        })
        self.assertEqual("  ⎿ error (1)", tool_result)

    def test_renders_final_verdict_once(self):
        watcher = RunWatcher(self.runs)
        self.assertEqual([], watcher.poll())
        run = self.runs / "20260718T174442-22222222"
        run.mkdir()
        (run / "state.json").write_text(json.dumps({"status": "partial", "error": "not printed"}))
        self.assertEqual(["  ◐ partial"], watcher.poll())
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

    def test_describe_codex_event_variants(self):
        self.assertEqual(
            (EventLine("⏺", "Bash", "echo hi", wrap=True),),
            describe_event({"type": "item.started", "item": {"type": "command_execution", "command": "echo hi"}}),
        )
        self.assertEqual(
            (EventLine("⎿", detail="one", wrap=True), EventLine("⎿", detail="two", wrap=True), EventLine("⎿", detail="exit 0", tone="dim")),
            describe_event({"type": "item.completed", "item": {"type": "command_execution", "command": "echo hi", "aggregated_output": "one\ntwo", "exit_code": 0}}),
        )
        self.assertEqual(
            (EventLine("⏺", "Search", "cross harness"),),
            describe_event({"type": "item.completed", "item": {"type": "web_search", "query": "cross harness"}}),
        )
        self.assertEqual(
            (EventLine("✻", detail="I inspected it.", wrap=True),),
            describe_event({"type": "item.completed", "item": {"type": "reasoning", "text": "I inspected it."}}),
        )
        self.assertEqual(
            (EventLine("·", detail="item.started", noise=True),),
            describe_event({"type": "item.started", "item": {"type": "reasoning", "text": "not displayed"}}),
        )
        self.assertEqual((EventLine("·", detail="item.started", noise=True),), describe_event({"type": "item.started"}))

    def test_describe_claude_event_variants(self):
        event = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I will inspect it."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "src/a.py"}},
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "watch"}},
        ]}}
        self.assertEqual(
            (EventLine("›", detail="I will inspect it.", wrap=True), EventLine("⏺", "Read", "src/a.py"), EventLine("⏺", "Grep", "watch")),
            describe_event(event),
        )
        self.assertEqual((EventLine("·", detail="system", noise=True),), describe_event({"type": "system", "subtype": "init"}))

    def test_message_bodies_strip_terminal_controls_and_preserve_newlines(self):
        events = [
            {"type": "item.completed", "item": {"type": "reasoning", "text": "one\n\x1b[31mtwo"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "one\n\x1b[31mtwo"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "one\n\x1b[31mtwo"}]}},
        ]
        for event in events:
            self.assertEqual("one\ntwo", describe_event(event)[0].detail)

    def test_noise_is_hidden_by_default_and_restored_by_all(self):
        event = {"type": "system", "subtype": "init", "secret": "not visible"}
        self.assertEqual([], render_lines(describe_event(event)))
        self.assertEqual(["  · system"], render_lines(describe_event(event), show_all=True))

    def test_command_output_is_visible_but_sensitive_payloads_never_render_even_with_all(self):
        events = [
            {"type": "item.completed", "item": {"type": "command_execution", "command": "echo safe", "aggregated_output": "AGGREGATED_SECRET"}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "TOOL_RESULT_SECRET"}]}},
        ]
        output = "\n".join(line for event in events for line in render_lines(describe_event(event), show_all=True))
        self.assertIn("AGGREGATED_SECRET", output)
        self.assertNotIn("TOOL_RESULT_SECRET", output)
        watcher = RunWatcher(self.runs, show_all=True)
        watcher.poll()
        run = self.runs / "20260718T174442-22222222"
        run.mkdir()
        (run / "state.json").write_text(json.dumps({"status": "failed", "error": "STATE_SECRET", "blocked_reason": "BLOCKED_SECRET"}))
        output = "\n".join(watcher.poll())
        self.assertNotIn("STATE_SECRET", output)
        self.assertNotIn("BLOCKED_SECRET", output)

    def test_command_output_shows_last_ten_lines_and_strips_terminal_controls(self):
        output = "\n".join(f"line {number}" for number in range(12)) + "\n\x1b[31mred\x1b[0m"
        lines = render_lines(describe_event({
            "type": "item.completed",
            "item": {"type": "command_execution", "aggregated_output": output, "exit_code": 1},
        }))
        self.assertEqual("  ⎿ … (3 lines omitted)", lines[0])
        self.assertEqual("  ⎿ line 3", lines[1])
        self.assertEqual("  ⎿ red", lines[-2])
        self.assertEqual("  ⎿ exit 1", lines[-1])
        self.assertNotIn("\x1b", "\n".join(lines))

    def test_command_output_json_is_rendered_without_delegation_summary(self):
        output = '{"status": "ok", "uptime": 42}'
        lines = render_lines(describe_event({
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "cat health.json", "aggregated_output": output, "exit_code": 0},
        }))
        self.assertEqual([f"  ⎿ {output}", "  ⎿ exit 0"], lines)

    def test_empty_command_output_does_not_render_output_lines(self):
        self.assertEqual(
            ["  ⎿ exit 0"],
            render_lines(describe_event({
                "type": "item.completed",
                "item": {"type": "command_execution", "aggregated_output": " \n\t ", "exit_code": 0},
            })),
        )

    def test_color_controls_and_non_tty_default(self):
        line = (EventLine("✔", detail="success", tone="green"),)
        self.assertNotIn("\033[", "\n".join(render_lines(line, color=False)))
        self.assertIn("\033[", "\n".join(render_lines(line, color=True)))
        output = StringIO()
        run = self.runs / "20260718T174442-22222222"
        run.mkdir()
        with patch("cross_harness.watch.load_config", return_value={"runtime_root": str(self.runs.parent)}), patch(
            "cross_harness.watch.time.sleep", side_effect=KeyboardInterrupt
        ), patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertEqual(0, watch(output=output, color="auto"))
        self.assertNotIn("\033[", output.getvalue())
        output = StringIO()
        with patch("cross_harness.watch.load_config", return_value={"runtime_root": str(self.runs.parent)}), patch(
            "cross_harness.watch.time.sleep", side_effect=KeyboardInterrupt
        ):
            self.assertEqual(0, watch(output=output, color="always"))
        self.assertIn("\033[", output.getvalue())

    def test_reasoning_marker_is_cyan_when_color_is_enabled(self):
        self.assertEqual(
            "  \033[36m✻\033[0m summary",
            render_lines((EventLine("✻", detail="summary", wrap=True),), color=True)[0],
        )

    def test_header_once_for_active_run_and_not_completed_run(self):
        active = self.runs / "20260718T174442-22222222"
        active.mkdir()
        (active / "execution.json").write_text(json.dumps({"role_name": "implementer", "harness": "codex"}))
        watcher = RunWatcher(self.runs)
        self.assertRegex(watcher.poll()[0], r"^── \d\d:\d\d:\d\d · implementer · codex")
        self.assertEqual([], watcher.poll())
        (active / "state.json").write_text(json.dumps({"status": "success"}))
        self.assertEqual(["  ✔ success"], watcher.poll())

    def test_long_agent_message_wraps_at_requested_width(self):
        lines = render_lines((EventLine("›", detail="one two three four five six", wrap=True),), width=12)
        self.assertEqual(["  › one two ", "    three ", "    four ", "    five six"], lines)
        self.assertTrue(all(len(line) <= 12 for line in lines))

    def test_multiline_agent_message_wraps_each_paragraph_with_marker_indent(self):
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": "一行目です。\n二行目です。\n三行目です。"}}
        self.assertEqual(
            ["  › 一行目です。", "    二行目です。", "    三行目です。"],
            render_lines(describe_event(event), width=60),
        )

    def test_agent_message_wraps_by_terminal_cell_width(self):
        lines = render_lines((EventLine("›", detail="日本語の発話本文です。次の文です。", wrap=True),), width=16)
        self.assertEqual(["  › 日本語の発話", "    本文です。次", "    の文です。"], lines)
        self.assertTrue(
            all(sum(2 if __import__("unicodedata").east_asian_width(char) in {"W", "F"} else 1 for char in line) <= 16 for line in lines)
        )

    def test_unreadable_execution_header_shows_run_name_once(self):
        run = self.runs / "20260719T151554-e73c766e"
        run.mkdir()
        self.assertRegex(run_header(run), r"· 20260719T151554-e73c766e ──────$")
        self.assertNotIn("20260719T151554-e73c766e · 20260719T151554-e73c766e", run_header(run))

    def test_delegation_result_message_renders_all_fields_safely(self):
        payload = json.dumps({
            "status": "success",
            "work_completed": "implemented\nwith details\x1b[31m",
            "changed_files": ["src/a.py", "tests/test_a.py"],
            "tests": ["pytest", "lint"],
            "error": "none\x1b[0m",
            "next_decision": "ship it",
        })
        rendered = "\n".join(render_lines((EventLine("›", detail=payload, wrap=True),)))
        self.assertIn("status: success", rendered)
        self.assertIn("work_completed:", rendered)
        self.assertIn("implemented", rendered)
        self.assertIn("with details", rendered)
        self.assertIn("changed_files:", rendered)
        self.assertIn("- src/a.py", rendered)
        self.assertIn("tests:", rendered)
        self.assertIn("- pytest", rendered)
        self.assertIn("error: none", rendered)
        self.assertIn("next_decision: ship it", rendered)
        self.assertNotIn("\x1b", rendered)

    def test_delegation_result_omits_empty_and_null_fields(self):
        payload = json.dumps({
            "status": "success",
            "work_completed": "",
            "changed_files": [],
            "tests": [],
            "error": None,
            "next_decision": None,
        })
        rendered = "\n".join(render_lines((EventLine("›", detail=payload, wrap=True),)))
        self.assertEqual("  › status: success", rendered)


if __name__ == "__main__":
    unittest.main()
