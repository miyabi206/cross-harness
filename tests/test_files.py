from pathlib import Path
import tempfile
import unittest

from cross_harness.files import MARKER_START, append_marker, atomic_write, remove_marker
from cross_harness.summarize import command_matches_check, failure_signature, parse_events, render_summary


class FileTests(unittest.TestCase):
    def test_marker_round_trip(self):
        before = "user content\n"
        merged = append_marker(before, "managed content")
        self.assertIn(MARKER_START, merged)
        self.assertEqual(before, remove_marker(merged))

    def test_atomic_write_permissions_and_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested/file"
            atomic_write(path, "hello")
            self.assertEqual("hello", path.read_text())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_failure_signature_removes_volatile_numbers(self):
        parsed = {"errors": ["test failed at 12.4s address 0xabc123"], "commands": []}
        first = failure_signature(1, parsed)
        second = failure_signature(1, {"errors": ["test failed at 99.8s address 0xdef999"], "commands": []})
        self.assertEqual(first, second)
        self.assertIsNone(failure_signature(0, {
            "errors": [], "commands": [],
            "executions": [{"command": "uv run pytest -q", "exit_code": 1}],
        }))

    def test_command_match_ignores_read_only_mentions_but_keeps_executable_check(self):
        check = "scripts/test.sh"
        self.assertFalse(command_matches_check('/bin/zsh -lc "cat scripts/test.sh"', check))
        self.assertFalse(command_matches_check('/bin/zsh -lc "grep scripts/test.sh README.md"', check))
        self.assertTrue(command_matches_check(
            '/bin/zsh -lc "env -u CROSS_HARNESS_ACTIVE scripts/test.sh"', check,
        ))
        self.assertTrue(command_matches_check(
            '/bin/zsh -lc "cat scripts/test.sh"', "cat scripts/test.sh",
        ))

    def test_parse_events_reads_claude_stream_result(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            events.write_text(
                '{"type":"result","session_id":"session-123","is_error":true,'
                '"result":"permission denied","usage":{"input_tokens":3,"output_tokens":1}}\n'
            )
            parsed = parse_events(events)
        self.assertEqual("session-123", parsed["thread_id"])
        self.assertEqual({"input_tokens": 3, "output_tokens": 1}, parsed["usage"])
        self.assertEqual(["permission denied"], parsed["errors"])

    def test_parse_events_records_failed_claude_bash_tool_result(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            events.write_text(
                '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"bash-1","name":"Bash","input":{"command":"uv run pytest -q"}}]}}\n'
                '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"bash-1","is_error":true,"content":"2 tests failed"}]}}\n'
            )
            parsed = parse_events(events)
        self.assertEqual([], parsed["errors"])
        self.assertEqual([{
            "command": "uv run pytest -q", "exit_code": 1, "output": "2 tests failed",
        }], parsed["commands"])
        self.assertEqual([{
            "command": "uv run pytest -q", "exit_code": 1,
        }], parsed["executions"])
        self.assertIsNotNone(failure_signature(0, parsed))

    def test_parse_events_records_all_completed_bash_executions_in_order(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            events.write_text(
                '{"type":"item.started","item":{"type":"command_execution","command":"ignored"}}\n'
                '{"type":"item.completed","item":{"type":"command_execution","command":"codex success","exit_code":0,"status":"completed","aggregated_output":"ok"}}\n'
                '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"bash-1","name":"Bash","input":{"command":"claude success"}},{"type":"tool_use","id":"bash-2","name":"Bash","input":{"command":"claude failure"}},{"type":"tool_use","id":"other-1","name":"Read","input":{}}]}}\n'
                '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"bash-1","is_error":false,"content":"ok"}]}}\n'
                '{"type":"item.completed","item":{"type":"command_execution","command":"codex failure","exit_code":7,"status":"failed","aggregated_output":"failed"}}\n'
                '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"bash-2","is_error":true,"content":"failed"},{"type":"tool_result","tool_use_id":"missing","is_error":false,"content":"ignored"}]}}\n'
            )
            parsed = parse_events(events)
        self.assertEqual([
            {"command": "codex success", "exit_code": 0},
            {"command": "claude success", "exit_code": 0},
            {"command": "codex failure", "exit_code": 7},
            {"command": "claude failure", "exit_code": 1},
        ], parsed["executions"])
        self.assertEqual([
            {"command": "codex failure", "exit_code": 7, "output": "failed"},
            {"command": "claude failure", "exit_code": 1, "output": "failed"},
        ], parsed["commands"])

    def test_parse_events_marks_cross_harness_hook_rejection_as_policy_denied(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            events.write_text(
                '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"bash-1","name":"Bash","input":{"command":"git status"}}]}}\n'
                '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"bash-1","is_error":true,"content":"PreToolUse:Bash hook error: [/Users/example/.local/bin/cross-harness hook claude-pre-tool-use]: cross-harness: nested executor launch from delegated Claude is blocked"}]}}\n'
            )
            parsed = parse_events(events)
        self.assertTrue(parsed["commands"][0]["policy_denied"])
        self.assertTrue(parsed["executions"][0]["policy_denied"])

    def test_parse_events_does_not_mark_quoted_hook_rejection_as_policy_denied(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            events.write_text(
                '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"bash-1","name":"Bash","input":{"command":"uv run pytest -q"}}]}}\n'
                '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"bash-1","is_error":true,"content":"FAILED test_policy.py\\nassert output == \'PreToolUse:Bash hook error: [/Users/example/.local/bin/cross-harness hook claude-pre-tool-use]: cross-harness: nested executor launch from delegated Claude is blocked\'"}]}}\n'
            )
            parsed = parse_events(events)
        self.assertNotIn("policy_denied", parsed["commands"][0])

    def test_parse_events_extracts_structured_claude_terminal_categories(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            events.write_text(
                '{"type":"system","subtype":"api_retry","error":"authentication_failed"}\n'
                '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}\n'
            )
            parsed = parse_events(events)
        self.assertEqual("rate_limit", parsed["blocked_category"])

    def test_summary_is_bounded_and_points_to_raw_artifacts(self):
        summary = {
            "status": "failed", "run_dir": "/tmp/run", "exit_code": 1,
            "role": "tester", "model": "luna", "effort": "medium",
            "changed_files": [], "tests": [], "error": "x" * 5000,
            "next_decision": None, "event_log": "/tmp/run/events.jsonl",
            "stderr_log": "/tmp/run/stderr.log", "final_message": "/tmp/run/final.json",
        }
        rendered = render_summary(summary, 1000)
        self.assertLessEqual(len(rendered), 1000)
        self.assertIn("summary truncated", rendered)

    def test_summary_renders_non_string_list_items_deterministically(self):
        summary = {
            "status": "success", "run_dir": "/tmp/run", "exit_code": 0,
            "role": "tester", "model": "luna", "effort": "medium",
            "changed_files": [{"z": 1, "a": "file"}, 42],
            "tests": [{"result": "passed", "command": "uv run pytest -q"}, 7],
            "event_log": "/tmp/run/events.jsonl", "stderr_log": "/tmp/run/stderr.log",
            "final_message": "/tmp/run/final.json",
        }

        rendered = render_summary(summary, 10_000)

        self.assertIn('changed_files: {"a": "file", "z": 1}, 42', rendered)
        self.assertIn('tests: {"command": "uv run pytest -q", "result": "passed"}; 7', rendered)
        self.assertIn("checks: none declared", rendered)

    def test_summary_renders_nonempty_work_completed_and_missing_final_message(self):
        summary = {
            "status": "success", "run_dir": "/tmp/run", "exit_code": 0,
            "role": "tester", "model": "luna", "effort": "medium",
            "changed_files": [], "tests": [], "work_completed": "Implemented the change.",
            "event_log": "/tmp/run/events.jsonl", "stderr_log": "/tmp/run/stderr.log",
            "final_message": None,
        }

        rendered = render_summary(summary, 10_000)

        self.assertIn("work_completed: Implemented the change.", rendered)
        self.assertIn("final_message: not available", rendered)

    def test_summary_omits_empty_work_completed(self):
        summary = {
            "status": "success", "run_dir": "/tmp/run", "exit_code": 0,
            "role": "tester", "model": "luna", "effort": "medium",
            "changed_files": [], "tests": [], "work_completed": "",
            "event_log": "/tmp/run/events.jsonl", "stderr_log": "/tmp/run/stderr.log",
            "final_message": None,
        }

        self.assertNotIn("work_completed:", render_summary(summary, 10_000))


if __name__ == "__main__":
    unittest.main()
