# E2E verification matrix

`scripts/e2e.sh` runs the deterministic suite with a temporary HOME, a temporary
Git repository, and a fake Codex process boundary. It does not consume model
quota. Live-only checks are deliberately separate.

| # | Plan check | Automated evidence | Live/manual status |
|---:|---|---|---|
| 1 | Normal startup loads assets | install/settings round-trip and SessionStart tests | **Pass:** authenticated natural-language session loaded the installed assets |
| 2 | Natural request delegates | runner produces one run record | **Pass:** synthetic edit produced run `20260717T153718-1d5b27c0` without a dedicated user command |
| 3 | ChatGPT subscription use | auth parser accepts only ChatGPT | **Known manual limit:** CLI reports ChatGPT auth; plan dashboard delta was not captured |
| 4 | No API billing path | API-key/provider/base-URL rejection tests | **Known manual limit:** fail-closed path passes; provider billing dashboards require user inspection |
| 5 | Personal model/effort | config, asset, and generated command tests | **Pass:** live runs recorded `gpt-5.6-terra/high`, `claude-fable-5`, `claude-haiku-4-5`, and `claude-sonnet-5` as routed |
| 6 | Ownership precedence | project schema rejects model ownership | **Pass:** executable validation rejects project model ownership |
| 7 | No unnecessary agents | small fake run asserts one invocation | **Pass:** small live edit used one Codex run and no Claude subagent |
| 8 | No recursive launch | Claude/Codex hook and environment-marker tests | **Pass:** all guards pass tests; exact Codex hook was reviewed and trusted in `/hooks`. Adversarial model run remains a documented manual limit |
| 9 | Huge log isolation | bounded deterministic summary test | **Pass:** raw artifacts stay in the run directory and parent output is bounded |
| 10 | Failure facts retained | event, turn failure, and signature tests | **Pass:** injected failures retain exit/cause and raw artifact paths |
| 11 | Diff/check verification | orchestrator contract and live successful verification | **Known manual limit:** deliberately incomplete live result/retry has not been run |
| 12 | Existing settings survive | exact and surgical install/uninstall tests | **Pass:** live rollback restored pre-install hashes; reinstall preserved Codex-added trust settings |
| 13 | Interrupt recovery | timeout, interrupted marker, cleanup, and orphan tests | **Known manual limit:** forced interruption of a paid live run was not performed |
| 14 | Dirty changes protected | stop and isolated-worktree tests | **Pass:** both policies preserve the original changes and baseline diff accounting |

The status report in `docs/e2e-results.md` and the detailed record in
`docs/live-verification.md` distinguish deterministic passes from live checks
that require dashboard or deliberately disruptive evidence. A manual
limitation is never reported as an automated pass.
