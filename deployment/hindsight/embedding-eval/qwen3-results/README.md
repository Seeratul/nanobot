# Qwen3 embedding test on Felix's Raspberry Pi

Measured 2026-10-15 on the actual Raspberry Pi 4 (4 GB RAM, Cortex-A72,
USB SSD), alongside the running Nanobot gateway. Hindsight/PostgreSQL were
not running. This is a small synthetic smoke test, not a production benchmark.

Model: `Qwen/Qwen3-Embedding-0.6B-GGUF`, Q8_0, 1024 dimensions.
Pinned model/source identities and checksums are in `manifest.json`.
The model and build artifacts were moved from RAM-backed `/tmp` to SSD-backed
`/var/tmp` before measurement. Requests stayed on `127.0.0.1:8891`.

| Measurement | 2 threads | 4 threads |
|---|---:|---:|
| Expected top-1 result, 14 queries / 18 synthetic records | 14/14 | 14/14 |
| Median query latency | 3.14 s | 1.65 s |
| Maximum query latency | 3.50 s | 1.83 s |
| Long passage, 310 tokens | 28.94 s | 15.49 s |
| Peak server RSS | 916.38 MiB | 916.37 MiB |
| Minimum system MemAvailable | 2243 MiB | 2283 MiB |
| Additional swap consumption | 0 MiB | 0 MiB |
| Maximum sampled temperature | 72.1 C | 80.3 C |

The four-thread run reused document vectors from the two-thread run, but
recomputed every query and the long passage. Startup times (14.08 s / 4.06 s)
are not comparable cold-start measurements because filesystem caches were warm
for the second run. RSS includes mapped model pages; MemAvailable accounts for
reclaimable file cache, so these figures must not be subtracted directly.

Use two threads as the initial background-ingestion setting. Four threads are
an option for interactive latency after cooling and combined-load testing.
The 80.3 C peak makes sustained four-thread ingestion a poor default on the
current cooling setup. A post-test `vcgencmd get_throttled` returned `0x0`;
throttle flags were not sampled continuously.

Tests used bilingual fictional records with related distractors and cosine
ranking, not Hindsight's complete retrieval pipeline. Quality on real memory,
large imports, simultaneous bot activity, full-stack RAM and budget/quota
behavior remain untested. Batch size and concurrency are deliberately one in
the candidate Hindsight configuration. No real conversations were processed.

The transient server shut down after both runs; port 8891 was no longer
listening and Nanobot's user service remained active. No production memory
configuration or profiles changed. This test supports trying Qwen3 in the
isolated Hindsight stack; it does not establish production readiness.
