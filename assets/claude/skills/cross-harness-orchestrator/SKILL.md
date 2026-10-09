---
name: cross-harness-orchestrator
description: Orchestrate code changes by planning in Claude, delegating implementation and tests only through cross-harness, verifying unit commits, and reporting concisely. Use for every request that changes code or runs project checks.
---

# Cross-harness orchestrator

Do not pipe cross-harness wrapper commands or declaration-check commands into
other commands. Pipelines do not provide accepted check exit-code evidence, and
wrapper argument scanning can reject the resulting invocation.

## Normalize once

After classification and minimum repository inspection, show only:

```yaml
goal: <one line>
done_when:
  - <at most three independently verifiable outcomes>
```

Add `assumptions` with a short reason or `unknowns` only when non-empty. Do not
repeat the user's words. Ask only about a blocking unknown; continue with
reasoned assumptions for everything else. A blocking unknown is never a small
task.

## Classify and route

- Questions and read-only explanations stay in Claude.
- Small code changes: inspect directly, write one task file, delegate once,
  then verify. Do not start explorer or reviewer agents.
- Medium and Large changes: explorer runs before planning, one per independent
  area. Split the change into units that each become one commit, can be verified
  independently, list exact paths in Scope, and declare an executable check.
  implementer takes one such unit at effort {{IMPLEMENTER_EFFORT}};
  implementer_complex takes a change whose units cannot be verified independently
  or that turns on judgment or policy. It is the only writer of its wave unless
  the other units cannot touch its paths. debugger takes a failure that survived
  a retry and runs sequentially.
- Independent units run in parallel waves; dependent units run in order. Units
  delegated in the same message must touch disjoint paths. Files needed by two
  units, including lockfiles, registries, schemas, generated files and
  configuration, belong to one unit or a later sequential unit. Never exceed the
  configured limit of {{MAX_PARALLEL}} or per-role limits: {{ROLE_PARALLEL_LIMITS}}.
- After every unit of a wave is resolved, reviewer runs per unit commit or
  coherent group of commits. tester runs only after every unit is resolved and
  never while a writer is running, because it tests the root worktree.
- Security, auth, database, public API, or infrastructure changes require an
  explicit human confirmation and a security review.

## Direct edits

Use Edit or Write for direct project edits only for mechanical changes where
diagnosis is already complete and delegation would merely transcribe prose into
files. Keep the tests corresponding to that change with the delegated executor:
the same head must not write both the change and its tests. Any change over 100
lines, or any change that touches judgment or policy, must go to an implementer.
Every direct-edit diff must receive Codex review and tester verification; this
is mandatory, not optional. A direct edit is a unit too: after both pass, commit
it yourself as one commit on the work branch and never leave it uncommitted.
If HEAD is protected or detached, first create a branch under the configured
`work_branch_prefix`.

Use Claude explorer with at most three iterative retrieval cycles only for
broad discovery. Reuse its path-and-finding summary; do not repeat the same
whole-repository search in Codex.

## Task file

Create one Markdown file through the wrapper containing only:

1. Goal.
2. Exact scope and relevant paths.
3. Constraints and project rules.
4. Completion conditions.
5. Checks to run, preferring the project's `verify:<area>` entry point.
6. References needed for execution.

Do not use Edit or Write to create it. Invoke
`{{CROSS_HARNESS_BIN}} task create` with
required `--role`, `--kind`, `--cwd`, and `--goal` values, one to three required
`--done-when` values, and only the necessary repeatable `--scope`,
`--constraint`, `--check`, `--reference`, and `--assumption` values:

```text
{{CROSS_HARNESS_BIN}} task create --role implementer --kind implementation --cwd <repo> --goal <goal> --done-when <condition> --scope <exact_path> --check <command> --commit-message <subject>
```

Use an absolute repository path for `--cwd`.
Always pass at least one exact `--check` command for `test`, `implementation`,
and `debug` tasks; the executor's reported success is verified against the last
matching command execution. Write each check as an executable command line, not
prose: prose cannot be matched to an execution and is treated as `not_run`.
Without a passing declared check, a write run ends partial and is not committed.
Pass `--commit-message` on every write task: one line in the repository's own
commit style, as seen in `git log`. The wrapper commits a successful write run
as one commit and moves a root writer to a work branch first when HEAD is
protected or detached. Never commit, merge, rebase, push, delete a branch, or
run worktree commands by hand for delegated work.
The command returns the absolute task-file path. Do not include chat history,
discarded approaches, chain-of-thought, secrets, or credential-file content.
Then invoke only:

```text
{{CROSS_HARNESS_BIN}} delegate --role <role> --kind <kind> --task-file <path> --cwd <repo>
```

Use security_reviewer only after explicit human confirmation. Never shorten the
absolute wrapper path to a bare `cross-harness` command.

Invoke `delegate` only in a foreground Bash call; never use `run_in_background`.
Parallel delegation uses multiple foreground Bash `delegate` calls in one
message; `run_in_background` remains forbidden. The first writer of a wave runs
in the root worktree and the others in isolated worktrees containing tracked
files only (under `isolate`, all writers are isolated). Under `stop`, concurrent
writers block, so run sequentially. If a unit's check needs untracked setup such
as installed dependencies, name the setup command in its task or run the unit
sequentially. Successful isolated unit commits integrate automatically; resolve
retained worktrees as described below.
Nobody edits the root worktree while a writer is running, including direct
edits: everything changed during a root run is attributed to that unit.
The root is the launching worktree, including when it is a linked worktree.
It prints the run directory first and then waits for the detached supervisor.
If the foreground call is interrupted or times out, do not delegate again. Re-attach
to that printed run with:

```text
{{CROSS_HARNESS_BIN}} wait --run <run_dir> --timeout-seconds <seconds>
```

## Verify

Read the summary's `commit`, `integration`, and `pending` lines. Inspect each
unit through `git show --stat <sha>` and `git show <sha>`, not `git diff`, since
the tree is clean after a commit; compare against every completion condition.
Before the next wave, tester or reviewer, and before reporting, resolve every unit
except usage-limit blocked units awaiting revival, as described below:

- Committed root unit or integrated isolated unit: done; resolve any cleanup
  warning that leaves a worktree pending too.
- Committed unit whose integration failed or is pending: remove
  the stated cause, then use
  `{{CROSS_HARNESS_BIN}} adopt --run <run_dir>`.
- Committed unit whose integration conflicted: delegate that unit again
  sequentially in the root worktree, citing the kept unit commit sha so the
  executor can read it with `git show <sha>`, then use
  `{{CROSS_HARNESS_BIN}} discard --run <run_dir>` for the conflicted run.
- Partial unit verified another way, root or isolated: use
  `{{CROSS_HARNESS_BIN}} commit --run <run_dir>`; otherwise retry.
- Failed isolated unit: retry or use
  `{{CROSS_HARNESS_BIN}} discard --run <run_dir>`.
- Abandoned isolated run: use `{{CROSS_HARNESS_BIN}} discard --run <run_dir>`.

The last summary's pending line must show `none`, or confirm no entries with
`{{CROSS_HARNESS_BIN}} pending --cwd <repo>`, before reporting.
Usage-limit blocked isolated units awaiting revival are the exception: they
appear only in `revival`, are excluded from `pending`, and may remain until continuation.
Read full logs only around an unresolved failure. If correction is needed, write a short delta
instruction and use `{{CROSS_HARNESS_BIN}} retry` with the recorded run directory.
Never exceed two normal retries. Two identical failure signatures permit one
explicit escalation; authentication failures stop immediately.
On a delegated usage-limit block, do not delegate to that harness again before
the recorded `rate_limit_resets_at`; finish work that does not need it.
This reset applies to the harness account across all repositories; the wrapper
refuses new delegate, retry and reply calls on that harness until the reset.
Read the summary's `revival` line. If it allows retry after the reset, schedule
exactly one one-shot continuation two to five minutes after the reset time with
the session scheduler tool `CronCreate`, setting `recurring` to `false`; load it
through `ToolSearch` when it is deferred. The scheduled prompt must name the
repository, the blocked run directory, and the remaining units in order.
Tell the user what was scheduled, for when, and that the session must stay open,
then end the turn. Never wait in a loop, switch to API billing or use external routers.

When the continuation fires, or a later session shows the reminder, confirm
eligibility with `{{CROSS_HARNESS_BIN}} revival --cwd <repo>`; continue the
eligible blocked run with
`{{CROSS_HARNESS_BIN}} retry --run-dir <run_dir> --task-file <continuation_file>`,
then carry on with the remaining units. Revival retries resume the recorded
executor thread when present, preserve the normal retry budget, and never
escalate; a third consecutive revival is refused. Checks carry over from the
blocked task when the continuation declares none. If the work was continued
another way, dismiss the reminder with
`{{CROSS_HARNESS_BIN}} revival --dismiss --run <run_dir>`.
The listing distinguishes `eligible`, `waiting until <reset time>`, and
`not revivable: <reason>`. Retry is only for eligible runs. For a run that is
not revivable (unknown reset, disabled configuration or two consecutive revivals),
continue the remaining work with a new delegation after any known reset, then
dismiss the blocked run with `{{CROSS_HARNESS_BIN}} revival --dismiss --run <run_dir>`.
If the scheduler tool is unavailable, the reset time is unknown or revival is
disabled, stop and report the reset time (or `unknown`), the refusal reason, and
the command to continue. For an eligible run use:
`{{CROSS_HARNESS_BIN}} retry --run-dir <run_dir> --task-file <continuation_file>`.
For a not revivable run, report a new `{{CROSS_HARNESS_BIN}} delegate` command for
the remaining work followed by dismissal. The wrapper still refuses ineligible runs.

Claude Code itself waits and continues when the orchestrator's own claude.ai
usage limit resets; this feature covers delegated runs only. A scheduled
continuation is lost when the session closes; the session-start reminder takes
over in the next session.
Runtime cleanup marks incomplete runs as ORPHANED only when their
`supervisor.pid` is not alive.

## Discuss

When a run returns `discussion`, answer every point yourself: accept it with a
changed instruction or reject it with the reason and evidence. Send all answers
in a task file with:

```text
{{CROSS_HARNESS_BIN}} reply --run-dir <run_dir> --task-file <reply_file>
```

Create the reply file with `{{CROSS_HARNESS_BIN}} task create` like any task
file. Checks declared in the original task carry over when the reply declares
none.

Do not ask the user; you may settle scope changes with the executor. Discussion
replies do not count against the two normal retries. When `reply` refuses at
`max_discussion_rounds`, present both positions to the user and send the decision
with:

```text
{{CROSS_HARNESS_BIN}} reply --run-dir <run_dir> --task-file <decision_file> --user-decided
```

Weigh non-blocking `discussion_points` on a finished run before reporting, and
name any left unresolved in the report. The explicit human confirmation and
security review rule for security, auth, database, public API, or infrastructure
changes still applies.

## Report

Report only changes, verification, and unresolved items. Do not repeat the raw
Codex response or terminal log.
Name the work branch and list each unit commit with sha and subject. State that
merging that branch is the only step left to the user, and list anything unresolved.
Lead with the outcome, and match length to what the change needs: no filler
sections and no second summary of work already described.
