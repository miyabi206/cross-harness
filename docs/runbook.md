# Operations runbook

## Normal operation

1. Start Claude Code normally. The user-level charter loads the orchestrator
   skill for code-changing requests; no prefix or dedicated chat command is
   required.
2. Confirm the SessionStart message says subscription checks passed. If it
   reports API-key variables or missing Claude auth, stop and correct the shell
   environment or login.
3. Let Claude create the bounded task file and call only `cross-harness
   delegate`. Inspect `summary.txt`; open raw logs only around an unresolved
   failure.
4. At each phase boundary, migrate to a new session when context use reaches
   the configured threshold (70 percent by default).

## Watching a delegated run

When a detached `cross-harness delegate` starts, its first stdout line is the
run directory. To follow the newest active run from another terminal, use:

```sh
cross-harness watch
```

Use `cross-harness watch --all` to include lifecycle events. Stop watching with
Ctrl-C; it does not stop the delegated run.

For Codex command executions, watch shows the complete command when it starts,
wrapping it to the terminal width. When the command completes, it shows the
last 10 lines of its aggregated output, an omitted-line count when applicable,
and its exit code. ANSI escapes and control characters are removed before this
output is rendered.

Watch output is written only to the terminal and is not added to the
orchestrator context. The complete raw event log is in the run directory's
`events.jsonl`. `aggregated_output` from Codex `command_execution` events is
shown, but Claude `tool_result` bodies and error text from `state.json` are not
shown, even with `--all`.

To start this watcher automatically when VS Code opens a repository folder,
run the following once for each repository:

```sh
cross-harness project setup --cwd /path/to/repository
```

This adds the folder-open task to that repository's `.vscode/tasks.json`; it
does not run merely because VS Code starts. VS Code must be configured to allow
automatic tasks for the folder. Remove the repository-local task with
`cross-harness project remove --cwd /path/to/repository`. Use `--dry-run` with
either command to see the planned change without writing files.

Run `cross-harness doctor` after either CLI upgrades, authentication changes,
hook changes, or a reinstall. Run `cross-harness cleanup` when stale run
directories need immediate maintenance; SessionStart also invokes it.

## Orchestrator action record

The non-delegated Claude `PreToolUse` hook appends one JSON object per `Edit`,
`Write`, and `Bash` invocation to
`<runtime_root>/orchestrator-actions.jsonl`. Each row has an UTC timestamp,
tool name, final `allowed` or `denied` decision, target (`file_path` for
`Edit`/`Write`, full command string for `Bash`), and hook `cwd`. This covers
every orchestrator Bash call; it does not attempt to infer whether a command
writes files. Delegated Claude executions are deliberately excluded because
their run records and executor logs are the authoritative record.

If a recorded target or cwd matches the credential detector, the row instead
contains only timestamp, tool name, decision, and `redacted: true`. Inspect
the file as JSON Lines (one object per line); it is stored only under the
runtime root. The active file rolls at 5 MiB into up to four numbered older
files, dropping the oldest archive first, so recent action records remain
available without unbounded growth.

## Verification constraints

For `test`, `implementation`, and `debug` work, a task must declare an
executable verification command. Otherwise a reported `success` is downgraded
to `partial` and the CLI exits non-zero; automation that assumes a zero exit
code must account for that verification requirement.

Inside a delegated Claude executor, all five checks in `tests/test_hooks.py`
fail deterministically because wrapper resolution and `PATH` differ there.
Consequently the Claude `tester` role cannot validate the complete
`scripts/test.sh` suite. Delegate deterministic full-suite verification to a
Codex executor with `kind=test` instead.

## Stop conditions

Stop delegation immediately for any of the following:

- an `*_API_KEY` variable is present;
- Codex cannot prove ChatGPT authentication;
- Claude authentication is unavailable;
- user or project Codex config selects a non-OpenAI provider or custom base URL;
- a rate or usage limit is reported;
- a delegated run attempts to start Claude or another Codex executor;
- a write task finds a dirty worktree under the default `stop` policy;
- two identical failures have already triggered the single escalation run;
- a high-risk task lacks explicit human confirmation.

Do not wait automatically, buy credits, switch login method, seed an auth file,
or route to an API model.

## Failure and recovery

Every run directory contains `task.md`, `command.json`, `events.jsonl`,
`stderr.log`, `final.json`, `diff-stat.txt`, `summary.json`, `summary.txt`, and
`state.json` when the process reached finalization. A timeout or user interrupt
also creates `INTERRUPTED`; an abandoned directory older than one hour is marked
`ORPHANED` on the next cleanup.

`summary.txt` lists both sides of the executor's changed-file declaration:
`unverified_changed_files` are files declared by the executor but not observed
in the worktree diff, while `unreported_changed_files` are observed changes not
declared by the executor. The latter is a visibility-only warning: it can signal
an external worktree change during a run or an executor omission, but does not
alter the run status, block category, or exit code.

For a correctable failure, write only the changed instruction into a new task
file and resume from the failed run. The normal budget is two retries. When the
same normalized signature occurs twice, the wrapper performs one escalation
(Luna→Terra→Sol and/or one effort step) and marks `escalated=true`. A run whose
executor explicitly returned `blocked` can be retried. Authentication and
rate-limit blocks remain safety-policy stops and cannot be resumed. A
dirty-worktree or missing-isolated-worktree block has no reusable result, so
create a new delegation instead.

By default, `dirty_worktree_policy="allow_delegated"` lets a write role
continue in a dirty worktree only when every dirty file was recorded by a prior
write delegation and has not changed since. Otherwise, the wrapper stops the
write role so the worktree can be reviewed before continuing.

With `dirty_worktree_policy="isolate"`, the wrapper creates a detached Git
worktree below the run directory and records it in `ISOLATED_WORKTREE`. Review
and transfer its commit or patch explicitly. Cleanup does not remove a retained
isolated worktree before the run's seven-day retention window.

With `dirty_worktree_policy="allow"`, write delegations and retries run in the
current worktree even when it contains uncommitted changes. The wrapper still
records and reports each run's observed diff, including its safeguards against
executor-initiated reversions.

## Disable, uninstall, and restore

To update an existing installation after changing this repository, run:

```sh
~/.local/bin/cross-harness install
```

Install first verifies the recorded installed hashes and symlink targets. It
backs up the current settings, preserves the personal configuration, and then
updates the managed runtime and user assets in place. If a managed file has
drifted, it lists every changed path and makes no changes; review and merge the
change, or use `install --force` only when overwriting that drift is intended.
Use `install --dry-run` to review the update operations without writing files.
For both `install` and `uninstall`, a deleted managed file is also drift. This
is intentional: use `--force` for the exact managed result, or
`--preserve-user-changes` to remove managed entries while retaining later user
changes.

To stop automatic activation while retaining artifacts, remove or disable the
three cross-harness entries in Claude settings and the Codex recursion hook only
after taking a backup. The supported exact rollback is:

```sh
~/.local/bin/cross-harness uninstall
```

The install manifest checks installed hashes before restoring. If post-install
user edits exist, uninstall stops without changing anything. To surgically
remove managed marker/JSON/TOML entries while keeping later user changes, use
`uninstall --preserve-user-changes`. Use `--force` only when exact restoration
from the recorded backup is explicitly intended. `--purge-runtime` first backs
up run state into the install backup and then removes the default runtime root.
Backups are under the source repository's ignored `.local/backups/` directory
and exclude auth, credentials, logs, transcripts, `.env`, keys, and
certificates.

After rollback, compare the home setting files with the backup manifest, run
`codex login status` and `claude auth status`, then restart both clients.

Do not pipe cross-harness wrapper commands or declaration-check commands into
other commands. A pipeline reports its final command's exit code, so it is not
accepted as check evidence; for wrapper calls, the piped argument text is also
scanned as an executor-launch pattern and can reject an otherwise valid task.

## Codex hook trust

User hooks are not trusted automatically. In a Codex interactive session, open
`/hooks`, verify the command resolves to `~/.local/bin/cross-harness hook
codex-pre-tool-use`, and trust it. Repeat after the hook definition changes.
Until this is done, the environment marker and executor charter remain active,
but the hook layer is not counted as verified.

After `/hooks` shows the exact command as `Trusted`, record that reviewed hash
and rerun diagnostics:

```sh
~/.local/bin/cross-harness trust codex-hook --confirmed-after-review
~/.local/bin/cross-harness doctor
```

Changing the managed hook invalidates the receipt and requires review again.

## Two-week observation record

For each production task, record date, task type, run directory, selected role
and model, retry count, first/final check result, human corrections, rate-limit
events, accidental billing evidence, destructive events, and final success.
Use `docs/observation-log.md` as the durable record format.
Keep the default routing unchanged until five representative task pairs and two
weeks of incident-free observation exist. Major incidents are accidental API
billing, loss/mixing of user changes, or an unbounded launch/retry loop; any one
immediately disables automatic activation.
