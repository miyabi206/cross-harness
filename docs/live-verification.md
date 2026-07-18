# Live verification record

Generated: 2026-07-17.

## T01 inventory and backup

- Historical pre-install inventory: `docs/inventory.md`.
- Credential-excluding backup: ignored local path
  `.local/backups/T01-pre-install/` with a manifest.
- Credential files, auth contents, transcripts, logs, `.env`, keys, and
  certificates were not copied.

## T02 independent Codex and subscription smoke

- Independent npm Codex: `/opt/homebrew/bin/codex`, version 0.144.5.
- `codex login status`: authenticated with ChatGPT; no API-key environment.
- Synthetic read-only Git repository under `/private/tmp` completed with
  `gpt-5.6-luna/low`, read-only sandbox, structured output, and no file changes.
- The smoke exposed and fixed two compatibility cases: strict-schema fields
  require explicit JSON types, and a `turn.failed` event overrides process exit
  zero. Both have regression tests.

The ChatGPT plan dashboard delta and provider billing dashboards remain manual
checks. The executable path rejects API keys, provider overrides, custom base
URLs, unknown auth, and rate-limit fallback before or during execution.

## Home activation, rollback, and diagnostics

- The user explicitly approved persistent home installation and
  hooks/permissions changes.
- Final installer backup: `.local/backups/20260717T072244Z`.
- An exact uninstall restored 17 paths and reproduced the backed-up hashes for
  Claude settings and Codex config, followed by a successful reinstall.
- Surgical uninstall/reinstall preserved a Codex-added project trust table that
  appeared after installation. The managed TOML block now stays at the top of
  `config.toml`, preventing later Codex tables from being captured inside it.
- Installed Claude commands use the exact absolute wrapper path. The natural
  E2E reported no permission denial for `task`, `delegate`, or `retry`.
- The Codex `/hooks` browser showed the installed user `PreToolUse` command as
  `/Users/itoutaisei/.local/bin/cross-harness hook codex-pre-tool-use`; it was
  reviewed and set to `Trusted`. Reinstall retained native trust. The local
  review receipt matches definition hash prefix `e33444418bee`.
- Final `cross-harness doctor`: all eight checks pass (configuration,
  independent CLI, API-key environment, Codex ChatGPT auth, Claude auth, both
  charters, and Codex hook trust receipt).

## Natural-language Claude to Codex E2E

Synthetic repository: `/private/tmp/cross-harness-claude-e2e.wt4x35`.

The only user prompt asked in Japanese to append `after` to `README.md` and run
`git diff --check`; it did not mention cross-harness. Claude loaded the
orchestrator skill, created a screened task via the absolute wrapper, delegated
once, inspected the result, and reported completion.

- Successful run: `20260717T153718-1d5b27c0`.
- Codex role/model/effort: `implementer`, `gpt-5.6-terra`, `high`.
- Delta: `README.md` only, `+1/-0`.
- Verification: `git diff --check` passed and the final line was `after`.
- Run artifacts include task, command, baseline, full JSONL/stderr/final
  output, bounded summaries, diff statistics, and state.
- Claude permission denials: one direct `git` verification command; wrapper
  task/delegate/retry denials: zero. Claude recovered with read-only inspection
  and its final report matched the actual diff.

## Claude role definitions

The installed user agents were invoked directly against the same synthetic
repository without override JSON:

- `cross-harness-explorer` completed a bounded path/header lookup with
  `claude-haiku-4-5-20251001`, three turns, no changes, and zero permission
  denials.
- `cross-harness-reviewer` reviewed only the existing README diff with
  `claude-sonnet-5`, three turns, no changes, and zero permission denials.

The checked-in definitions and tests additionally verify `haiku/low`,
`sonnet/high`, the allowed tool sets, non-edit/non-delegation rules, and the
explorer's three-cycle retrieval limit.

## E2E and live benchmark

- `scripts/e2e.sh`: 56 tests pass, configuration valid.
- The 14 plan checks are marked pass or known manual limitation in
  `docs/e2e.md`; dashboard evidence and deliberately disruptive live cases are
  not mislabeled as automated passes.
- T14 uses the supplied `/Users/itoutaisei/renai` checkout and completed issues
  #526, #579, #346, #538, and #531. Ten matching clean worktrees, lockfile
  environments, a disposable PostgreSQL database, and the tested stream
  collector are ready. Explicit private-repository transmission approval was
  received on 2026-07-17. Both `small_fix` runs and both `bug_debug` runs are
  complete; all four independent check suites and manual `done_when` audits
  pass. The `bug_debug` cross-harness run is still recorded as an overall
  failure because Claude reached its subscription session limit before normal
  completion. The remaining six runs resume after the 18:30 JST reset.
