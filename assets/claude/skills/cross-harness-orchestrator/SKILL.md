---
name: cross-harness-orchestrator
description: Orchestrate code changes by planning in Claude, delegating implementation and tests only through cross-harness, verifying the resulting diff, and reporting concisely. Use for every request that changes code or runs project checks.
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
- Medium changes: one bounded exploration, plan, implement, test, review.
  The implementer role runs at effort {{IMPLEMENTER_EFFORT}}. Split a Medium
  change into independently verifiable units before delegating when that
  effort is medium or lower. Delegate a Medium or Large change whole to
  implementer_complex, without splitting, when its units cannot be verified
  independently or when it turns on judgment or policy.
- Large changes: split into independently verifiable units and run them
  sequentially by default.
- Independent read-only investigation, verification, and review roles are
  parallelized by default when there are multiple independent units.
  Parallelize only truly independent units and never exceed the
  configured limit of {{MAX_PARALLEL}}.
  Small changes still do not use explorer or reviewer agents. When write roles
  are delegated in parallel, the second and later roles run in isolated
  worktrees when `dirty_worktree_policy` is not `stop`. Under `stop`, they are
  not isolated and become blocked, so write roles must run sequentially. Adopt
  each isolated result with
  `{{CROSS_HARNESS_BIN}} adopt --run <run_dir>` before incorporating it.
- Security, auth, database, public API, or infrastructure changes require an
  explicit human confirmation and a security review.

## Direct edits

Use Edit or Write for direct project edits only for mechanical changes where
diagnosis is already complete and delegation would merely transcribe prose into
files. Keep the tests corresponding to that change with the delegated executor:
the same head must not write both the change and its tests. Any change over 100
lines, or any change that touches judgment or policy, must go to an implementer.
Every direct-edit diff must receive Codex review and tester verification; this
is mandatory, not optional.

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
{{CROSS_HARNESS_BIN}} task create --role implementer --kind implementation --cwd <repo> --goal <goal> --done-when <condition>
```

Use an absolute repository path for `--cwd`.
Always pass at least one exact `--check` command for `test`, `implementation`,
and `debug` tasks; the executor's reported success is verified against the last
matching command execution. Write each check as an executable command line, not
prose: prose cannot be matched to an execution and is treated as `not_run`.
The command returns the absolute task-file path. Do not include chat history,
discarded approaches, chain-of-thought, secrets, or credential-file content.
Then invoke only:

```text
{{CROSS_HARNESS_BIN}} delegate --role <role> --kind <kind> --task-file <path> --cwd <repo>
```

Use implementer for code, implementer_complex for a change that cannot be split
into independently verifiable units or that turns on judgment or policy, tester
for checks, debugger for complex failures, and security_reviewer only after
explicit human confirmation. Never shorten the
absolute wrapper path to a bare `cross-harness` command.

Invoke `delegate` only in a foreground Bash call; never use `run_in_background`.
Parallel delegation uses multiple foreground Bash `delegate` calls in one
message; `run_in_background` remains forbidden. For parallel write roles, the
second and later roles use isolated worktrees when `dirty_worktree_policy` is
not `stop`; under `stop`, they are not isolated and become blocked, so run
write delegations sequentially. Adopt each isolated result with
`{{CROSS_HARNESS_BIN}} adopt --run <run_dir>`; adoption conflicts leave the
root worktree unchanged and return non-zero.
It prints the run directory first and then waits for the detached supervisor.
If the foreground call is interrupted or times out, do not delegate again. Re-attach
to that printed run with:

```text
{{CROSS_HARNESS_BIN}} wait --run <run_dir> --timeout-seconds <seconds>
```

## Verify

Read the returned summary and paths, inspect `git diff --stat` and the relevant
diff hunks, and compare them against every completion condition. Read full logs
only around an unresolved failure. If correction is needed, write a short delta
instruction and use `{{CROSS_HARNESS_BIN}} retry` with the recorded run directory.
Never exceed two normal retries. Two identical failure signatures permit one
explicit escalation; authentication or rate-limit failures stop immediately.
Runtime cleanup marks incomplete runs as ORPHANED only when their
`supervisor.pid` is not alive.

## Report

Report only changes, verification, and unresolved items. Do not repeat the raw
Codex response or terminal log.
Lead with the outcome, and match length to what the change needs: no filler
sections and no second summary of work already described.
