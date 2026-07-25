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

`dirty_worktree_policy` defaults to `"allow_delegated"`, which permits a write
role to continue only when every dirty file is unchanged and recorded by a
previous write delegation. `"stop"` blocks write roles when the repository has
uncommitted changes, and `"isolate"` runs the write role in a detached worktree
instead. `"allow"` runs write delegations and retries in the current worktree
regardless of pre-existing changes or their recorded provenance. It may be set
globally or in a project override. Do not edit the working tree while a
delegated write run is executing: a concurrent user edit can be observed as
that run's delta and recorded as delegated, so this policy depends on that
operational discipline.

## Roles

Every role has `harness`, `model`, `effort`, `max_parallel`, `retries`,
`timeout_seconds`, `write`, `output_limit_chars`, and `delegate_kinds`.
The required roles are orchestrator, explorer, implementer, tester, reviewer,
debugger, and security_reviewer.

The checked-in defaults implement plan section 6. Codex uses the explicit
model IDs `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna`; Claude uses the
personal aliases from the plan. Change these only in the personal file.
The read-only `security_reviewer` may perform a `review` without high-risk
confirmation; `security_review` still requires `--confirm-high-risk`.

## Orchestrator direct-edit scope

Claude's orchestrator hook permits `Edit` and `Write` under the Git repository
root resolved by walking upward from the hook's absolute, existing `cwd`. It
fails closed when that `cwd` is invalid or no Git root is found, resolves target
paths before checking them to prevent symlink escapes, and never permits paths
under the repository's `.git` directory. Claude plan files and per-project
memory files under `~/.claude` remain permitted. This does not alter the
read-only tools or the write scope of delegated executors.

## Fallback and escalation

Fallback stays inside the same subscription harness. Rate limits never trigger
fallback. Retries are capped at two; two identical failure signatures stop the
normal retry loop and permit one explicit escalation. There is no API-provider
fallback. A run explicitly blocked by its executor (`blocked_category` of
`executor_reported`) may be retried. Authentication and rate-limit blocks are
safety-policy stops and cannot be retried. Dirty-worktree and missing-isolated-
worktree blocks have no reusable result; create a new delegation instead.

## Context and retention

The default session migration threshold is 70 percent. Runtime artifacts are
kept for seven days. Authentication results may be cached for at most the
current day and never longer than `auth_cache_hours`.
