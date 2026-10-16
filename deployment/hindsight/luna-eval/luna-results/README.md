# Luna acceptance tests on the Pi, 2026-10-16

The dedicated Hindsight Codex login was verified without copying existing bot
credentials. Tests ran as the unprivileged `hindsight` system user with private
synthetic data under `/var/lib/hindsight/luna-test`.

Configuration: Hindsight Slim 1.0.0, `openai-codex`, `gpt-6-luna`, low reasoning,
strict structured output, one worker slot, one concurrent LLM request,
Qwen3-Embedding-0.6B Q8_0 with two threads and RRF. No paid API fallback.
Initial retain/consolidation had no automatic output-language override. One
English source produced Spanish facts, so the correction run explicitly used
`HINDSIGHT_API_LLM_OUTPUT_LANGUAGE=English`. This sets internal generated text,
not the user's requested conversation language. Production should use a
consistent output language matching its configured full-text search language.

## Actual provider and API checks

- Six fictional documents were extracted into structured facts by Luna.
  The reported retain usage totaled 12,315 tokens. This is only those six
  retains; it excludes consolidation, profile generation and later test calls,
  and does not measure the account's remaining subscription quota.
- Six recall queries: expected source document ranked first in 4/6 and among
  the first three in 6/6. Median latency was 2.99 seconds.
- One first-source miss returned the correct vegan fact from a different source;
  the notification query returned a communication-style fact before the relevant
  notification fact. Source ranking is not a semantic accuracy metric.
- Consolidation completed and generated recallable observations.
- Saved `agent-soul` and `user-profile` Mental Models generated successfully.
  The actual Nanobot ProfileExporter wrote both into a synthetic workspace.
  The profiles preserved the explicit name, vegan preference and language;
  the unconfirmed assistant proposal about fish was not promoted to a preference.
- Editing local USER.md prevented its subsequent overwrite.
- Stopping the real API preserved both existing profile files.
- The actual SQLite outbox/worker submitted an async retain. A reconstructed
  worker reconciled completion, and a duplicate submission reused the same
  operation identity.
- An HTTP 429 simulated with MockTransport preserved a pending local job and
  scheduled backoff. This did not exhaust the account or test a real upstream
  quota reset. It is separate from the actual provider/API tests above.

The stack left at least 1943 MiB MemAvailable during the first run, with 76.1 MiB
additional swap and a 66.7 C maximum sampled temperature. These are short tests
without simultaneous heavy agent activity or a historical import.

## Preference correction

A later explicit synthetic correction changed Mira's answer-language preference
from German to English while keeping the vegan preference and assistant name.
After consolidation and saved-model refresh, the profile stated the new English
preference and identified German as the superseded preference. Existing files
under the real Nanobot workspace were never read or modified.

`report.json` contains the initial facts, ranked results and generated profiles.
`correction-report.json` contains the updated profile and recall results.
These are small acceptance checks; they do not establish broad model quality.

## Background runtime preparation

The new `nanobot.memory_sync.service` runs outbox delivery and saved-profile
polling independently. An API error retries later while retaining local files.
Polling only fetches saved models; it does not invoke reflect or refresh. The
process requires an initialized profile baseline and emits status changes
without raw error payloads. A user-service example is included for later setup.

Validation: 18 tests plus 10 subtests passed for the service, worker and profiles;
Ruff passed; strict checking of the service module reported no errors or warnings.
The example unit is not installed or enabled, and the source feature is not
installed in the production bot. All transient test servers were stopped.

Still required before activation: durable service/bootstrap setup with real
authorized baselines; live MCP configuration; complete remember/forget handling;
multi-iteration compaction/recovery verification and controlled production switch.
Dream remains active until the replacement is ready.

References: [Codex authentication](https://learn.chatgpt.com/docs/auth),
[Hindsight configuration](https://hindsight.vectorize.io/developer/configuration).
