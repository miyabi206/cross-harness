from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from cross_harness.experiment import (
    collect_claude_metrics,
    collect_codex_metrics,
    first_check_pass,
    render_task_prompt,
)


class ExperimentTests(unittest.TestCase):
    def test_collect_claude_metrics(self) -> None:
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "途中"},
                        {
                            "type": "tool_use",
                            "id": "read-1",
                            "name": "Read",
                            "input": {"file_path": "a.py"},
                        },
                        {
                            "type": "tool_use",
                            "id": "bash-1",
                            "name": "Bash",
                            "input": {"command": "uv run pytest"},
                        },
                        {"type": "tool_use", "id": "task-1", "name": "Task", "input": {}},
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "bash-1",
                            "content": "ok",
                            "is_error": False,
                        }
                    ]
                },
                "tool_use_result": {"stdout": "ok\n", "stderr": ""},
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "usage": {
                    "input_tokens": 1,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "output_tokens": 4,
                },
            },
            {"type": "rate_limit_event"},
        ]
        metrics = collect_claude_metrics(events)
        self.assertEqual(10, metrics.usage)
        self.assertEqual(len("途中".encode()), metrics.message_bytes)
        self.assertEqual(3, metrics.terminal_bytes)
        self.assertEqual(1, metrics.read_operations)
        self.assertEqual(("a.py",), metrics.read_targets)
        self.assertEqual(1, metrics.subagents)
        self.assertEqual(0, metrics.commands[0].exit_code)
        self.assertTrue(metrics.result_success)
        self.assertEqual(1, metrics.rate_limit_events)

    def test_collect_codex_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "run"
            run.mkdir()
            (run / "summary.json").write_text(
                json.dumps(
                    {
                        "raw_artifact_bytes": 100,
                        "summary_bytes": 20,
                        "usage": {"input_tokens": 30, "output_tokens": 5},
                    }
                )
            )
            (run / "state.json").write_text(json.dumps({"attempts": 2}))
            (run / "events.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": "rg TODO src",
                                    "exit_code": 0,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": "done"},
                            }
                        ),
                    ]
                )
                + "\n"
            )
            metrics = collect_codex_metrics([run])
        self.assertEqual(35, metrics.usage)
        self.assertEqual(4, metrics.message_bytes)
        self.assertEqual(100, metrics.raw_terminal_bytes)
        self.assertEqual(20, metrics.summary_bytes)
        self.assertEqual(1, metrics.read_operations)
        self.assertEqual(1, metrics.retries)

    def test_first_check_and_prompt(self) -> None:
        events = collect_claude_metrics(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "b",
                                "name": "Bash",
                                "input": {"command": "cd server && uv run pytest --tb=short"},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "b",
                                "content": "ok",
                                "is_error": False,
                            }
                        ]
                    },
                },
            ]
        )
        self.assertTrue(first_check_pass(["cd server && uv run pytest --tb=short"], events.commands))
        prompt = render_task_prompt(
            {
                "issue": "https://example.invalid/issues/1",
                "goal": "goal",
                "done_when": ["done"],
                "checks": ["check"],
            }
        )
        self.assertIn("別構成のworktree", prompt)
        self.assertIn("commit、push、PR作成は行わない", prompt)


if __name__ == "__main__":
    unittest.main()
