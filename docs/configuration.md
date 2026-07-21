# Configuration reference

The personal configuration is installed at `~/.config/cross-harness/config.toml`.
It is validated before every delegation. Missing values, unknown keys, invalid
enums, and unsafe limits fail closed. The machine-readable structural contract
is in `schema/harness.schema.json`; the executable validation is implemented in
`cross_harness.config` because the source format is TOML.

## Ownership

- Personal-only: role harness, model, effort, concurrency, retry, timeout,
  write capability, output limit, parent harness, authentication, and fallback
  chain.
- Project-strengthened: checks, prohibited operations, and quality criteria.
- Non-overridable safety: API-key rejection, ChatGPT authentication proof,
  recursion guard, secret-file exclusion, and prohibition of
  `danger-full-access`.

`projects."/absolute/path"` may set only `checks`, `delegate_kinds`, and
`dirty_worktree_policy`. It cannot select a model, authentication method, or
sandbox. The most specific matching project path wins.

## Dirty worktrees

`dirty_worktree_policy` defaults to `"stop"`, which blocks write roles when
the repository has uncommitted changes. `"isolate"` runs the write role in a
detached worktree instead. `"allow_delegated"` is an explicit opt-in that
permits only unchanged dirty files recorded by a previous write delegation;
any unrecorded, deleted, un-fingerprintable, or modified file still blocks the
run. It may be set globally or in a project override. Do not edit the working
tree while a delegated write run is executing: a concurrent user edit can be
observed as that run's delta and recorded as delegated, so this policy depends
on that operational discipline.

## Roles

Every role has `harness`, `model`, `effort`, `max_parallel`, `retries`,
`timeout_seconds`, `write`, `output_limit_chars`, and `delegate_kinds`.
The required roles are orchestrator, explorer, planner, implementer, tester,
reviewer, debugger, and security_reviewer.

The checked-in defaults implement plan section 6. Codex uses the explicit
model IDs `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna`; Claude uses the
personal aliases from the plan. Change these only in the personal file.

## Fallback and escalation

Fallback stays inside the same subscription harness. Rate limits never trigger
fallback. Retries are capped at two; two identical failure signatures stop the
normal retry loop and permit one explicit escalation. There is no API-provider
fallback.

## Context and retention

The default session migration threshold is 70 percent. Runtime artifacts are
kept for seven days. Authentication results may be cached for at most the
current day and never longer than `auth_cache_hours`.
