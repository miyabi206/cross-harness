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
| T14 | five-task/commit verifier, strict 5×2 validator, stream collector, full-metric tables, aggregation and recommendation | **In progress:** private-repository transmission is approved; 4/10 runs are complete with manual audits, while the remaining six wait for the Claude session-limit reset at 18:30 JST |
| T15 | runbook, rollback/recovery, activation, observation template | **Implemented/active:** home activation and recovery verified; benchmark-driven routing adjustment waits on T14 |

All verification outside the real RenAI comparison is complete. Authorization
to send task-relevant source and tool output to Anthropic and OpenAI has been
received, and the real 5×2 measurement is in progress. Its final aggregation
gates the T14 comparison and T15 routing adjustment. Exact evidence and
remaining checks are in `docs/live-verification.md`.
