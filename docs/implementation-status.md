# Plan section 14 implementation status

| Task | Implemented artifact | Verification/status |
|---|---|---|
| T01 | inventory and credential-excluding backup engine | **Pass:** live inventory plus `.local/backups/T01-pre-install/` manifest exist |
| T02 | independent Codex CLI prerequisite and live smoke | **Pass:** npm Codex 0.144.5, ChatGPT auth, synthetic read-only run |
| T03 | installer, manifest backup, dry-run, exact/surgical uninstall | **Pass:** temporary-HOME tests and live rollback/reinstall; Codex-added settings preserved |
| T04 | default TOML, schema, strict executable validator | **Pass:** all eight roles and unknown/missing/enum/range/ownership tests |
| T05 | fail-closed delegation core | **Pass:** auth/provider/API-key/dirty-tree guards plus live read/write delegation |
| T06 | per-run raw artifacts, bounded deterministic summary, cleanup | **Pass:** output/baseline/retention/orphan tests and live artifacts |
| T07 | resume budget, normalized signatures, one escalation, auth/rate stop | **Pass:** retry/escalation/timeout/blocked-state tests |
| T08 | Codex charter, ChatGPT config merge, custom agents | **Pass:** active in home; pre-existing and later Codex config values preserved |
| T09 | environment marker, Codex hook, executor charter | **Pass:** adversarial tests; exact native hook reviewed and trusted, hash receipt current |
| T10 | short Claude charter and orchestrator skill | **Pass:** natural-language synthetic edit routed without a dedicated user command |
| T11 | Claude explorer/reviewer definitions | **Pass:** installed roles launched natively as Haiku 4.5 and Sonnet 5 with zero permission denials; model/effort/tool/retrieval bounds are tested |
| T12 | SessionStart, Stop, PreToolUse, structured permissions merge | **Pass:** active authenticated session; wrapper task/delegate permission denials were zero |
| T13 | reproducible suite and 14-item evidence matrix | **Pass/known limits:** 56 deterministic tests pass; dashboard/disruptive limits are itemized in `docs/e2e.md` |
| T14 | five-task/commit verifier, strict 5×2 validator, stream collector, full-metric tables, aggregation and recommendation | **Stopped/documented limitation:** the 5×2 measurement produced one usable comparison pair (`small_fix`) only. The other records are not a basis for aggregation or a routing recommendation; its delivered value was discovery of the background-delegation-kill defect, fixed in `5472f0c` on 2026-07-18; `98cdfa7` later repaired the supervisor's package import through the installed wrapper. |
| T15 | runbook, rollback/recovery, activation, observation template | **Implemented/active:** home activation and recovery are verified. Routing adjustment now depends on the two-week production-task observation log, not on the stopped benchmark. |

Tasks T01 through T13 remain independently verified, including the fourteen-item
end-to-end evidence matrix; stopping this measurement does not make the harness
itself unverified. The RenAI 5×2 benchmark is stopped as a documented
limitation rather than described as nearly complete: only `small_fix` has a
usable baseline/cross-harness pair. It also used a fable parent model while the
installed orchestrator setting is opus, so the existing records cannot support
a routing recommendation. Effect measurement now moves to
`docs/observation-log.md`, where real production tasks can be observed for two
weeks without additional subscription-quota cost. Exact audit evidence and the
decision are in `docs/benchmark-results.md` and `docs/plan.md` section 16.
