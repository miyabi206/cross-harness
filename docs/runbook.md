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

Run `cross-harness doctor` after either CLI upgrades, authentication changes,
hook changes, or a reinstall. Run `cross-harness cleanup` when stale run
directories need immediate maintenance; SessionStart also invokes it.

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

For a correctable failure, write only the changed instruction into a new task
file and resume from the failed run. The normal budget is two retries. When the
same normalized signature occurs twice, the wrapper performs one escalation
(Luna→Terra→Sol and/or one effort step) and marks `escalated=true`. A blocked
auth/rate-limit run cannot be resumed automatically.

With `dirty_worktree_policy="isolate"`, the wrapper creates a detached Git
worktree below the run directory and records it in `ISOLATED_WORKTREE`. Review
and transfer its commit or patch explicitly. Cleanup does not remove a retained
isolated worktree before the run's seven-day retention window.

## Disable, uninstall, and restore

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
