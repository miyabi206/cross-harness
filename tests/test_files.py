from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import shlex
import tempfile
import unittest

from cross_harness.files import MARKER_END, MARKER_START, append_marker, atomic_write, dump_json, extract_marker, load_json, remove_marker
from cross_harness.runner import _check_results
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
        self.assertFalse(command_matches_check("tail -100 | ./scripts/test.sh", check))

    def test_command_match_rejects_empty_checks_without_raising(self):
        for check in ("", " ", "\t\n "):
            for command in ("", " ", "uv run pytest -q", "cd /tmp && scripts/test.sh"):
                with self.subTest(command=command, check=check):
                    self.assertFalse(command_matches_check(command, check))

    def test_command_match_accepts_declared_special_prefix_only_at_segment_head(self):
        checks = (
            "! grep -rn TODO src",
            '"/tmp/path with spaces/test.sh"',
            "'./scripts/test.sh'",
            "$(command -v python3) --version",
            "`command -v python3` --version",
            r"\./scripts/test.sh",
            ": scripts/test.sh",
            "true scripts/test.sh",
            "false scripts/test.sh",
            "eval scripts/test.sh",
            "nohup scripts/test.sh",
        )
        for check in checks:
            accepted = (
                check,
                f"cd /tmp && {check}",
                f"MODE=test {check}",
                f'MODE="test mode" COUNT=2 {check}',
                f"MODE='test mode' COUNT=2 {check}",
                f"MODE=two\\ words {check}",
                f"cd /tmp && MODE=test {check}",
                f"{check} --extra",
                f"{check} && echo done",
                f"set -o pipefail; {check} | tail -100",
                f"set -o pipefail; {check} 2>&1 | tail -100",
            )
            rejected = (
                f"! {check}",
                f": {check}",
                f"# {check}",
                f"true {check}",
                f"false {check}",
                f"eval {check}",
                f"nohup {check}",
                f'MODE="test mode" eval {check}',
                f"result=$({check})",
                f"result=`{check}`",
                f"custom-program {shlex.quote(check)}",
                f"{check}; echo done",
                f"{check}\necho done",
                f"{check} || true",
                f"{check} &",
                f"{check} & echo done",
                f"true || {check}",
                f"true | {check}",
                f"true |& {check}",
                f"{check} | tail -100",
                f"set -o pipefail; set +o pipefail; {check} | tail -100",
                f'custom-program "set -o pipefail"; {check} | tail -100',
            )
            for expected, commands in ((True, accepted), (False, rejected)):
                for command in commands:
                    for invocation in (command, f"bash -lc {shlex.quote(command)}"):
                        with self.subTest(check=check, command=invocation):
                            self.assertEqual(expected, command_matches_check(invocation, check))
                            self.assertEqual(
                                [{"check": check, "status": "passed" if expected else "not_run",
                                  "exit_code": 0 if expected else None}],
                                _check_results([check], [{"command": invocation, "exit_code": 0}]),
                            )

    def test_command_match_rejects_unexecuted_or_status_masked_checks(self):
        check = "uv run pytest -q"
        commands = (
            f'env bash -lc "{check}"',
            f'env sh -c "{check}"',
            f'eval "{check}"',
            f'python -c "{check}"',
            f'custom-program "{check}"',
            f"true || {check}",
            f"echo ready || {check}",
            f"true | {check}",
            f"true |& {check}",
            f"set -o pipefail; true | {check}",
            f"{check} &",
            f"{check} & echo done",
            f"{check} &\necho done",
            f"set -o pipefail; {check} | tail -100 &",
            f"! {check}",
            f": {check}",
            f"# {check}",
            f"true {check}",
            f"false {check}",
            f"eval {check}",
            f"nohup {check}",
            f"MODE=test ! {check}",
            f"result=$({check})",
            f"result=$(echo ready; {check})",
            f"result=$(echo ready\n{check})",
            f"result=$(echo $({check}))",
            f"result=`{check}`",
            f"result=`echo ready; {check}`",
            f"set -o pipefail; set +o pipefail; {check} | tail -100",
            f"set -o pipefail && set +o pipefail && {check} | tail -100",
            f'custom-program "set -o pipefail;"; {check} | tail -100',
            f'custom-program "set -o pipefail"; {check} | tail -100',
        )
        for command in commands:
            for shell in (None, "zsh", "bash", "sh", "/bin/zsh", "/bin/bash", "/bin/sh"):
                for option in ("-c", "-lc") if shell else (None,):
                    invocation = f"{shell} {option} {shlex.quote(command)}" if shell else command
                    with self.subTest(command=invocation):
                        self.assertFalse(command_matches_check(invocation, check))
                        self.assertEqual(
                            [{"check": check, "status": "not_run", "exit_code": None}],
                            _check_results([check], [{"command": invocation, "exit_code": 0}]),
                        )

    def test_command_match_rejects_later_commands_that_can_hide_check_failure(self):
        check = "uv run pytest -q"
        commands = (
            f"{check}; echo $?",
            f"{check}\necho $?",
            f"{check} || true",
            f"{check} && echo done; echo $?",
            f"{check} && echo done\necho $?",
            f"{check} && echo done || true",
            f"{check};\n echo $?",
            f"set -o pipefail; {check} | tail -100; echo $?",
            f"set -o pipefail; {check} | tail -100 || true",
            f"{check} && echo done | tail -100",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(command_matches_check(command, check))
                for shell in ("zsh", "bash", "sh"):
                    self.assertFalse(command_matches_check(f'/bin/{shell} -lc "{command}"', check))

    def test_command_match_keeps_commands_that_preserve_check_failure(self):
        check = "uv run pytest -q"
        commands = (
            check,
            f"{check};",
            f"{check};\n ",
            f"cd /tmp && {check}",
            f"{check} && echo done",
            f"echo ready; {check}",
            f"echo ready\n{check}",
            f"MODE=test {check}",
            f"{check} --verbose",
            f"{check} -k 'some test'",
            f"{check} 2>&1",
            f"{check} > results.txt",
            f"{check} &> results.txt",
            f"{check} &>> results.txt",
            f"{check} 2>&1 && echo done",
            f"{check} && echo 'done; ready | ok'",
            f"set -o pipefail; {check} | tail -100",
            f"set -o pipefail; {check} 2>&1 | tail -100",
            f"set -o pipefail; {check} |& tail -100",
            f"set -o pipefail; {check} | tail -100 && echo done",
            f"set +o pipefail; set -o pipefail; {check} | tail -100",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(command_matches_check(command, check))
                for shell in ("zsh", "bash", "sh", "/bin/zsh", "/bin/bash", "/bin/sh", "/usr/local/bin/bash"):
                    for option in ("-c", "-lc"):
                        invocation = f"{shell} {option} {shlex.quote(command)}"
                        with self.subTest(invocation=invocation):
                            self.assertTrue(command_matches_check(invocation, check))

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
        self.assertIsNone(parsed["rate_limit_resets_at"])

    def test_parse_events_extracts_claude_epoch_reset_in_local_timezone(self):
        epoch = 1784648400
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            events.write_text(json.dumps({
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": epoch},
            }) + "\n")
            parsed = parse_events(events)
        expected = datetime.fromtimestamp(epoch).astimezone()
        self.assertEqual(expected.isoformat(), parsed["rate_limit_resets_at"])
        self.assertIsNotNone(datetime.fromisoformat(parsed["rate_limit_resets_at"]).utcoffset())

    def test_parse_events_extracts_codex_clock_and_date_reset_forms(self):
        reference = datetime(2026, 10, 9, 2, 0).astimezone()
        cases = (
            ("try again at 3:01 AM", reference, datetime(2026, 10, 9, 3, 1)),
            ("TRY AGAIN AT 3:01 am.", reference, datetime(2026, 10, 9, 3, 1)),
            ("try again at 3:01 AM.", datetime(2026, 10, 9, 4).astimezone(), datetime(2026, 10, 10, 3, 1)),
            ("try again at 3:01 AM", datetime(2026, 10, 9, 3, 1).astimezone(), datetime(2026, 10, 10, 3, 1)),
            ("try again at 12:00 AM", reference, datetime(2026, 10, 10)),
            ("try again at 12:00 PM", reference, datetime(2026, 10, 9, 12)),
            ("try again at Oct 12th, 2026 9:00 AM", reference, datetime(2026, 10, 12, 9)),
            ("TRY AGAIN AT oct 12TH, 2026 9:00 am.", reference, datetime(2026, 10, 12, 9)),
            ("try again at Jan 1st, 2027 12:00 PM.", reference, datetime(2027, 1, 1, 12)),
            ("try again at Nov 2nd, 2026 7:15 PM", reference, datetime(2026, 11, 2, 19, 15)),
            ("try again at Dec 3rd, 2026 8:00 AM", reference, datetime(2026, 12, 3, 8)),
        )
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            for message, reference, expected in cases:
                for kind in ("error", "turn.failed"):
                    for nested in (False, True):
                        with self.subTest(message=message, kind=kind, nested=nested, reference=reference):
                            text = "You've hit your usage limit. Upgrade or try again later, or " + message
                            event = {"type": kind}
                            event["error" if nested else "message"] = {"message": text} if nested else text
                            events.write_text(json.dumps(event) + "\n")
                            parsed = parse_events(events, reference_time=reference)
                            self.assertEqual(expected.astimezone().isoformat(), parsed["rate_limit_resets_at"])

    def test_parse_events_extracts_codex_duration_reset_forms(self):
        # Pass UTC to verify reference times are converted to the local timezone.
        reference = datetime(2026, 10, 9, 2, 0, 17, tzinfo=timezone.utc)
        cases = (
            ("try again in 3 hours 2 minutes", timedelta(hours=3, minutes=2)),
            ("TRY AGAIN IN 3 HOURS 2 MINUTES.", timedelta(hours=3, minutes=2)),
            ("try again in 2 days 3 hours 2 minutes.", timedelta(days=2, hours=3, minutes=2)),
            ("try again in 1 day 1 hour 1 minute", timedelta(days=1, hours=1, minutes=1)),
            ("try again in 1 day", timedelta(days=1)),
            ("try again in 2 days", timedelta(days=2)),
            ("try again in 1 hour.", timedelta(hours=1)),
            ("try again in 3 hours", timedelta(hours=3)),
            ("try again in 1 minute", timedelta(minutes=1)),
            ("try again in 2 minutes.", timedelta(minutes=2)),
            ("try again in less than a minute", timedelta(minutes=1)),
            ("TRY AGAIN IN LESS THAN A MINUTE.", timedelta(minutes=1)),
        )
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            for message, duration in cases:
                for kind in ("error", "turn.failed"):
                    with self.subTest(message=message, kind=kind):
                        events.write_text(json.dumps({"type": kind, "error": {"message": message}}) + "\n")
                        parsed = parse_events(events, reference_time=reference)
                        self.assertEqual((reference + duration).astimezone().isoformat(), parsed["rate_limit_resets_at"])

    def test_parse_events_defaults_duration_reference_to_current_time(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            events.write_text('{"type":"error","message":"try again in 1 minute."}\n')
            before = datetime.now().astimezone() + timedelta(minutes=1)
            parsed = parse_events(events)
            after = datetime.now().astimezone() + timedelta(minutes=1)
        reset = datetime.fromisoformat(parsed["rate_limit_resets_at"])
        self.assertLessEqual(before, reset)
        self.assertLessEqual(reset, after)

    def test_parse_events_leaves_reset_unset_for_unexpected_or_non_limit_events(self):
        reference = datetime(2026, 10, 9, 2).astimezone()
        events_to_ignore = [
            {"type": "error", "message": text} for text in (
                "Unexpected error", "try again later.", "try again at 13:00 AM",
                "try again at 0:01 AM", "try again at 3:99 AM", "try again at 3:01 XM",
                "try again at Oct 32nd, 2026 9:00 AM", "try again at Feb 30th, 2026 9:00 AM",
                "try again at Oct 12th, 0000 9:00 AM", "try again in three hours",
                "try again in -1 hour", "try again in " + "9" * 400 + " days",
                "try again at", "try again in", "", None, [], {},
            )
        ] + [
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": value}}
            for value in (None, "not a timestamp", "1784648400", True, [], {}, 10**400, float("nan"), float("inf"))
        ] + [
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1784648400}},
            {"type": "rate_limit_event", "rate_limit_info": None},
            {"type": "result", "is_error": True, "result": "try again in 1 minute"},
            {"type": "assistant", "message": "try again at 3:01 AM"},
            [], None, {"type": []},
        ]
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            self.assertIsNone(parse_events(events)["rate_limit_resets_at"])
            for event in events_to_ignore:
                with self.subTest(event=event):
                    events.write_text(json.dumps(event) + "\n")
                    self.assertIsNone(parse_events(events, reference_time=reference)["rate_limit_resets_at"])

    def test_parse_events_retains_reset_when_later_event_has_no_time(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events.jsonl"
            events.write_text(
                '{"type":"error","message":"try again in 1 hour"}\n'
                '{"type":"turn.failed","error":{"message":"usage limit exceeded"}}\n'
            )
            reference = datetime(2026, 10, 9, tzinfo=timezone.utc)
            parsed = parse_events(events, reference_time=reference)
        self.assertEqual((reference + timedelta(hours=1)).astimezone().isoformat(), parsed["rate_limit_resets_at"])

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
