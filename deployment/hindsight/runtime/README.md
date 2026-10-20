# Dedicated Pi runtime and source deletion, 2026-10-16

The native stack is installed under /opt/hindsight and /var/lib/hindsight. The
production Nanobot package, config, SOUL.md, USER.md and conversations are unchanged.
The real SOUL.md and USER.md baselines have been imported. Historical import preparation and current progress are documented below. Earlier acceptance banks contained fictional data and were removed.

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
Finish memory-sync unit activation, MCP
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


## Existing profile bootstrap, 2026-10-16

`python -m nanobot.memory_sync.bootstrap` imports the existing SOUL.md and USER.md
as explicitly user-authorized initial baselines. It uses the same private outbox
and bank as the future delivery service. Required arguments are --workspace,
--outbox, --manifest, --profile-state and --pause-file; --bank-id defaults to
personal and --api-url to the loopback API. Parent state directories must exist.

The manifest stores hashes and deterministic source identities, not backup
copies. Source changes, possible credentials, unrelated bank documents, malformed
models and failed generation stop the bootstrap. Lost acknowledgements resume
through the existing operation UUIDs. A nonempty generated model is required;
creation timestamps alone cannot make the import ready. The importer rechecks
sources before registering the projection baseline. It does not write SOUL.md or
USER.md and does not start automatic projection.

70 memory tests and 10 subtests passed, including bootstrap restart/idempotence,
changed sources, foreign source refusal, credential-pattern refusal and models
that have timestamps without content. Ruff and strict checking of all three
changed production modules passed. Real profile inputs were additionally tested
against local Qwen: 362/301 tokenizer tokens, 1024 output dimensions, and reported
embedding input usage of 363/302 tokens. This validates these initial profiles;
long mixed/code history inputs still need separate coverage before history import.

The live bootstrap runs as nanobot in a transient systemd unit with
NoNewPrivileges=true, using the isolated verified source tree. Its outbox and
state live in the existing workspace session directory. Production Nanobot
package/configuration and automatic projection remain unchanged until the
controlled switch; Dream must be disabled before projection is activated.


The real bootstrap completed successfully under NoNewPrivileges=true: two source
profiles, twelve world facts and twelve derived observations, with both saved
Mental Models generated. Existing local profile contents still match the imported
baseline hashes. No original profile was overwritten, no backup was created, and
no operations remained pending at the final check. Production Nanobot and all
dedicated Hindsight services remained active. See profile-import-report.json.

Review compared the combined SOUL.md + USER.md context, because both files load
in every conversation and valid information may move between them. The initial
per-file check incorrectly reported such moves as losses/additions. The combined
check found no unsupported additions or contradictions, but flagged two intro
statements; a focused source comparison confirmed their meanings were already
present. The private review records those raw flags, their resolution and the
reviewed model hashes. The public report contains aggregate counts/booleans only.
This automated review is not a mathematical proof of semantic fidelity.

The baseline/projection state is now registered but automatic projection is NOT
enabled. Existing MEMORY.md and historical conversations were not imported in
this step. Next activation work is Dream runtime removal, permanent package and
memory-sync installation, guarded MCP configuration and the controlled Nanobot
switch. Long mixed/code embedding inputs must pass before historical import.

## Initial history import, 2026-10-17

Import precedes production activation. The initial snapshot contains 17 archived
conversations and one legacy MEMORY.md; 10 Dream journals are excluded. Original
source archives, SOUL.md and USER.md are never written by the importer. Provider
state, hidden automation and memory-tool echoes are excluded using the existing
export adapter. Credential patterns and configured secret values are redacted
locally. The sanitized plan contains 3,400 nonempty exported messages and
9,844,286 characters; it is deleted after the real outbox owns all submissions.
This transient submission plan and outbox support delivery, not backups.

Native chunking produces 4,786 chunks of at most 3,000 characters. Every chunk
was measured with the actual Qwen tokenizer: 3,304,313 tokens in total, median
789, p95 1,177, maximum 2,120. The native cap tokenizer's maximum was 1,786,
below Hindsight's 8,192-token truncation cap. No legacy execution-record migration
was needed in the 17 source sessions.

The previous 2,048-token context rejected the largest actual chunks. The dedicated
embedding service now uses a 4,096-token context, preserving two threads and the
1,024-token batch settings. Long inputs are measured separately with enough time
for the Pi. The import gate requires successful 1,024-dimensional embeddings,
reported usage matching full input lengths, a matching plan hash, and each test
under the native embedding client's 600-second timeout. Failed acceptance cannot
queue historical sources.

`hindsight-history-import.service` uses a separate root-owned source copy under
/opt/hindsight/nanobot-import and the maintenance script under
/opt/hindsight/maintenance. Its runtime does not depend on /tmp. It runs as
nanobot with NoNewPrivileges=true, private umask and reduced CPU priority. It uses
the existing private outbox and bank personal, with deterministic document and
operation IDs. Known quota/transport errors keep jobs queued through the existing
worker's backoff; there is no paid provider fallback. Unknown failures stop for
attention. Restarting the unit resumes the manifest instead of duplicating data.

Nine focused tests cover matching acceptance, rejection of slow/mismatched
acceptance, lost enqueue acknowledgements, source conflicts, pending forgets,
suppressed source protection, unchanged local profiles, and recall of duplicate
facts. Completion verification checks all expected remote documents, native
operation completion, fact listings and an index round trip per source with
facts. Identical facts retrieved from another source are counted separately.
This checks storage/index accessibility and does not prove semantic fidelity.

Operational commands: `sudo systemctl status hindsight-history-import` and
`sudo journalctl -u hindsight-history-import -o cat --no-pager`. The safe
history_import_progress.py helper emits only counts and phase progress. After
completion, inspect the private manifest's aggregate verification report before
production handover. Chat activity and Dream may continue during the snapshot
import; check for newer evidence and profile changes before activating the new
Nanobot package and profile projection. No production activation is performed
by this import service.

The full-input acceptance passed with the expanded context: the largest real
chunk (2,120 tokenizer tokens) returned all 2,121 input tokens including BOS,
1,024 dimensions and HTTP 200 in 279.115 seconds. The mixed Unicode case returned
all 2,280 input tokens, 1,024 dimensions and HTTP 200 in 303.295 seconds. This is a
measured throughput limit on this Pi, not a guarantee for every future input.

The historical import started on 2026-10-17 at 13:27:46 CEST. All 18 source
submissions were durably queued; the temporary sanitized plan was removed after
queue ownership transferred. The import service is enabled and runs independently
of Codex. Production Nanobot and local profile projection remain unchanged.

The 3,304,313 tokenizer tokens above describe the source-chunk safety inventory,
not the actual embedding workload. In concise extraction mode the native retain
pipeline embeds date/entity-augmented extracted facts; it stores the source
chunks as evidence. Do not estimate total import duration by assuming that all
raw source tokens are embedded. Use measured fact/chunk progress and allow for
later consolidation and saved-model refresh work.
The first observation ended at 13:33:08 CEST after approximately 320 seconds:
three of 18 sources completed, ten native chunks stored, and 19 new facts.
All dedicated services and unchanged production Nanobot were active. There was
about 1,505 MiB available RAM and swap use remained about 498 MiB. No unknown
job failures were observed. This is an early snapshot, not completion evidence.

Ten chunks in 320 seconds linearly project to about 43 hours for the 4,786 source
chunks. The reported rough remaining ETA is two to four days including allowance
for consolidation and model refresh, with low confidence: the first sources are
small and not representative of all technical chats. Quota pauses can extend it.
The user requested only a five-minute observation, then an ETA and an end to the
Codex monitoring loop. Monitoring has ended; the enabled import service continues
independently. See history-import-report.json for the sanitized measured snapshot.

## Batch eight and paused initial import, 2026-10-17

At Felix's request the history importer and dedicated Hindsight worker were
stopped before changing batching. Stop verification checked MainPID=0 and
ControlPID=0; submitted operations were not cancelled or declared complete.
Original archives and the durable outbox remain intact.

Hindsight is configured for eight texts per embedding HTTP request, with one
in-flight request. Qwen uses eight parallel sequence slots, two CPU threads and
the existing 1,024-token logical/physical batch sizes. A text batch count is not
the same parameter as llama.cpp's token batch size.

Reserving 32,768 context tokens for eight fully reserved 4,096-token slots caused
memory pressure and swapping on this Pi; validation was stopped and that
configuration was removed. The installed version supports a unified KV pool:
`--ctx-size 8192 --kv-unified --kv-unified-per-slot 4096 --parallel 8`.
The total shared pool is 8,192 tokens, with a 4,096-token maximum per sequence;
eight simultaneous sequences cannot each occupy a full 4,096-token context.
The service's nominal MemoryMax is 2300M, but the earlier kernel limitation still
means this is not an enforced RAM limit. Verify live memory rather than relying
on it. No KV quantization, model weights, thread counts or boot settings changed.

The corrected configuration reports eight slots with n_ctx=4096 and initially
about 1.9 GiB available system RAM during the mixed test. The first short test
used the same eight synthetic inputs, once as sequential single-text requests
and once as one eight-text request: 10.144 seconds versus 10.352 seconds, minimum
cosine similarity 0.9996568554, correct indices and 1,024 dimensions. This single
short test shows no speedup and does not establish production throughput. The
large mixed test includes the actual longest initial source chunk (2,120 tokens)
plus seven short texts and checks all eight result indices and complete token
usage. See batch8-report.json after validation finishes.

configure_batch8.py requires the importer and worker to be stopped, updates only
the non-secret batching choices inside the existing private runtime configuration,
preserves file ownership and mode, and installs the bounded unified-pool command.
It does not resume import. The normal installer now emits the same settings.


The mixed long batch passed: eight 1,024-dimensional vectors, correct result
indices and all 2,221 input tokens including per-sequence BOS, in 280.693 seconds.
The import and worker remain explicitly paused. The API and embedding service
are restored; Nanobot remains active. This change does not claim a throughput
gain: the measured single short comparison was approximately equal in speed.

## Six CPU threads, 2026-10-17

At Felix's request, Qwen now uses `--threads 6 --threads-batch 6`. The Pi reports
four online CPU cores; these are six software threads scheduled on four cores.
The eight-text batches, eight slots and bounded shared KV configuration remain
unchanged. Both installer and batch configuration helper preserve this choice.
The history importer and Hindsight worker remain stopped. A short batch smoke
test uses exactly the eight synthetic inputs from the previous batching check;
its one-shot timing is not a representative full-import benchmark.

## Repeated real-fact thread comparison, 2026-10-17

The longer comparison uses 32 unique stored world/experience fact texts, selected
at evenly spaced length ranks among eligible 80-600-character facts. The chosen
texts are frozen in process RAM and never copied to the repository or journal.
The workload contains 2,470 input tokens per run including BOS, in four batches
of eight. This is an embedding-only test of real fact text; it excludes native
date/entity augmentation, Luna extraction, consolidation and database writes.

The run order is 2, 6, 6, 2 threads, with both --threads and --threads-batch set
together. Every run starts a fresh embedding process and performs the same
synthetic warm-up outside its timed section. All context, batch, model and slot
settings remain fixed. Both cases run twice with identical input order. The
controller checks dimensions, indices, finite vectors, exact input token usage
and cosine similarity against the first run, without exporting vectors.

CPU and memory counters come from /proc, thermal_zone0 is sampled each second,
and vcgencmd throttle flags are captured around every run. The report records
whole-system and embedding-process CPU use separately, available RAM, process
peak RSS including warm-up, and swap-in/out page deltas. Nanobot remains running;
external activity can affect timings. Two repetitions per case are an observed
comparison, not a general performance guarantee.

The dedicated bounded transient benchmark unit runs only while importer and
worker are stopped. Its finally handler and root-owned ExecStopPost helper both
restore six threads and the API, including on failure/timeout. It does not start
the worker or importer. Private counter reports are written atomically under
/var/lib/hindsight/benchmarks; the public report excludes the dataset hash.


The ABBA comparison completed successfully. Two threads took 224.838 and
225.538 seconds (mean 225.188); six took 127.517 and 128.881 (mean 128.199).
With identical workloads, six threads used 43.07% less embedding wall time,
about 1.757x throughput in this test. Whole-system busy CPU averaged about 53%
with two threads and 98% with six. Maximum recorded temperature was 63.783 C;
all throttle flags remained zero, minimum available RAM was 1722.9 MiB and no
swap-out pages were recorded during the timed sections. All vector/index/token
checks passed. This does not imply the entire Luna/database import is 43% faster.

The unit exited cleanly. Six threads, the eight slots/shared pool, and API HTTP
200 were verified after restoration. Nanobot is active; the worker and importer
remain stopped. No benchmark or Codex monitoring loop remains running. The
sanitized full measurement is in thread-comparison-report.json.

## Pi-local evening resume trigger, 2026-10-17

Felix requested the chosen batch eight / six-thread configuration to remain and
the import to resume automatically at 21:00 Europe/Berlin, with the Pi available
for other work before then. The existing installer/helper defaults and live unit
already persist both six-thread settings and eight text slots/batches.

The enabled native hindsight-scheduled-import.timer is a single dated calendar
event: 2026-10-17 21:00:00 Europe/Berlin (19:00 UTC). It uses Persistent=true,
AccuracySec=1s and no randomized delay. It runs directly on the Pi and does not
require Codex, the Windows PC, or a recurring AI automation. If the Pi is off at
the deadline, normal persistent-timer catch-up runs when the Pi is available.

The importer, worker, API and embedding services were all stopped. A root-owned
scheduled-import.hold marker and per-service ConditionPathExists drop-ins prevent
those enabled services, including the worker gate, from starting before the
scheduled resumption after a reboot. The PostgreSQL service and production
Nanobot are untouched. The check reported all four pipeline services inactive,
Nanobot active, and about 3,023 MiB available RAM after stopping the model/API.

The scheduled oneshot verifies the absolute local deadline and refuses to resume
while targeted fact retirement is pending. It removes the hold durably, then
starts embedding, API, worker and importer in order, preserving their existing
hardening and forget gate. It checks nonzero service PIDs and emits only generic
status counters. Its NoNewPrivileges setting is enabled. It does not report the
import complete merely because services have started.

systemd-analyze calendar/verify, Ruff on both scripts, the live --check mode,
loaded enabled timer and next firing time were checked. The observed timer state
was active with next elapse 2026-10-17 21:00:00 CEST, no prior trigger. The dry
check confirmed deadline_reached=false, hold_present=true, forget_pending=false.
The future job was not run early. No Codex monitoring loop remains active.


## Two native worker processes, 2026-10-18

At Felix's request the historical import continues with two native worker
processes and two task slots per process. Both run under the existing
hindsight-worker.service and its control group. The stable worker IDs are
felix-pi (retained for native recovery of the interrupted task) and felix-pi-2;
their loopback health ports are 8892 and 8893. The API's embedded worker stays
disabled. LLM and embedding concurrency limits remain one per process, so up
to two calls can run across the two processes. Qwen still has eight text slots,
batch eight, six CPU/batch threads and the bounded shared 8192-token KV pool.

worker_supervisor.py fails the shared unit if either child exits unexpectedly,
allowing the existing Restart=on-failure policy to recover the group. Under
KillMode=control-group systemd sends the stop signal to both children directly;
the supervisor does not forward a duplicate SIGTERM. It allows 38 seconds for
child shutdown before killing a remaining child. The unit stop timeout is
50 seconds. The fixed forget gate and marker condition still stop the entire
shared unit, with NoNewPrivileges retained. MemoryMax is nominally 1400M for
the combined workers; the current Pi kernel lacks memory-controller enforcement.

The preceding 603.2-second experiment used an ephemeral database copy, which
was removed afterwards. Both processes worked concurrently (three claimed
tasks), completed at least 322 successful extraction calls, used at most
791.8 MiB combined RSS and reached at most 54.53 C. No large retain job finished
within that window. It establishes resource/concurrency feasibility, not a
measured throughput improvement against an identical single-worker baseline.
See worker-parallel-test-report.json for aggregate counters.

install_parallel_workers.py is an explicit upgrade helper for an already
stopped installation. It preserves runtime credentials and provider settings,
sets two slots, installs the supervisor, adjusts the worker unit, and reloads
systemd without starting the importer itself. Do not rerun the initial installer
on an existing installation. The production Nanobot package remains unchanged.


## Local numeric usage ledger, 2026-10-18

After the native workers are healthy, run install_usage_tracking.py as root from
the installed maintenance directory, with the sibling usage modules and
install_parallel_workers.py present. It installs/enables a local systemd timer
that scrapes the two fixed loopback metrics endpoints every 30 seconds. It does
not restart workers, change runtime credentials, or enable content tracing.

The ledger is /var/lib/hindsight/usage/usage.sqlite3 (directory 0700, database
0600, owner hindsight). SQLite transactions use synchronous=FULL. Run:

    sudo -u hindsight /opt/hindsight/venv/bin/python /opt/hindsight/usage_ledger.py report

Counters are keyed by fixed worker ID and Linux boot/PID/start identity.
Repeated scrapes update a maximum rather than double count; totals survive
collector and worker restarts. The first scrape captures counts since the
currently running workers started, not the entire historical import. Counters
before tracking and the unsampled tail of an abruptly stopped worker cannot be
reconstructed. A crashed worker can lose up to the scrape interval of call
counts (longer if collection fails). The native counters count provider calls,
not every internal HTTP retry.

At the next worker start, the supervisor launches usage_worker.py, which wraps
only CodexLLM.call and then runs the native worker entrypoint. It records
successful calls' provider character/4 input/output estimates in daily UTC
aggregates. The in-flight import was deliberately not restarted to activate
this hook. Earlier calls have no estimates; missing estimates never imply zero
usage. Estimates omit hidden reasoning, cache accounting and potentially schema
overhead; they are neither billed-token counts nor subscription quota usage.
The wrapper persists no arguments, output content, error strings, bank IDs,
document IDs or arbitrary metric labels. Only fixed worker IDs, timestamps,
numeric totals and process-start identities are stored. Errors do not turn an
already successful model call into a retry. Native full-content tracing stays
disabled. No external telemetry service or new model calls are added.


## Large historical-retain wall limit, 2026-10-18

HINDSIGHT_API_RETAIN_WALL_TIMEOUT is explicitly 86400 seconds (24 hours).
The native default was an absolute one-hour ceiling. Two partially imported
multi-megabyte documents entered delta retain, which gathers extraction for all
missing chunks before embedding/writing them. Those jobs repeatedly exceeded
one hour; the outbox retried their failed operations, so successful Luna calls
did not translate into new durable chunks.

install_retain_timeout.py is the stopped-installation upgrade helper. Stop
hindsight-history-import first, take a final usage-counter sample, then stop the
shared hindsight-worker unit. The helper atomically changes only the retain
wall-limit field, preserving runtime ownership, permissions and other settings.
Start the existing worker/import services afterwards. A restart discards
uncommitted extraction results; committed source chunks and facts are retained.
Native per-call LLM timeouts, two workers/two slots, Qwen batch eight and six
threads, provider identity, OAuth and the forget gate remain unchanged.

Validation checks the native _wall_timeout_for against actual task payload
types (batch_retain), not just the JSON field. This is a timeout correction,
not evidence that the import has finished or a new throughput/ETA measurement.


## Resumable extraction and embeddings, 2026-10-18

A final live check still found 1571/4786 historical source chunks stored.
The new recovery version was therefore implemented and tested for this pinned
hindsight-api-slim 1.0.0 runtime. It is installed through the root-owned worker
startup adapter; native chunk ordering, fact conversion and database writes
are unchanged. The current running import was not replaced during development.

Extraction results are checkpointed per source chunk with Pydantic-validated
JSON; successful OpenAI-compatible embedding batches are checkpointed before
the next batch is requested. SQLite uses synchronous=FULL and private directory
0700/database 0600 under /var/lib/hindsight/checkpoints. These recovery payloads
contain extracted facts and vectors, not full prompts; they are distinct from
the content-free usage ledger. Checkpoint keys bind to the native operation,
source/input, relevant extraction configuration, model, dimensions and native
implementation hashes. Schema changes invalidate caches. Attachment-bearing
chunks bypass extraction caching because asset contents are not fingerprinted.
Only the installed default/public-schema worker path is supported.

The control-directory identity and mtime/ctime namespace fences cache use.
Creating or removing forget-pending invalidates the previous namespace, including
after a stopped retirement, so its cached facts cannot be replayed into a later
retain. Existing native forget gates and original-source suppression stay in
charge. A cached extraction returns zero new token usage; it does not create a
fabricated Luna call in the usage ledger. Invalid/corrupted data fails closed.

Each embedding plan reports planned/completed/remaining vector counts, last
checkpoint time and in_progress/complete/interrupted state. Duplicate inputs
are counted by multiplicity while vector order is preserved. These are recovery
counts, not committed Hindsight source chunks: the native delta write still
waits until the whole phase succeeds. A synthetic third-batch interruption after
16/24 vectors resumes with only eight vectors computed after reopening the DB.
Repeated successful calls reuse all batches. Checkpoints do not recover
uncommitted work from runs that predate installation.

Failure observations retain only worker, timestamp, phase and a fixed category.
Native poller failure/retry hooks also capture categories before retry clears
the native error field. Categories based on native error messages are heuristic;
raw messages, prompts and responses are not included in reports/history.
One failure can produce observations at more than one layer.

To activate, stop hindsight-history-import first and the shared hindsight-worker
unit second, then run install_retain_checkpoints.py as root with sibling sources.
It refuses active workers, checks the pinned package and native signatures,
installs root-owned code and initializes the private DB as hindsight. It reads
outstanding task retry counts and grants only the recovery allowance needed for
this controlled switch (current installation needs two rather than one).
It refuses an allowance above five. It does not reset task rows or start
services. Resume with the existing fixed resume helper. Current uncommitted
results are lost at the switch; cached future results survive subsequent retries.
Qwen batch eight/six threads, Luna, two workers/two slots and the 24-hour retain
ceiling remain configured through the existing runtime.

Report and cleanup (on the Pi):

    sudo -u hindsight /opt/hindsight/venv/bin/python /opt/hindsight/retain_checkpoints.py report
    sudo -u hindsight /opt/hindsight/venv/bin/python /opt/hindsight/retain_checkpoints.py prune

Prune reads native operation status and deletes only recovery data for completed/
cancelled operations or invalidated forget namespaces; it never deletes native
memories or source archives. Failed/incomplete operations keep their recovery
data. Failure-category history remains available after pruning.

Validation: 11 checkpoint tests plus five usage-ledger tests against installed
native interfaces, with synthetic data and no provider calls. Includes restart/
cancellation recovery, duplicate batches, model/operation isolation, corruption,
forget namespace fences, extraction reuse without token double counting, private
file modes and sanitized failure history. Live installer guard was checked
against running services and refused installation without changing them.

## OpenRouter restart and full-query timeout stop (2026-10-19)

The user explicitly requested replacing local embeddings with OpenRouter
qwen/qwen3-embedding-8b, a complete historical-import restart, and a recovery
point for the old run. Two synthetic requests verified 1024-dimensional output.
The model stays at 1024 dimensions for the existing pgvector schema, but lives
in a NEW database. Never mix the old 0.6B vectors with the new 8B vectors.
Luna/Codex OAuth remains the extraction provider; its numeric ledger continues.

switch_openrouter.py is a guarded, one-time Pi migration with snapshot, switch,
seed, report and rollback actions. Snapshot stops importer, workers, API and
local embeddings, preserves SQLite outbox/checkpoints using SQLite backup,
private runtime/state and original helpers, and exports/verifies a private
PostgreSQL custom archive. The ORIGINAL database stays present and unchanged.
Recovery checksums remain private. The new database is initialized by the API;
profile models are copied with NEW remote embeddings and no old vector or
old fact links. Existing SOUL.md/USER.md and raw conversation archives are not
rewritten. Twenty immutable source/profile jobs are reset for the new database.
Existing forgotten/suppressed sources block this migration. Historical text
already includes source dates; the enclosing retained snapshot now explicitly
uses timestamp=unset instead of inventing a new processing-time date on retry.
The original frozen snapshot still requires later catch-up before main cutover.

The user rejected search batching. The FINAL runtime therefore passes the
COMPLETE original arrays to the native ANN function exactly once. No slicing,
changed top-k, reduced similarity threshold or replacement search is installed.
Both DB command and statement limits are 120 seconds, and asyncio.timeout(120)
bounds the COMPLETE ANN operation (including its fact-type queries and setup).
A CPU-free counterprobe still reproduced the old 2853-vector timeout at the
previous 60-second SQL cap; it did not establish a physical hardware defect.

ann_timeout_stop.py writes a durable, content-free hold on ANN/task timeouts,
blocks further claims, and turns native timeout retries into terminal failure.
The root-owned fixed timeout_gate.py path action stops the importer and the
shared two-worker unit. Unit conditions and the resume helper refuse restart
while /var/lib/hindsight/import-timeout.hold exists. There is NO
automatic clearing or restart. This marker is outside the forget-control
directory, so stopping does not invalidate valid recovery checkpoints.

Install install_timeout_stop.py with its sibling sources only when importer,
workers and API are stopped. It pins native 1.0.0, validates interfaces, grants
only needed recovery allowance, installs the fixed path gate and updates the
private runtime. resume_scheduled_import.py starts local embeddings only when
the actual configured base URL is the original loopback embedding endpoint.
The local model/service remain installed for recovery, but stopped/disabled.

Five Linux unit tests verify full-array identity/single-call semantics, deadline
cancellation, private durable hold, suppressed native retries/claims and normal
non-timeout behavior. A synthetic marker tested the REAL systemd path action,
blocked starts of both real stopped units, and refusal by the resume helper;
only the synthetic marker was then removed. This is not an injected real error
or evidence that the complete import has finished. No monitoring loop runs.

Explicit rollback (leaves the restored old run stopped for inspection):

    sudo /opt/hindsight/venv/bin/python /opt/hindsight/maintenance/switch_openrouter.py rollback

After review, resume the restored runtime with the existing resume helper.
The old 0.6B database and private archive are preserved, not automatically deleted.

## Verified initial import and incremental catch-up (2026-10-19)

The initial 20 retain jobs and consolidation finished. All 18 historical source
bodies match the frozen outbox after native sanitization; the only byte-level
difference was removal of 170 NUL characters. Native streaming retain skips
persisting batches that produce no facts, so 4,182 stored historical chunks must
not be divided by the 4,786 pre-chunked inputs to report completion. All retain
jobs reported zero extraction errors. The verifier now uses the native mid
recall budget with 4,000 output tokens: all 17 fact-bearing sources passed a
source recall round-trip; one source produced no facts. This is a mechanical
coverage/retrieval check, not proof of complete semantic extraction.

catchup_history.py prepare freezes only unexported conversation tails, validates
all existing prefix fingerprints before queueing, and preserves immutable source
archives. Its private temporary plan survives interruption between queue commit
and manifest commit, then is removed after the outbox takes ownership. Repeating
preparation neither loses nor duplicates queued tails. Source changes, forget
requests and timeout holds fail closed. New jobs explicitly use timestamp=unset.
The initial manifest remains complete; a separate hindsight-history-catchup.json
tracks the incremental snapshot. Current catch-up: two segments, 577 messages,
1,566,899 characters. The existing importer unit runs catchup_history.py deliver
through a root-owned catchup.conf override, retaining all timeout-stop conditions.
The explicit rollback helper removes this override when restoring the old run.

The timeout marker is now /var/lib/hindsight/import-timeout.hold, private mode
0600 in the traversable data directory. The Nanobot user can test its existence;
the checkpoint directory remains 0700. The worker hook, path gate, unit conditions,
resume helper and rollback helper use the same location. Installer refuses either
an old or a new existing hold. A synthetic marker blocked both service starts
and the resume helper; only that synthetic hold was removed afterward.

Validation on the Pi: 319 existing targeted tests plus 10 subtests, four new
catch-up tests, and five full-query timeout tests passed. Checks include exact
incremental-tail delivery, idempotency, queue/manifest interruption recovery and
changed-prefix rejection. Test HOME and dependencies are isolated from production.
The production Nanobot package/configuration and profile files remain unchanged.
Catch-up completion, current USER.md reconciliation, durable sync/MCP wiring,
Dream shutdown and controlled live activation are still required before cutover.

## Two workers with four slots and four Luna calls each (2026-10-19)

The user explicitly requested 2x4 task slots and four concurrent Luna calls per
worker. configure_worker_capacity.py applies this only with importer and workers
stopped, refuses timeout/forget holds and phase-specific concurrency conflicts,
preserves runtime ownership/mode and grants only the native task-recovery
allowance needed for the controlled restart. It does not change task rows or
start services. Run it as root from /opt/hindsight/maintenance after stopping
the importer first and shared worker unit second, then resume those two units.

The persistent runtime values are HINDSIGHT_API_WORKER_MAX_SLOTS=4 and
HINDSIGHT_API_LLM_MAX_CONCURRENT=4. The existing supervisor still creates exactly
two workers. The native process-global LLM semaphore is shared by extraction,
consolidation and profile calls within each worker: up to eight calls in total,
not four per task. Remote embeddings retain batch8/max-concurrency1, and full
ANN plus the 120-second timeout stop remain unchanged. Native recovery resumes
unfinished tasks; uncommitted streaming work can enter the delta/checkpoint path
and its next durable write need not happen immediately after restart.

## Full ANN planner fix, validated and activated 2026-10-20

The failed 910-vector delta input was reconstructed from operation-scoped cached
extraction and embeddings, with provider fallback forbidden and a matching full
embedding-plan/order fingerprint: 855 world and 55 experience vectors. Actual
native top_k is 50, not the 20 used in the earlier representative EXPLAIN. Tests
kept ef_search60, iterative_scanoff, threshold and ALL vectors unchanged.

With workers stopped, the original query timed out at120.076s and again at
120.015s after warming caches. Its world plan repeatedly scans/sorts approximately
6609 candidates per seed. A transaction-local enable_sortoff preference selects
HNSW for both native fact-type queries: complete probe28.794s, actual patched
wrapper7.271s with warm caches. Their returned link sets AND scores match.

ann_timeout_stop now opens a transaction/savepoint around the complete native
call, reads the prior enable_sort value, sets only that local planner preference,
then explicitly restores it on success (including inside caller transactions).
Failure/cancellation rolls back the scope. The original full arrays are passed
once. No search breadth, threshold, top-k or embedding settings are reduced.
The existing120s deadline covers setup, queries and restoration; durable timeout
stop/claim/retry protections remain. It does not change global PostgreSQL config.

HNSW is approximate: against a full exact cosine reference, returned links cover
26474/26537 exact links (99.7626%); 63 exact-reference links are absent and 47/910
seeds have a differing set. This is measured nearest-neighbour link recall, not
semantic/factual accuracy. Source facts are not dropped by this comparison.
It must not be described as identical to an exhaustive scan. Scores agree with
the reference within6.23e-7. See ann-plan-fix-report.json for numeric evidence.

23 focused timeout/checkpoint/usage tests passed, including deadline coverage of
planner setup, preservation of initially enabled/disabled sort, rollback on
errors and no retry under hold. The guarded install_ann_plan_fix.py requires
stopped workers, matching real timeout evidence, pinned native1.0.0 and a passing
full patched replay. It installs source but preserves the hold and starts no
services. After review, timeout evidence was preserved privately under usage,
the reviewed hold cleared and workers/importer resumed at10:23:41UTC. Existing
2x4 slots/four Luna calls each remain. Original recovery operation is retained.
Main Nanobot package/config/profiles remain untouched; full cutover is pending.

Diagnostics are maintenance-only, not alternate production embedding or ANN
implementations. Raw diagnostic vectors/links remain private; public reports
contain aggregate data only. No model requests or persistent memory writes
occurred during query diagnostics. Access after the user reboot used the Pi's
resolved IPv6 address with its existing IPv4 HostKeyAlias and strict key checking;
no SSH trust checks or network configuration were weakened.


### Production memory cutover (2026-10-20)

Nanobot now uses Hindsight for long-term recall, durable evidence delivery,
targeted fact forgetting and saved-profile projection. SOUL.md and USER.md remain
in every normal conversation context. Legacy MEMORY.md injection and scheduled/
manual Dream writers are disabled in Hindsight mode; conversation compaction
and original archives remain in place.

The installed system unit is nanobot-memory-sync.service, running as nanobot
with NoNewPrivileges and mode0077. It uses the existing outbox and profile state,
the shared forget marker, and /var/lib/hindsight/import-timeout.hold. That hold
blocks submission/retry and startup, and the root-owned timeout gate stops the
sync service before stopping the importer and both workers. Do not clear a real
timeout hold without reviewing its evidence.

New outbox segments explicitly use native timestamp "unset": source messages
carry their own dates, and retries must not invent a changing event date.
The pre-existing configured main model and LangSmith launcher are preserved.
Hindsight keeps Luna, remote Qwen3 embeddings and the unchanged full ANN deadline.

Activation required reconciling USER.md drift and repairing incomplete generated
profiles. Saved queries contain the private inherited baseline, with explicit
user corrections/forgetting taking precedence and source text treated as data.
No private profile text is in this repository. A final content-free semantic
comparison reported no missing standing information or contradictions; this is
an automated consistency check, not semantic proof. The normal exporter then
projected both reviewed models and verified their state hashes.

Validation: 200 focused tests plus10subtests passed on the Pi; actual installed
AgentLoop memory registration/observer/profile context checks passed; actual
scoped MCP recall and saved-model read passed; Discord connected after restart
with0service restarts. All23 source outbox jobs completed. Consolidation and its
final historical verification continue normally in the background; activation
does not imply every derived observation has finished.

install_memory_cutover.py is a one-time guarded installer, not a general updater:
it requires the exact private review receipt, stopped gateway, matching model/
local hashes and no timeout/forget hold. verify_memory_cutover.py checks the
installed integration and live scoped reads. reconcile_cutover_profiles.py emits
only content-free line references. The existing pre-embedding recovery point
remains intact; stop/deactivate production Hindsight sync before restoring an
older database/runtime.
