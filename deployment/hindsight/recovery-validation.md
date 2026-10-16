# Multi-round recovery validation, 2026-10-16

The runtime checkpoint previously replaced each completed tool exchange with the
latest exchange. A crash during a later iteration could therefore lose earlier
results from the restored public transcript.

The runner now includes the preceding messages of the current run in the existing
checkpoint sidecar. It excludes initial history and retains original tool results
across provider-native compaction. It does not introduce a second full journal.
Recovery validates complete prior tool exchanges, deduplicates an already saved
tail, acknowledges restored queued follow-ups, and preserves runtime-context
markers so injected private context can be removed from public history.
Malformed prior messages prevent offering automatic continuation and invalidate
provider-native state. Legacy checkpoints without prior messages still load.
A checkpoint can recover only data already written at a checkpoint boundary;
this does not provide exactly-once execution for external side effects in flight.

## Validation

Executed on the Pi in /tmp/nanobot-hindsight-dev, using a temporary HOME and the
installed Nanobot Python interpreter, without installing the feature into the
production bot:

- 393 tests and 10 subtests passed across runner core, turn persistence, session
  storage/recovery, governance, injections, compaction, outbox, worker, service,
  and profile projection.
- New disk-reload tests interrupt the second exchange before execution, after
  execution, and after the final response; each runs with and without a follow-up.
  Earlier results survive, unfinished calls become interrupted results, and
  restoration itself does not execute tools. A second restoration is a no-op.
- Corrupt prior messages, incomplete exchanges, saved-tail overlap, follow-up
  acknowledgement, and runtime-context visibility have focused regression tests.
- Both native-compaction scopes additionally restore the original tool exchange
  and final response from the last checkpoint (44 governance tests passed after
  adding these assertions; these are part of the 393-test selection, not 44 extra
  distinct tests).
- Ruff passed for all changed Python files. Strict BasedPyright checked the two
  changed production modules: zero errors and warnings.
- Production user service nanobot-discord remained active. No production source,
  configuration, profile or conversation data was modified.

## Still required before production cutover

Full remember/forget, remote in-flight cancellation/deletion, profile invalidation,
durable service deployment, MCP configuration, Dream removal and history import.
Hindsight cancellation is cooperative: a processing batch may still finish after
its operation row becomes cancelled. Do not claim forgetting is complete merely
from cancellation acknowledgement followed by one document deletion. An isolated
race test and a proven quiescence/deletion protocol are required.
