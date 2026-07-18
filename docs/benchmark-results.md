# Benchmark results

Status: stopped on 2026-07-18 as a documented measurement limitation. This is
not a nearly complete 5×2 benchmark and it does not produce an aggregation or
routing recommendation. Only one of the five task types has a usable baseline/
cross-harness comparison pair.

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

The benchmark used Claude fable as its parent model, whereas the installed
orchestrator setting is opus. This configuration mismatch is a further reason
the records cannot support a routing recommendation.

## Run validity

The nine executed benchmark records are listed below. "Valid" for a single run
means that its automatic result and manual `done_when` audit support that run;
it does not turn an unpaired result into a comparison. The only usable pair is
`small_fix`.

| Task type | Configuration | Validity | Audit outcome |
|---|---|---|---|
| `small_fix` | baseline | valid comparison member | automatic success; manual audit passed |
| `small_fix` | cross_harness | valid comparison member | automatic success; manual audit passed |
| `bug_debug` | baseline | valid but unpaired | automatic success; manual audit passed |
| `bug_debug` | cross_harness | invalid comparison member | acceptance criteria passed, but Claude was truncated by a subscription session limit before normal completion (`automatic_task_success=false`) |
| `medium_feature` | baseline | valid but unpaired | automatic success; manual audit passed |
| `medium_feature` | cross_harness | invalid comparison member | background delegation was killed when the headless parent turn ended; no task changes were made |
| `test_addition` | baseline | valid but unpaired | automatic success; manual audit passed |
| `test_addition` | cross_harness | invalid comparison member | the same background-delegation-kill defect left the worktree unchanged; passing checks were only the untouched baseline suite |
| `cross_refactor` | baseline | invalid/incomplete | Claude hit a subscription session limit mid-implementation; typecheck and build failed on the partial refactor |

`cross_refactor`/`cross_harness` was never started. The discarded
`test_addition` baseline attempt stored as
`baseline.session-limited-attempt1` is not one of the nine executed benchmark
records; its incomplete artifact is retained under `.local`.

## Failure causes and delivered value

The audit files identify three distinct failure causes:

1. `bug_debug`/`cross_harness`: a Claude subscription session limit occurred
   before normal completion, despite an acceptance-criteria-passing diff.
2. `medium_feature`/`cross_harness` and `test_addition`/`cross_harness`: the
   background-delegation-kill defect terminated the wrapper and executor when
   the headless `-p` parent turn exited, measuring an orchestrator defect rather
   than the harness.
3. `cross_refactor`/`baseline`: a Claude subscription session limit interrupted
   implementation, leaving an invalid partial refactor.

The benchmark's delivered value was discovery of the
background-delegation-kill defect. It was fixed on 2026-07-18 in commit
`5472f0c`, which introduced the detached supervisor and the skill rule against
background delegation; `98cdfa7` later repaired that supervisor's package
import through the installed wrapper; the defect records are retained as defect evidence, not harness
measurements. No human correction or result substitution was made.

Tasks T01 through T13, including the fourteen-item end-to-end evidence matrix,
pass independently of this stopped measurement. Effect measurement now moves
to `docs/observation-log.md`: its two-week production-task observation avoids
additional subscription-quota cost and supplies the evidence for any future
routing adjustment.
