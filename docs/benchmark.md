# Benchmark procedure

Select one reproducible completed issue for each task type: small fix, medium
feature, test addition, bug debug, and cross-cutting refactor. For every issue,
create two worktrees from the same commit and use the same goal and completion
conditions. Run one with Claude alone and one with the cross harness. Do not
reuse model output between configurations.

Start from `benchmarks/tasks.template.json`. The selected, validated RenAI set
is recorded in `benchmarks/tasks.json`. Record the completed issue URL,
full 40-character start commit, normalized goal, one to three completion
conditions, and focused project checks for all five task types. Validate both
the manifest and commit availability before creating paired worktrees:

```sh
./bin/cross-harness benchmark-plan \
  --input benchmarks/tasks.json \
  --repo /absolute/path/to/renai
```

Capture Claude usage/transcript counts, Codex JSONL usage, final/intermediate
message bytes, raw and summarized terminal bytes, files read, subagent count,
retry count, elapsed seconds, first/final check pass, human corrections, and
task success. If exact tokens are unavailable, record bytes, request count,
rate-window delta, and elapsed time.

## Isolated experiment runner

Create two detached worktrees for every task at its exact `start_commit`,
install the lockfile dependencies before timing, and run one configuration at a
time. `scripts/benchmark_experiment.py` rejects a dirty worktree or a mismatched
HEAD, renders the same prompt from the task manifest, captures Claude stream
JSON, discovers only new cross-harness run directories for that worktree, runs
the required checks independently, and writes `record.json` plus `audit.json`.

Baseline uses Claude `fable[1m]` at `xhigh` in safe mode. The prompt explicitly
requires reading the repository `AGENTS.md`, so disabling personal
customizations does not disable repository rules. Cross-harness uses the same
Claude model, effort, prompt, and allowed built-in tools with the installed
orchestrator, hooks, and wrapper active. Neither configuration may inspect the
completed PR, completed commit, paired worktree, or paired output.

Example for one pair:

```sh
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/renai_test \
  scripts/benchmark_experiment.py \
  --tasks benchmarks/tasks.json \
  --task-type small_fix \
  --configuration baseline \
  --worktree /private/tmp/cross-harness-benchmark-20260717/small_fix/baseline \
  --artifacts .local/benchmarks/20260717-renai/small_fix/baseline
```

Raw prompts, model streams, check logs, and per-run audits remain under the
ignored `.local/benchmarks/` directory. Only the task manifest, normalized
measurements, aggregate report, and non-sensitive audit conclusions are kept as
durable repository artifacts.

## Metric definitions

| Record field | Exact collection rule |
|---|---|
| `claude_usage` | Sum of Claude result `input_tokens`, cache creation/read input tokens, and `output_tokens`. |
| `codex_usage` | Sum of Codex final `input_tokens` and `output_tokens` across wrapper runs. |
| `message_bytes` | UTF-8 bytes of Claude text blocks plus Codex `agent_message` items. Thinking blocks are excluded. |
| `raw_terminal_bytes` | Baseline Bash stdout/stderr; for cross-harness, non-delegation Claude Bash output plus Codex raw artifacts. |
| `summary_bytes` | Equal to raw terminal bytes for baseline; for cross-harness, non-delegation output plus wrapper summaries. |
| `files_read` | Count of Claude Read/Grep/Glob operations plus Codex read/search shell operations. Unique Claude targets remain in `audit.json`. |
| `subagents` | Claude Task/Agent launches plus cross-harness wrapper runs. |
| `retries` | Sum of `attempts - 1` from cross-harness state; baseline has no wrapper retries. |
| `duration_seconds` | Wall time from Claude invocation through its final result; dependency preparation and independent evaluation are excluded. |
| `first_check_pass` | Result of the first required check observed in the implementation transcript. Missing execution is false. |
| `final_check_pass` | All required checks pass in the independent post-run evaluator. |
| `human_corrections` | Commits or edits made by a human after the model report. Benchmark runs receive no corrective edit. |
| `task_success` | Claude completed, all independent checks passed, and the manual `done_when` audit accepted the result. |

The selected repository is private. Running the experiment necessarily sends
the task prompt and repository material read by the model to Anthropic, and the
cross-harness configuration also sends delegated repository material to
OpenAI. Obtain explicit approval for all ten runs first. Never include `.env`,
credentials, secrets, production data, or external/cloud operations.

Copy `benchmarks/records.template.json`, use the same source issue and start
commit for both configurations of each task, then replace every placeholder
and zero measurement with observed data. All primary metrics are mandatory;
placeholder sources, missing metrics, invalid types, non-positive duration or
terminal/message byte counts, duplicate pairs, and mismatched pair sources are
rejected. Run:

```sh
scripts/benchmark.sh benchmarks/records.json docs/benchmark-results.md
```

The aggregator requires all ten task/configuration pairs. It emits source,
usage/output, and execution/quality tables covering every metric from plan
section 11, then calculates terminal compression, first/final check rates,
retry mean, duration ratio, and a conservative routing recommendation. Model
or effort changes remain manual changes to the personal configuration.
