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

Felix chose selective forgetting. request_fact_forget accepts concrete source
fact IDs, validates their owning document and type, and uses Hindsight's native
state=invalidated curation. Other live fact IDs and the shared document remain.
Invalidated facts are excluded from recall/consolidation; their audit archive
and the original chat archive remain. This is not forensic erasure or removal
of text already present in the still-open conversation. The older whole-source
administrative routine remains for compatibility and is not the requested
user-facing forget contract.

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
   are deleted only in the older whole-source mode. In selected-fact mode the
   worker instead validates each target's owning document and source fact type,
   then PATCHes only those facts to invalidated. Grounded observations are
   invalidated by Hindsight and remaining facts can be consolidated again.
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

Selective forgetting is chosen and implemented at the queue/worker boundary.
Finish real profile bootstrap, memory-sync unit activation, MCP
client configuration, Dream runtime removal and controlled Nanobot
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

## Selective fact acceptance, 2026-10-16

The final real test retained town and instrument facts in the SAME document.
Only the town fact was retired. The instrument's original fact ID and shared
source document remained, recall returned the instrument without the town,
and both saved Mental Models plus local USER.md were regenerated without the
town. USER.md still contained the instrument. The test ran as nanobot under
NoNewPrivileges=true; its fictional bank was then removed. See
fact-forget-report.json and real_fact_forget_test.py.

514 tests and 10 subtests passed across the affected recovery, memory and config
selection. Ruff passed; strict type checking of the two changed production
modules reported zero errors/warnings. Tests cover invalid IDs, wrong source,
source preservation and revisions preventing a later fact request from being
acknowledged by an earlier in-flight attempt. Legacy outboxes receive a serialized
schema upgrade for revisions and the fact-target table.

Internal reflection now has HINDSIGHT_API_RECALL_INCLUDE_CHUNKS=false. New Mental
Models explicitly set trigger.include_chunks=false, exclude_mental_models=true,
and refresh_after_consolidation=true. The trigger object is required: sending
refresh_after_consolidation at the request root is silently ignored in this API.
An initial last_refreshed_at alone does not prove content generation completed;
acceptance waits for nonempty saved content before checking the profile.

The original source identity is quarantined against replay. The memory tool now
creates a new active-context/export generation automatically (see below).
The caller must identify all intended fact IDs if a
piece of information has duplicate/paraphrased entries; one UUID is not a semantic
ban on all future restatements or manually rewritten/forked source identities.
The production Nanobot is still unchanged; no real profiles or chats were imported.


## Explicit memory tools and context reset, 2026-10-16

With Hindsight enabled, the main agent registers one exclusive `memory` tool:
`remember` queues original persisted conversation evidence; `forget` accepts
explicit fact UUIDs; `status` reports delivery, forget receipts and worker pause /
resume state. It is not discovered by subagents. Mutation requires a persisted
user request context. Queued is never reported as completed.

Forget resolves every fact through the configured bank before queuing anything.
Only world/experience facts with a locally known source document are accepted;
derived observations and foreign source IDs fail without creating receipts.
Tool HTTP traffic uses the shared URL guard and pinned DNS transport, requires
an explicit loopback SSRF whitelist, and never follows redirects or environment
proxies. Remote error bodies and source text are not returned in errors.

A durable SQLite generation invalidates active context in both the source and
requesting sessions. Old rows remain in the archive; provider continuation state,
saved summaries and the AutoCompact RAM summary are discarded. A turn already in
flight is fenced when saved, and a restarted process checks the same generation
before assembling another model request. Stale asynchronous compaction cannot
commit its old summary. New exports start at the new boundary and use a new
conversation identity; the quarantined prefix cannot be automatically reimported.
Forks created after reset preserve the export fence.

During pending forgetting, profile reads use pending placeholders; legacy
MEMORY.md is excluded from Hindsight-enabled prompts altogether. Normal SOUL.md
and USER.md injection resumes after the worker has invalidated and recreated the
saved profiles. Existing facts outside the selected IDs remain available through
recall. The archive and Hindsight invalidation audit are retained; this is not
forensic deletion or a semantic ban on future user restatements or preexisting
independent copies. Dream must still be disabled before production activation.

Validation: 575 tests and 10 subtests passed across memory, recovery, context,
configuration and agent persistence. Ruff passed. Strict checking of all eight
changed production modules reported zero errors and warnings. The final legacy
MEMORY.md prompt exclusion is additionally covered by the enabled-mode regression.


The real acceptance test exercised `memory.remember` and `memory.forget` through
the guarded loopback transport against the native Luna/Qwen stack under
NoNewPrivileges=true. One town fact was retired; the instrument fact and original
shared document remained, saved profiles retained the instrument without the
town, active context was reset, and subsequent original evidence queued under a
new identity without the old fact. The fictional bank was removed; all dedicated
services and the unchanged production Nanobot remained active. See
memory-tool-report.json and real_memory_tool_test.py. After the final prompt
change, 84 focused context/agent tests and Ruff passed; both changed modules were
rechecked with strict typing.
