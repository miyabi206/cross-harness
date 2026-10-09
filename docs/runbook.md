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
   Resolve any finished isolated runs listed at SessionStart before reporting;
   the reminder is silent when none remain and collection failures do not block
   the session.
4. At each phase boundary, migrate to a new session when context use reaches
   the configured threshold (70 percent by default).

## Unit commits and parallel waves

Split a change into independently verifiable single-commit units with exact
Scope paths and an executable check. Delegate independent units in parallel
waves with disjoint paths; dependent units run in order. Assign shared files
(lockfiles, registries, schemas, generated files and configuration) to one unit
or a later sequential unit. Respect both global and per-role parallel limits.
Pass a one-line `--commit-message` in the repository's commit style on every
write task. A write run without a passing declared check is partial and is not
committed.

The first writer uses the root worktree; concurrent writers use isolated
worktrees containing tracked files only. Under `stop`, writers run sequentially;
under `isolate`, every writer is isolated. Name dependency installation or other
untracked setup in the task if a check needs it, or run the unit sequentially.
With automatic commits enabled, the wrapper first moves a root writer off a
protected branch or detached HEAD to a work branch under `work_branch_prefix`,
then commits each successful write run as one unit. Successful isolated commits
are cherry-picked onto the root work branch under its lock.

Inspect unit commits with `git show --stat <sha>` and `git show <sha>` because
the worktree is clean after committing. Resolve every unit before another wave,
reviewer, tester, or report; tester checks the root and must never run alongside
a writer. Report the work branch, each unit commit sha and subject, verification,
and unresolved items. Merging the work branch is the user's remaining step.
The wrapper never pushes or merges into a protected branch. Do not commit,
merge, rebase, push, delete branches or run worktree commands by hand for
delegated work. A direct edit is also one unit: after mandatory review and tester
verification, the orchestrator commits it on the work branch itself.

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

The watch header shows the delegated role, harness, model, and effort. Model
and effort are omitted when the run record does not provide usable values.

Codex publishes only a short heading line for each completed reasoning item,
so watch displays that heading with a `✻` marker; reasoning items that have not
completed are not displayed. A delegated result JSON message is rendered as
readable text for `status`, `work_completed`,
`changed_files`, `tests`, `error`, and `next_decision`. Fields whose values are
null or empty are omitted with their entire line.

Watch output is written only to the terminal and is not added to the
orchestrator context. The complete raw event log is in the run directory's
`events.jsonl`. `aggregated_output` from Codex `command_execution` events is
shown, but Claude `tool_result` bodies and error text from `state.json` are not
shown, even with `--all`.

Claude SessionStart automatically creates the folder-open watcher when
`.vscode/tasks.json` is absent in the repository. Existing files are left alone.
To install or update the task manually, run:

```sh
cross-harness project setup --cwd /path/to/repository
```

The task runs the next time VS Code opens that folder, and VS Code must allow
automatic tasks for it. `cross-harness project remove --cwd /path/to/repository`
removes the cross-harness task when possible and disables future automatic
setup for the repository; `project setup` enables it again. Use `--dry-run`
with either command to see the planned change without writing files.

Run `cross-harness doctor` after either CLI upgrades, authentication changes,
hook changes, or a reinstall. Run `cross-harness cleanup` when stale run
directories need immediate maintenance; SessionStart also invokes it.

## Self-update

The install manifest records the repository path and the source commit for
human-facing diagnostics. Runtime drift is determined from the contents of the
managed `bin`, `src`, `config`, `schema`, `schemas`, and `assets` trees; commit
changes alone do not trigger an update, and `__pycache__`/`*.pyc` are ignored.

Check without changing files, preview an update, or apply it explicitly with:

```sh
cross-harness self-update --check
cross-harness self-update --dry-run
cross-harness self-update
```

Installing this repository also adds fail-open Git hooks for post-merge,
post-commit, post-checkout, and post-rewrite. They invoke self-update and
always return success so pull, commit, checkout, and rebase are not blocked.
An active delegated supervisor causes the update to be skipped with a warning.
If a refresh changes the Codex hook definition, review it again in `/hooks`
and record the new receipt with `cross-harness trust codex-hook
--confirmed-after-review`.

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
- a rate or usage limit is reported: stop using that harness across repositories
  until its recorded reset;
- a delegated run attempts to start Claude or another Codex executor;
- a write task finds unrecorded dirty changes under `allow_delegated`, or any
  dirty changes under `stop`;
- two identical failures have already triggered the single escalation run;
- a high-risk task lacks explicit human confirmation.

Do not wait in a loop, buy credits, switch login method, seed an auth file,
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
executor explicitly returned `blocked` can be retried. Authentication blocks
remain safety-policy stops and cannot be resumed. Usage-limit blocks follow
the continuation policy below. A
dirty-worktree or missing-isolated-worktree block has no reusable result, so
create a new delegation instead.

### Delegated usage-limit continuation

Read `rate_limit_resets_at` and `revival` in the blocked run's summary. Stop
delegating to the limited harness before that reset; finish work that does not
need it. When revival permits retry, schedule exactly one one-shot continuation
two to five minutes after reset with the session scheduler `CronCreate`, setting
`recurring=false` and loading it through `ToolSearch` when deferred. The prompt
names the repository, blocked run directory and remaining units in order.
Tell the user what was scheduled, for when, and that the session must stay open,
then end the turn. Waiting in a loop, API billing and external routers remain forbidden.

When it fires, or a later session shows the reminder, run
`~/.local/bin/cross-harness revival --cwd <repo>` to confirm eligibility, then
`~/.local/bin/cross-harness retry --run-dir <run_dir> --task-file <continuation_file>`
and continue the remaining units. Listing prints run directory, role, reset time
and `eligible`, `waiting until <reset time>`, or `not revivable: <reason>`,
separated by tabs. Retry is only for eligible runs and resumes the executor thread
when present and inherits the root, worktree, commit subject and chain paths.
It leaves attempts unchanged and never escalates, and inherits the blocked task's
checks when the continuation declares none. Two consecutive revivals that
hit another usage limit stop the chain; any other outcome clears the count.
If work was continued another way, use
`~/.local/bin/cross-harness revival --dismiss --run <run_dir>`.

If the scheduler is unavailable, reset time is unknown or revival is disabled,
stop and report the reset time (or `unknown`) and the command to continue.
For a not revivable run, use a new delegation of the remaining work after any
known reset, then dismiss the blocked run; the retry command above is only for
eligible runs. The wrapper enforces known future resets per harness account
across repositories for delegate, retry and reply. `auto_revival` defaults to true and may be
overridden per launching root. Claude Code itself waits and continues when the
orchestrator's own claude.ai usage limit resets; this covers delegated runs only.
Closing the session loses its scheduled continuation; the session-start reminder
takes over. Session reminders are best effort and never prevent startup.

Executor results have exactly seven fields: `status`, `work_completed`,
`changed_files`, `tests`, `error`, `next_decision`, and `discussion_points`
(an array of strings). A `discussion` turn is finalized and has no block category.
Answer every point in a delta task file and use
`~/.local/bin/cross-harness reply --run-dir <run_dir> --task-file <reply_file>`;
`retry` refuses these runs. The reply resumes the recorded thread, preserves
attempts, increments `discussion_rounds`, and never auto-escalates the model.
Checks declared in the original task carry over when the reply declares none.
`adopt` refuses discussion runs because they await a reply.
The points and round count are retained in summary and state artifacts, and
printed before summary content that may be truncated. Non-blocking points on
finished runs should be weighed and unresolved points named in the report.
At `max_discussion_rounds` (default 3, range 0 to 10), present both positions to
the user and send the decision with `reply --user-decided`. That flag skips only
the round-limit check, still increments the count, and is recorded in
`command.json` and `summary.json`. A limit of 0 requires it for every reply.

By default, `dirty_worktree_policy="allow_delegated"` lets a write role
continue in a dirty worktree only when every dirty file was recorded by a prior
write delegation and has not changed since. Otherwise, the wrapper stops the
write role so the worktree can be reviewed before continuing.

With `dirty_worktree_policy="isolate"`, the wrapper creates a detached Git
worktree below the run directory and records it in `ISOLATED_WORKTREE`.
Successful unit commits with a recorded `ROOT_WORKTREE` integrate automatically into the launching worktree,
which is the root even when it is a linked worktree. Other worktrees are left
untouched. Legacy isolated runs without that marker remain pending for explicit,
file-by-file `adopt`. Cleanup does not remove a retained isolated worktree before the run's seven-day retention window.

The summary's `commit` line describes the unit commit, `integration` describes
its root integration, and `pending` lists finished isolated runs with remaining
worktrees. Integration statuses mean:

- `integrated`: unit commits reached the root work branch; successful cleanup
  removes the worktree and writes `INTEGRATED`.
- `conflict`: cherry-picking conflicted; the wrapper rolls back and keeps the
  isolated unit. Delegate that unit again sequentially in the root worktree,
  citing the kept unit commit sha so the executor can read it with
  `git show <sha>`, then discard the conflicted run.
- `failed`: another integration error; remove the stated cause and use `adopt`.
- `pending`: integration is outstanding, for example after a root lock timeout
  or an uncommitted unit; for a committed unit, remove the stated cause and use
  `adopt`; for an uncommitted unit, use `adopt` or retry as appropriate.

Integration refuses any staged root changes or unfinished Git operation. Dirty
and ignored paths must not collide with any unit path, including parent/child
paths; unrelated unstaged changes are preserved. This collision rule is why
parallel units must have disjoint paths. Conflicts and failures retain the
isolated worktree; successful integration can also leave a cleanup warning
and a pending worktree that still needs resolving.

```sh
~/.local/bin/cross-harness adopt --run <run_dir>
~/.local/bin/cross-harness discard --run <run_dir>
~/.local/bin/cross-harness commit --run <partial_run_dir>
~/.local/bin/cross-harness pending --cwd /path/to/repository
```

`adopt` retries committed integration under the root lock; it refuses a live
run or a discussion awaiting a reply. For an uncommitted isolated result, it
requires matching root and isolated HEADs and checks for file conflicts before
copying changes, then writes `ADOPTED`. `discard` removes an abandoned isolated
worktree and writes `DISCARDED`. Resolution also updates runs sharing the
worktree through a retry chain. A committed unit whose integration failed or
is pending is resolved by removing the stated cause and using `adopt`.
A committed unit whose integration conflicted is resolved by delegating that
unit again sequentially in the root worktree, citing the kept unit commit sha
so the executor can read it with `git show <sha>`, then discarding the conflicted
run.
For a partial unit verified another way, root or isolated, use `commit`; it
checks recorded file fingerprints, commits the unit, and integrates it if
isolated. A failed isolated unit is resolved by retry or discard. Adopting or
discarding an already integrated unit only completes cleanup and retains the
integrated record; removal requires a clean worktree at the integrated unit's
commit. The last summary must say `pending: none`, or `pending` must
return no entries, before reporting. Usage-limit blocked isolated units awaiting
revival are the exception: they appear only in `revival` and are excluded from
`pending` and its session reminder.

With `dirty_worktree_policy="allow"`, write delegations and retries run in the
current worktree even when it contains uncommitted changes. The wrapper still
records and reports each run's observed diff, including its safeguards against
executor-initiated reversions.

## Disable, uninstall, and restore

To update an existing installation after changing this repository, run:

```sh
./bin/cross-harness install
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
from the recorded backup is explicitly intended. As with personal settings,
the codex_config-managed file (`~/.codex/config.toml`) is never fully restored
from backup by default, `--force`, or `--preserve-user-changes`; those modes
only remove its cross-harness managed marker block. `--purge-runtime` first backs
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
