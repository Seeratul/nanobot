"""Compare full ANN output with a full exact cosine reference; emit aggregates only."""
import asyncio
import collections
import json
import os
import time
import zlib
from pathlib import Path

os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'

import asyncpg
import numpy as np

ROOT = Path('/var/tmp/hindsight-ann-replay-20261019')


async def main():
    payload = json.loads(zlib.decompress((ROOT / 'inputs.json.z').read_bytes()))
    links = json.loads(zlib.decompress((ROOT / 'index-links.json.z').read_bytes()))
    actual = collections.defaultdict(dict)
    for source, target, _, score, _ in links:
        actual[int(source)][target] = score
    config = json.loads(Path('/var/lib/hindsight/runtime.json').read_text())
    conn = await asyncpg.connect(config['HINDSIGHT_API_DATABASE_URL'], timeout=15)
    totals = collections.Counter()
    max_score_difference = 0.0
    started = time.monotonic()
    try:
        for fact_type in sorted(set(payload['types'])):
            indices = [i for i, value in enumerate(payload['types']) if value == fact_type]
            rows = await conn.fetch(
                'SELECT id::text,embedding::text FROM memory_units '
                'WHERE bank_id=$1 AND fact_type=$2 AND embedding IS NOT NULL',
                payload['bank'], fact_type)
            ids = [row[0] for row in rows]
            positions = {value: i for i, value in enumerate(ids)}
            matrix = np.asarray([json.loads(row[1]) for row in rows], dtype=np.float64)
            seeds = np.asarray([payload['vectors'][i] for i in indices], dtype=np.float64)
            matrix /= np.linalg.norm(matrix, axis=1)[:, None]
            seeds /= np.linalg.norm(seeds, axis=1)[:, None]
            # All seeds of this native fact type against all stored candidates.
            # This is a diagnostic reference, not a replacement production path.
            scores = np.clip(seeds @ matrix.T, 0.0, 1.0)
            top_k = min(50, len(rows))
            for local_index, source in enumerate(indices):
                score = scores[local_index]
                best = np.argpartition(score, -top_k)[-top_k:]
                expected = {ids[i] for i in best if score[i] >= payload['threshold']}
                found = set(actual[source])
                totals['exact_links'] += len(expected)
                totals['ann_links'] += len(found)
                totals['exact_links_found'] += len(expected & found)
                totals['missing_exact_links'] += len(expected - found)
                totals['seeds'] += 1
                totals['seeds_with_exact_set'] += expected == found
                for target, actual_score in actual[source].items():
                    if target not in positions:
                        raise RuntimeError('ANN returned a candidate outside its fact type')
                    difference = abs(float(score[positions[target]]) - actual_score)
                    max_score_difference = max(max_score_difference, difference)
    finally:
        await conn.close()
    result = {**dict(totals), 'exact_link_recall':
              totals['exact_links_found'] / totals['exact_links'] if totals['exact_links'] else 1.0,
              'max_cosine_score_difference': max_score_difference,
              'seconds': round(time.monotonic() - started, 3),
              'model_calls': 0, 'persistent_memory_writes': 0}
    descriptor = os.open(ROOT / 'quality-report.json', os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(result, handle)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
