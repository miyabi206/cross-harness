from pathlib import Path
import tempfile
import unittest

from cross_harness.files import MARKER_START, append_marker, atomic_write, remove_marker
from cross_harness.summarize import failure_signature, parse_events, render_summary


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


if __name__ == "__main__":
    unittest.main()
