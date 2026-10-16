# Dedicated Pi runtime and source deletion, 2026-10-16

The native stack is installed under /opt/hindsight and /var/lib/hindsight. The
production Nanobot package, config, SOUL.md, USER.md and conversations are unchanged.
The new Hindsight database contains no real conversation/profile imports. All
acceptance data was fictional and its test bank was removed.

## Installed services

- hindsight-postgres: PostgreSQL 17, pgvector, private cluster, loopback 55433.
- hindsight-embedding: the hash-verified Qwen3-Embedding-0.6B Q8_0 and evaluated
  native llama.cpp build, two threads, loopback 8891.
- hindsight-api: Hindsight Slim 1.0.0, loopback 8888, embedded worker disabled.
- hindsight-worker: one background slot, loopback health 8892, GPT-6 Luna through
  the dedicated hindsight user's ChatGPT Codex login. No paid API fallback.
- hindsight-worker-gate.path and its root-owned oneshot service observe only
  /var/lib/hindsight/control/forget-pending and stop/start only hindsight-worker.

All are enabled for boot. API /version confirms worker=false, llm_trace=false,
bank_config_api=false. MCP allows only recall and get_mental_model. No raw provider
logs are persisted by the API/worker systemd units. runtime.json and the database
password are private; they must never be copied into the repository or printed.
Binaries and shared libraries are root-owned under /opt. No service depends on
/tmp or /var/tmp evaluation binaries. The retained test scripts are not services.

The kernel exposes no memory cgroup controller. MemoryMax values in the units
are therefore not effective enforcement. A post-test snapshot showed about
1993 MiB MemAvailable and 463 MiB swap in use; this is not a peak-load guarantee.
Heavy concurrent Nanobot work and a large initial history import remain untested.

## Forget protocol implemented so far

This implementation deletes an entire source conversation from the long-term
bank, not a selectively chosen individual fact. Its user-facing tool is not
connected pending Felix's choice of granularity. It does not erase the original
chat archive or the still-open conversation context and is not forensic erasure.

1. A SQLite transaction tombstones the source, erases local queued payloads and
   records its document/operation receipts. The durable pause signal is written
   first. Reimport of that conversation identity is suppressed permanently.
2. The fixed root path service stops the dedicated worker. The unprivileged
   memory process retains NoNewPrivileges=true and only reads systemd status.
   It verifies inactive/failed, MainPID=0, ControlPID=0, KillMode=control-group,
   SendSIGKILL=yes. The API must report embedded worker=false.
3. Existing SOUL.md/USER.md projections are cleared to headings while generation
   is unavailable. This also clears previously edited local profiles when
   an authorized forget operation is performed; no backup copy is made.
4. All parent AND child operations are cancelled. Parent cancellation alone
   does not cancel child retains in this Hindsight version. Source documents
   and their associated facts/grounded observations are deleted.
5. Both saved Mental Models are removed and recreated from remaining evidence.
   Their refreshes cannot run while the worker is stopped.
6. Only after all queued deletions complete is the pause signal removed. The
   root path service resumes the worker. A durable resume receipt handles a
   crash between deletion and successful restart. Transport failures keep the
   source suppressed, profiles empty and worker stopped for a later retry.

Delivery, projection and deletion share the existing delivery lock. A marker
without a committed request fails closed and needs inspection. The pause-file
path is bound to the outbox settings so reopening cannot silently use a different
signal. This deployment currently assumes one Nanobot workspace/outbox.

## Validation

The real acceptance test ran as user nanobot in a transient systemd unit with
NoNewPrivileges=true. It caught a Retain CHILD in processing while its Batch
parent was pending, stopped the worker, injected HTTP 503 on document deletion,
then resumed using fresh outbox/worker objects. The document remained absent
15 seconds after worker restart, recall returned zero results, original-source
reimport was blocked and the synthetic bank was removed. See forget-report.json.
No production source conversations or profiles were used.

507 tests and 10 subtests passed across the affected recovery/memory/configuration
selection. After final pause-path binding and crash-window fixes, 52 focused
memory tests and 10 subtests passed (overlapping the broader selection). Ruff
passed; strict typing checked all nine affected production modules without
errors or warnings, then rechecked the final two modified modules.

The earlier sudo-based controller was replaced after automatic security review
rejected weakening NoNewPrivileges. The final marker controller passed the same
real acceptance test with the protection enabled. No rejected change was applied.

## Remaining activation work

Resolve whether the requested forget tool must selectively remove facts or may
remove whole source conversations. Then finish the appropriate tool contract,
explicit remember, real profile bootstrap, memory-sync unit activation, MCP
client configuration, Dream/runtime MEMORY.md removal and controlled Nanobot
switch. Test long mixed/code inputs against the embedding context limit before
history import. The current standalone runtime is ready; the Nanobot memory
migration is not complete.

## Installation artifacts

install_runtime.py is a one-time installer from the already evaluated Pi files;
it refuses an installed manifest or occupied ports. Do not rerun blindly after a
partial failure. finish_runtime.py records the initial readiness repair and
hardening used on this installation; it is not a generic migration/recovery tool.
install_worker_gate.py installs the narrow fixed-unit controller. The production
memory-sync unit must retain NoNewPrivileges=true and pass
--pause-file /var/lib/hindsight/control/forget-pending; Nanobot's explicit
agents.defaults.hindsight.pauseFile must use the same path. Real profile seeding
and baseline registration must finish before starting that unit.
