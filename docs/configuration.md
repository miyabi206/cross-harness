# Configuration reference

The personal configuration is installed at `~/.config/cross-harness/config.toml`.
It is validated before every delegation. Missing values, unknown keys, invalid
enums, and unsafe limits fail closed. The machine-readable structural contract
is in `schema/harness.schema.json`; the executable validation is implemented in
`cross_harness.config` because the source format is TOML.
`tests/test_schema_contract.py` checks that the schema and executable validation
remain aligned.

## Ownership

- Personal-only: role harness, model, effort, concurrency, retry, timeout,
  write capability, output limit, parent harness, authentication, and fallback
  chain.
- Project-strengthened: checks, prohibited operations, and quality criteria.
- Non-overridable safety: API-key rejection, ChatGPT authentication proof,
  recursion guard, secret-file exclusion, and prohibition of
  `danger-full-access`.

`projects."/absolute/path"` may set only `checks`, `delegate_kinds`,
`dirty_worktree_policy`, `mode`, `project_auto_setup`, `auto_commit`, `auto_revival`, and
`protected_branches`. It cannot select a
model, authentication method, or sandbox. The most specific matching project
path wins.

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

## Usage-limit reset times

`auto_revival` is a boolean defaulting to `true`: when a delegated run is blocked
by a usage limit, allow it to be continued after the recorded reset time. It may
be overridden per project; the closest matching project's value wins, falling
back to the global value if omitted. Set it to `false` to opt out. This unit
adds the configuration switch and reset-time extraction; automatic continuation
is added in the next unit.

`parse_events` exposes `rate_limit_resets_at` as an ISO 8601 timestamp in the
local timezone with a UTC offset, or `None` if no time can be derived. Rejected
Claude rate-limit events supply epoch seconds in `resetsAt`; events allowing
overage in use are ignored. Codex `error` and `turn.failed` messages may say
`try again at 3:01 AM`, `try again at Oct 12th, 2026 9:00 AM`, or
`try again in 3 hours 2 minutes` (also days or a single unit). Matching ignores
case and accepts a trailing period. A clock time without a date means its next
local occurrence after the reference time; `parse_events` accepts a
`reference_time` datetime, defaulting to the current time. `in less than a
minute` uses a conservative one-minute delay.

## Automatic commits and work branches

`auto_commit` is a boolean defaulting to `true`: each successful write delegation
with a passing declared check becomes one unit commit. Write tasks supply a
one-line `--commit-message` in the repository's commit style; missing passing
check evidence makes the run partial and skips the commit. Runs with no changes
skip committing too. Fresh runs exclude all pre-existing dirty paths from
the commit, even if the executor modifies them. Retry, reply and escalation
may commit baseline paths changed by earlier runs of the same chain only.
Unrelated staged changes are preserved. Setting `auto_commit`
to `false` disables automatic branch creation, commits, and isolated integration;
isolated results remain pending for explicit resolution. `protected_branches` defaults
to `["main", "master"]` and lists branches that never receive automatic commits.
It accepts a unique array of literal short branch names, including an empty
array. Entries cannot contain `*`, `?`, square brackets, whitespace, or start
with `refs/`. Setting the key replaces the default list: repeat `main` and
`master` when adding other protected branches.
Both settings may be overridden per project; the closest matching project's
value wins over the global value. If that project omits a setting, the global
value applies.

`work_branch_prefix` defaults to `"cross-harness/"` and names the branch created
when a root write delegation starts on a protected branch or detached HEAD.
An existing unprotected branch is reused. The wrapper never pushes or merges
into a protected branch; the user merges the work branch. The prefix is global
only and must be a non-empty string ending in `/`. Each slash-separated component must
be non-empty, contain only ASCII letters, digits, `.`, `_`, or `-`, start with
neither `.` nor `-`, end with neither `.` nor `.lock`, and contain no `..`.
Older personal configurations inherit all three defaults without being rewritten;
the inherited keys appear in the defaulted paths.

Concurrent writers after the root writer use detached isolated worktrees with
tracked files only, except under `stop`, which blocks them; `isolate` isolates
every writer. Plan single-commit units with exact, disjoint paths and executable
checks, assigning shared files to one unit or a later sequential unit. Name any
untracked setup command in the task or run that unit sequentially.
The root is the launching worktree, including a linked worktree; project
settings resolve against its path. Successful isolated unit commits are
cherry-picked automatically under that worktree's root lock. Integration refuses
staged root changes, unfinished Git operations, and
unit-path collisions with dirty or ignored root paths (including parent/child
paths), while preserving unrelated unstaged changes.

The summary records `commit`, `integration`, and `pending`. Integration statuses
are `integrated`, `conflict`, `failed`, and `pending`; conflicts and failures
retain the isolated worktree for resolution. `INTEGRATED` marks automatic or
explicit committed integration, `ADOPTED` marks file-by-file adoption of
uncommitted changes, and `DISCARDED` marks an abandoned isolated worktree.
Use `cross-harness adopt --run <run_dir>` after removing an integration failure's
cause (including conflicts), `cross-harness discard --run <run_dir>` for
abandoned work, and `cross-harness commit --run <run_dir>` for a partial root
or isolated run verified another way. Failed isolated units require retry or
discard. Already integrated runs use adopt or discard only to finish cleanup.
`cross-harness pending --cwd <repo>` lists finished isolated runs whose
worktrees still exist; resolve them before the next wave, tester, reviewer, or
report. SessionStart lists these runs for the current repository and stays
silent about them if there are none or collection fails.

## Enforcement mode

`mode` is `"on"` or `"off"` and defaults fail closed to `"on"`. It may be set
globally or in a project override, where the project value takes precedence.
`"off"` disables enforcement; set a project value to `"on"` to opt back in.

`project_auto_setup` defaults to `true`. At Claude SessionStart, the hook adds
the VS Code delegation task only when `.vscode/tasks.json` does not exist. Set
it to `false` globally or in a project override to disable automatic setup;
`cross-harness project remove --cwd /path/to/repository` also disables it for
that repository until `cross-harness project setup` is run again.

## Roles

Every role has `harness`, `model`, `effort`, `max_parallel`, `retries`,
`timeout_seconds`, `write`, `output_limit_chars`, and `delegate_kinds`.
The top-level `max_discussion_rounds` defaults to 3 and accepts integers from 0
to 10. Discussion replies preserve attempts and do not consume the normal retry
budget or auto-escalate the model. Once the round count reaches this limit,
`reply` requires `--user-decided`; 0 requires it for every reply. The flag skips
only the limit check and still increments `discussion_rounds`. Older personal
configurations inherit 3 through default merging without being rewritten.
The top-level `max_parallel` is an enforced runtime limit across all delegated
runs and defaults to 4. Role limits default to 3 for explorer and implementer,
2 for reviewer, and 1 for every other role. Global and role limits accept
integers from 1 to 5. Each non-orchestrator role's `max_parallel` is also enforced separately;
when either limit is full, the delegation is recorded as blocked immediately
without waiting or queueing.
Parallel capacity is counted from non-terminal runs whose supervisor is alive; an executor left alive after its supervisor exits does not consume capacity. PID identity is not verified, so a reused PID may be conservatively counted until the run is marked `ORPHANED` or exceeds `retention_days`.
The required roles are orchestrator, explorer, implementer, implementer_complex,
tester, reviewer, debugger, and security_reviewer. `implementer` handles changes
that can be split into independently verifiable units, while
`implementer_complex` handles unsplittable changes or changes involving
judgment or policy.

The checked-in defaults implement plan section 6. Codex uses the explicit
model IDs `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna`; Claude uses the
personal aliases from the plan. Change these only in the personal file.
The read-only `security_reviewer` may perform a `review` without high-risk
confirmation; `security_review` still requires `--confirm-high-risk`.

The implementer effort and global and per-role parallel limits expanded into
the orchestrator `SKILL.md` are fixed at install time. The role list includes
every non-orchestrator role in configuration order. Changing the configuration
alone leaves installed values unchanged; run install again to render new values.

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
