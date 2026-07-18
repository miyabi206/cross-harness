## Shared cross-harness safety rules

- Never pass API keys, access tokens, credential files, `.env` contents, or
  keychain values between harnesses or into logs.
- Never launch Claude from a delegated Codex run or Codex from another Codex
  run. Cross-harness delegation has exactly one direction and one level.
- Preserve user changes. Do not reset, overwrite, clean, or mix pre-existing
  uncommitted work into delegated changes.
- Use the least sandbox permission that completes the role. Never use
  `danger-full-access` in this harness.
- Stop on unknown authentication, rate limits, recursion detection, or an
  exhausted retry budget. Never switch to API billing or an external router.

