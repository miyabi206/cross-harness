# Benchmark results

Status: execution in progress after explicit approval for private-repository transmission.

Repository `chofurenairengo/renai` was resolved from the supplied checkout.
GitHub reports all five source issues as completed, and local history contains
the exact pre-implementation commits. `benchmarks/tasks.json` validates against
the checkout.

| Task type | Completed issue | Start commit |
|---|---|---|
| Small fix | `#526` CPU利用量に制限を設ける | `cfaf55d94b697f6a00ca966822f596e2221f16fc` |
| Medium feature | `#579` お知らせページ作成 | `96224bf562abfa295c950dd4a242d4ac3f58bc4a` |
| Test addition | `#346` テストも実localdbを使うようにする | `eb1fa0e647d2565786f6cfddf9007226d20b85f0` |
| Bug debug | `#538` generateで(笑)をショウって読み上げてしまう | `6ae016320eecb3521615d4601b14b419433fee36` |
| Cross-cutting refactor | `#531` simulationをVRM/動画アバターで分離 | `afa62b56dc3242d52e43c66d56da4360b45b9111` |

Ten clean detached worktrees and lockfile environments are prepared under
`/private/tmp/cross-harness-benchmark-20260717`. A disposable PostgreSQL 16
database was used on `127.0.0.1:55432` without an existing RenAI volume or
database, then stopped and auto-removed during the subscription-limit pause.
Collector tests pass, all ten HEAD values match the manifest, and
representative ML/server/web preflight checks pass.

Explicit approval to transmit task-relevant private source, diffs, and test
output to Anthropic and OpenAI was received on 2026-07-17. Four of ten runs are
complete: both `small_fix` runs and both `bug_debug` runs. All four independent
check suites pass and all four diffs pass the manual `done_when` audit. The
`bug_debug` cross-harness record nevertheless remains an overall failure because
Claude hit its subscription session limit before returning a normal completion;
that limit resets at 18:30 JST. No human correction or result substitution was
made. The final comparison table, routing proposal, and `benchmarks/records.json`
will be generated after the remaining six runs finish.
