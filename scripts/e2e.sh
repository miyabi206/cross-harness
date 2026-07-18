#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if scripts/test.sh; then
  result=PASS
else
  result=FAIL
fi
codex_version=$(codex --version 2>/dev/null || true)
claude_version=$(claude --version 2>/dev/null || true)
claude_status=$(claude auth status 2>/dev/null || true)
case $(printf '%s' "$claude_status" | tr -d ' ') in
  *'"loggedIn":true'*) claude_auth=authenticated ;;
  *) claude_auth='not verified by this run' ;;
esac

{
  echo '# E2E results'
  echo
  echo "Generated: $started"
  echo
  echo "Deterministic suite: **$result**"
  echo
  echo "- Codex: ${codex_version:-unavailable}"
  echo "- Claude Code: ${claude_version:-unavailable}"
  echo '- Automated coverage: temporary-HOME installation/rollback, schema, auth/provider guards, recursion hooks, bounded summaries, run artifacts, cleanup, and dirty-worktree protection.'
  echo "- Claude authentication during this run: $claude_auth."
  echo '- Live evidence: natural-language routing, installed wrapper execution, rollback/reinstall, and Codex hook review are recorded in `docs/live-verification.md`.'
  echo '- Known manual limitations: ChatGPT plan dashboard deltas, provider billing dashboards, incomplete-result orchestration, adversarial live recursion, and forced interactive interruption.'
} > docs/e2e-results.md

test "$result" = PASS
