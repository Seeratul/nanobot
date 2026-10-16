# Isolated Hindsight stack test, 2026-10-16

The real Raspberry Pi 4 ran Hindsight API Slim 1.0.0 in a separate Python 3.13
venv, PostgreSQL 17.11 with pgvector 0.8.0 in a separate SSD data directory,
and Qwen3-Embedding-0.6B Q8_0 using two threads. Nanobot remained active.
All test services bound only to loopback and stopped after the test.

The test used `LLM_PROVIDER=none`: real chunk storage, vectors, hybrid recall
and RRF, with no LLM fact extraction, reflection, observations or consolidation.
No existing conversations or production profiles were read or changed.

| Measurement | Result |
|---|---:|
| Synthetic records stored | 18 |
| Queries with expected first result | 10/14 |
| Queries with expected result among first three | 13/14 |
| Median recall latency | 3.20 s |
| Maximum recall latency | 3.57 s |
| Median synchronous retain latency | 3.52 s |
| Peak Qwen RSS sampled | 900 MiB |
| Peak Hindsight RSS sampled | 354 MiB |
| Minimum system MemAvailable | 1969 MiB |
| Additional swap consumption | 11.3 MiB |
| Maximum sampled temperature | 67.2 C |
| Recall still works after API process restart | Passed |

Minimum available RAM includes PostgreSQL and other running services. Per-process
RSS covers the directly sampled Qwen/API processes, not every descendant or
PostgreSQL backend; it is not a sum of the stack's unique physical pages.
There was no concurrent agent/import stress load. The baseline memory snapshot
was taken after PostgreSQL started. Post-test throttle flags were `0x0`.

The four top-1 misses concerned answer language versus keyboard layout, Atlas
frontend versus backend, startup failure versus DNS repair, and HDMI repair
versus disk repair. The language memory was outside the first three; a separate
post-restart recall did return it. This differs from the earlier pure cosine
test (14/14). The combined retrieval pipeline needs further evaluation with
Luna fact extraction; the current result does not establish production quality.

PostgreSQL packages were installed without creating a Debian default cluster.
The private test directory is `/var/tmp/hindsight-pi-stack-test` (mode 0700),
and the venv is `/var/tmp/hindsight-pi-test-venv`. Test database credentials stay
in that private directory and are never included in this repository.
The saved data contains only fictional records. Package versions are recorded
in `hindsight-pi-test-requirements.txt`; these are a test snapshot, not a
cross-platform lockfile with artifact hashes.

Hindsight's defaults reserve two consolidation worker slots. For this one-slot,
LLM-free test, `HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS=0` is required.

## Next dependency: separate Codex login

The service identity `hindsight` has home `/var/lib/hindsight`, no sudo privileges,
and a private Codex directory `/var/lib/hindsight/codex`. No existing Nanobot or
desktop credentials were copied. Its config uses file credential storage and
ChatGPT login. There was no dedicated `auth.json` at the last check.

Felix can run `../login-hindsight.ps1` in his own PowerShell terminal, follow the
device-auth link and enter the one-time code there. Do not send the auth file or
tokens in chat. After login, verify Luna structured output, extraction,
consolidation, quota handling, async outbox behavior and profile generation
before connecting real sessions or removing Dream.

References: [Hindsight configuration](https://hindsight.vectorize.io/developer/configuration),
[OpenAI Codex authentication](https://learn.chatgpt.com/docs/auth).
