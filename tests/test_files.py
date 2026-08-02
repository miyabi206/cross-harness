from pathlib import Path
import tempfile
import unittest

from cross_harness.files import MARKER_END, MARKER_START, append_marker, atomic_write, dump_json, extract_marker, load_json, remove_marker
from cross_harness.summarize import command_matches_check, failure_signature, normalize_comparison_path, parse_events, render_summary


class FileTests(unittest.TestCase):
    def test_marker_round_trip(self):
        before = "user content\n"
        merged = append_marker(before, "managed content")
        self.assertIn(MARKER_START, merged)
        self.assertEqual(before, remove_marker(merged))

    def test_extract_marker_rejects_invalid_marker_structure(self):
        managed = f"{MARKER_START}\nmanaged\n{MARKER_END}"
        cases = (
            f"prefix {MARKER_START}\nmanaged\n{MARKER_END}",
            f"{managed}\n{managed}",
            f"{MARKER_END}\n{MARKER_START}\nmanaged",
        )

        for content in cases:
            with self.subTest(content=content):
                self.assertIsNone(extract_marker(content))

    def test_atomic_write_permissions_and_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested/file"
            atomic_write(path, "hello")
            self.assertEqual("hello", path.read_text())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_json_surrogate_fallback_keeps_output_valid_utf8_and_unicode_literal(self):
        value = {"label": "日本語", "path": "bad-\udcff-name.txt"}
        rendered = dump_json(value)

        self.assertIn("日本語", rendered)
        self.assertIn(r"\udcff", rendered)
        self.assertNotIn("\udcff", rendered)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            atomic_write(path, rendered)
            self.assertEqual(value, load_json(path, None))
            path.read_bytes().decode("utf-8")

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

    def test_command_match_rejects_check_piped_to_another_command_without_pipefail(self):
        check = "./scripts/test.sh"
        self.assertFalse(command_matches_check(
            "./scripts/test.sh 2>&1 | tail -100", check,
        ))
        self.assertFalse(command_matches_check("grep 'a|b' README.md", check))
        self.assertTrue(command_matches_check(
            "set -o pipefail; ./scripts/test.sh 2>&1 | tail -100", check,
        ))
        self.assertTrue(command_matches_check("tail -100 | ./scripts/test.sh", check))

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

    def test_parse_events_marks_rejected_overage_as_notice(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            events.write_text(
                '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1784648400,"rateLimitType":"five_hour","overageStatus":"allowed","overageResetsAt":1784640000,"isUsingOverage":true}}\n'
            )
            parsed = parse_events(events)
        self.assertIsNone(parsed["blocked_category"])
        self.assertEqual("overage_allowed", parsed["rate_limit_notice"])

    def test_summary_is_bounded_and_points_to_raw_artifacts(self):
        summary = {
            "status": "failed", "run_dir": "/tmp/run", "exit_code": 1,
            "role": "tester", "model": "luna", "effort": "medium",
            "changed_files": [], "tests": [], "error": "x" * 5000,
            "next_decision": None, "event_log": "/tmp/run/events.jsonl",
            "stderr_log": "/tmp/run/stderr.log", "final_message": "/tmp/run/final.json",
            "final_text": "/tmp/run/final.txt",
        }
        rendered = render_summary(summary, 1000)
        self.assertLessEqual(len(rendered), 1000)
        self.assertIn("summary truncated", rendered)

    def test_summary_renders_non_string_list_items_deterministically(self):
        summary = {
            "status": "success", "run_dir": "/tmp/run", "exit_code": 0,
            "role": "tester", "model": "luna", "effort": "medium",
            "changed_files": [{"z": 1, "a": "file"}, 42],
            "reported_changed_files": ["observed.txt", "reported-only.txt"],
            "unverified_changed_files": ["reported-only.txt"],
            "unreported_changed_files": ["observed-only.txt"],
            "tests": [{"result": "passed", "command": "uv run pytest -q"}, 7],
            "event_log": "/tmp/run/events.jsonl", "stderr_log": "/tmp/run/stderr.log",
            "final_message": "/tmp/run/final.json",
            "final_text": "/tmp/run/final.txt",
        }

        rendered = render_summary(summary, 10_000)

        self.assertIn('changed_files: {"a": "file", "z": 1}, 42', rendered)
        self.assertIn("reported_changed_files: observed.txt, reported-only.txt", rendered)
        self.assertIn("unverified_changed_files: reported-only.txt", rendered)
        self.assertIn("unreported_changed_files: observed-only.txt", rendered)
        self.assertIn('tests (executor-reported): {"command": "uv run pytest -q", "result": "passed"}; 7', rendered)
        self.assertIn("checks: none declared", rendered)

    def test_summary_renders_nonempty_work_completed_and_missing_final_message(self):
        summary = {
            "status": "success", "run_dir": "/tmp/run", "exit_code": 0,
            "role": "tester", "model": "luna", "effort": "medium",
            "changed_files": [], "tests": [], "work_completed": "Implemented the change.",
            "event_log": "/tmp/run/events.jsonl", "stderr_log": "/tmp/run/stderr.log",
            "final_message": None,
            "final_text": "/tmp/run/final.txt",
        }

        rendered = render_summary(summary, 10_000)

        self.assertIn("work_completed (executor-reported): Implemented the change.", rendered)
        self.assertIn("final_message: not available", rendered)
        self.assertIn("final_text: /tmp/run/final.txt", rendered)

    def test_summary_omits_empty_work_completed(self):
        summary = {
            "status": "success", "run_dir": "/tmp/run", "exit_code": 0,
            "role": "tester", "model": "luna", "effort": "medium",
            "changed_files": [], "tests": [], "work_completed": "",
            "event_log": "/tmp/run/events.jsonl", "stderr_log": "/tmp/run/stderr.log",
            "final_message": None,
            "final_text": None,
        }

        self.assertNotIn("work_completed (executor-reported):", render_summary(summary, 10_000))
        self.assertNotIn("unverified_changed_files:", render_summary(summary, 10_000))
        self.assertNotIn("unreported_changed_files:", render_summary(summary, 10_000))

    def test_comparison_path_normalization_preserves_summary_item_handling(self):
        cwd = Path("/tmp/project")
        self.assertEqual("nested/file.txt", normalize_comparison_path("./nested//file.txt", cwd))
        self.assertEqual("nested/file.txt", normalize_comparison_path("/tmp/project/nested/file.txt", cwd))
        self.assertEqual("null", normalize_comparison_path(None, cwd))

    def test_summary_renders_overage_allowed_notice(self):
        summary = {
            "status": "success", "run_dir": "/tmp/run", "exit_code": 0,
            "role": "tester", "model": "luna", "effort": "medium",
            "changed_files": [], "tests": [], "rate_limit_notice": "overage_allowed",
            "event_log": "/tmp/run/events.jsonl", "stderr_log": "/tmp/run/stderr.log",
            "final_message": None,
            "final_text": None,
        }

        self.assertIn("rate_limit_notice: overage_allowed", render_summary(summary, 10_000))

    def test_summary_renders_bounded_last_unrelated_failed_command(self):
        summary = {
            "status": "success", "run_dir": "/tmp/run", "exit_code": 0,
            "role": "reviewer", "model": "luna", "effort": "medium",
            "changed_files": [], "tests": [], "unrelated_failed_command_count": 1,
            "last_unrelated_failed_command": {"command": "x" * 600, "exit_code": 17},
            "event_log": "/tmp/run/events.jsonl", "stderr_log": "/tmp/run/stderr.log",
            "final_message": None,
            "final_text": None,
        }

        rendered = render_summary(summary, 10_000)

        self.assertIn("last_unrelated_failed_command: " + "x" * 497 + "... (exit 17)", rendered)


if __name__ == "__main__":
    unittest.main()
