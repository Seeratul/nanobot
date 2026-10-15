# Hindsight memory integration status

Development branch: `felix/hindsight-memory` in https://github.com/Seeratul/nanobot.
The running Pi still uses the previously captured source and configuration.

## Implemented and tested components

- An opt-in observer at Session.commit_summary_checkpoint and Session.clear
  captures original session prefixes before the corresponding local mutation.
  Original JSONL sessions remain the source archive; no second full journal.
- SQLite outbox commits immutable sanitized segments and export cursors together.
  Deterministic conversation/document/operation identities handle restart and
  uncertain delivery. Summary markers do not affect message sequence numbers.
- Export failure prevents a checkpoint/reset mutation. When export is enabled,
  the existing session and runtime-checkpoint writes request fsync.
- Hindsight v1 worker polls the durable operation identity before submission.
  HTTP acceptance is not acknowledged as completion. Quota/network failures
  preserve jobs and back off; configuration/schema errors need attention.
- SOUL.md and USER.md projection code fetches saved Mental Models without
  calling reflect on every poll. Atomic writes, size/format checks and local-edit
  detection preserve the last valid files on errors. File validation does not
  prove factual correctness; the model source queries constrain attribution.
- Runtime configuration defaults to hindsight.enabled=false. No paid-provider
  fallback or credit purchasing is implemented.

## Validation

The new regression tests and existing session/memory/compaction tests run using
the Pi's Python dependencies against an isolated source checkout under /tmp,
with a temporary HOME and temporary test tooling. No real source conversations,
LLM requests or production profiles are used. Ruff checks the modified files.

The tests cover transaction rollback, restart before session save, duplicate
capture, simultaneous enqueue, /new, incomplete tool exchanges, unchanged source
prefixes, payload corruption, bank changes, quota backoff, lost HTTP responses,
profile outages and preservation of local profile edits.

## Required before production activation

1. Discuss and choose the local embedding model with Felix before downloading
   weights or running an embedding benchmark. This is explicitly pending.
2. Set up pinned Hindsight v1.0.0 and PostgreSQL/pgvector with loopback access.
   Test actual API behavior; HTTP mocks are not a deployment test.
3. Create a dedicated Codex login for Hindsight. No dedicated auth.json exists
   on the Pi. Do not copy/share rotating credentials from the running agent.
4. Complete service/bootstrap wiring, initial authorized profile ingestion and
   Mental Model creation/refresh. Connect the profile poller to the service.
5. Finish explicit remember/forget operations, including in-flight cancellation,
   remote deletion, local profile invalidation and suppression of re-import.
   Local outbox suppression alone is not an implemented end-to-end forget tool.
6. Exercise token/native compaction inside a running multi-iteration turn and
   recovery after power loss. The shared observer captures at local session
   checkpoint commit, not before the provider performs native compaction.
   Existing runtime recovery must be checked for all intermediate tool rounds;
   the current tests do not establish that stronger durability guarantee.
7. Configure MCP tool filtering and explicit loopback allowance. Existing
   tools.ssrfWhitelist supports a literal 127.0.0.1/32 exception, so a new HTTP
   transport is not required. No network whitelist has been changed yet.
8. Switch the live service only after successful integration checks; then remove
   Dream and the redundant automatic MEMORY.md injection. Preserve active context
   compaction and the SOUL.md/USER.md bootstrap files.

Felix selected GPT-6 Luna through Codex, local embeddings and no backups for now.
No backup jobs, embedding model downloads, Hindsight containers, profile changes
or live-service restarts have been performed during this development stage.

API contract inspected: Hindsight release v1.0.0, published 2026-10-13,
`hindsight-docs/static/openapi.json`. Runtime HTTP behavior still needs testing.

Validation on the Pi: 267 tests and 10 subtests passed; Ruff passed. Strict
type checking of the five memory modules and three modified existing files
reported zero errors and zero warnings. These are component checks, not a
completed live Hindsight migration.
