# Cross Harness

Personal, fail-closed orchestration from Claude Code to Codex CLI. The supported
topology is Claude as the orchestrator and Codex as the executor: Claude keeps
requirements, planning, review, testing, and reporting, while Codex performs
bounded implementation, debugging, and security-review work through one wrapper.
Every delegated role has a `harness` setting and can be placed on either
harness, but this does not make Codex an orchestrator. The current defaults put
explorer, reviewer, and tester on Claude; implementer, debugger, and
security_reviewer on Codex.

The harness uses saved ChatGPT subscription authentication only. It rejects any
`*_API_KEY` environment variable, non-OpenAI provider override, custom OpenAI
base URL, unknown authentication state, write delegation over an existing dirty
worktree, rate-limit fallback, and exhausted retries. It blocks recursive
delegation through its wrapper and blocks direct launch commands matched by its
PreToolUse hook; the hook cannot inspect indirect launches through scripts or
Makefiles. SessionStart rejection is notification only, not a blocking boundary.

## Prerequisites

- macOS, Git, Python 3.11 or newer, and Claude Code.
- Independent Codex CLI installed with `npm install --global @openai/codex`.
- `codex login status` must report ChatGPT authentication.
- `claude auth status` must report an authenticated subscription session.

Credential files are never read, copied, backed up, or logged. Only CLI status
commands and the existence of `~/.codex/auth.json` are inspected.

## Install

Run the non-mutating checks first:

```sh
./bin/cross-harness validate --config config/default.toml
./bin/cross-harness inventory --output docs/inventory.md --backup .local/backups/pre-install
./bin/cross-harness install --dry-run
```

Then install through the repository-owned installer:

```sh
./bin/cross-harness install
~/.local/bin/cross-harness doctor
```

The installer backs up non-credential settings, copies its runtime under
`~/.local/share/cross-harness/current`, installs the personal configuration at
`~/.config/cross-harness/config.toml`, and merges user assets. Markdown and TOML
additions use visible marker blocks. JSON hooks and permissions are merged as
structured entries; the install manifest preserves their exact prior content.

Codex requires one manual trust action for non-managed hooks: open Codex, run
`/hooks`, inspect the user-level recursion guard, and trust its exact definition.
This is a native Codex security boundary and is not bypassed by the installer.
Then bind diagnostics to that reviewed definition hash:

```sh
~/.local/bin/cross-harness trust codex-hook --confirmed-after-review
~/.local/bin/cross-harness doctor
```

## Delegate

The orchestrator writes a task file and invokes:

```sh
~/.local/bin/cross-harness delegate \
  --role implementer \
  --kind implementation \
  --task-file /absolute/path/task.md \
  --cwd /absolute/path/repository
```

The wrapper returns only a bounded summary and artifact paths. Raw JSONL,
stderr, the structured final response, diff statistics, and retry state stay in
`~/.local/state/cross-harness/runs/<run-id>/`. A correction uses a delta-only
task file with `~/.local/bin/cross-harness retry --run-dir ... --task-file ...`.

## Change role settings

Edit `~/.config/cross-harness/config.toml`. The repository's
`config/default.toml` is a template copied only during first installation;
editing it does not change the running configuration.

Each role has `harness`, `model`, and `effort` settings. `harness` selects
Claude or Codex as the execution target, and any role can be assigned to either
harness. The supported parent remains Claude; setting a role to Codex changes
only that role's execution target. `model` accepts any string without
validation. `effort` is also passed through without rejection; `validate` only
warns when it is outside the known values: Codex supports `minimal`, `low`,
`medium`, `high`, and `xhigh`; Claude supports `low`, `medium`, `high`, `xhigh`,
and `max`.

Codex role changes apply to the next `delegate`. Claude role changes require a
new Claude session, because its session-start hook synchronizes
`~/.claude/agents/cross-harness-*.md`. `orchestrator` is the session itself,
so its `model` and `effort` settings currently have no effect.

Check the configuration with:

```sh
cross-harness validate --config ~/.config/cross-harness/config.toml
```

## Verify and remove

```sh
scripts/test.sh
scripts/e2e.sh
~/.local/bin/cross-harness uninstall
```

Uninstall refuses to overwrite files changed after installation. Review the
drift and merge it first; use `--preserve-user-changes` for surgical managed
entry removal, and `--force` only when exact restoration of the recorded backup
is explicitly desired. See [the runbook](docs/runbook.md) for stop, recovery,
isolation, hook trust, and observation procedures.
