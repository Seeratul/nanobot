"""Verify eight-slot embedding batching without exposing any private source text."""
import asyncio
import json
import math
import sqlite3
import time
from pathlib import Path

import httpx
from hindsight_api.engine.retain.fact_extraction import chunk_text

ROOT = Path('/home/nanobot/.nanobot/sessions/8679b657a17401f5f41c32a247d800ee')
SAMPLES = [
    'Synthetic test: Mira prefers short answers in German.',
    'Synthetic test: The Atlas application stores records in PostgreSQL.',
    'Synthetic test: Jonas plays piano on Thursday afternoons.',
    'Synthetic test: The deployment uses a blue container with two workers.',
    'Synthetic test: The garden has three apple trees and one pear tree.',
    'Synthetic test: A project review is scheduled for September twenty third.',
    'Synthetic test: The assistant should ask before sending external messages.',
    'Synthetic test: The robot has a temperature sensor and a red indicator.',
]


def cosine(left, right):
    return sum(a*b for a, b in zip(left, right, strict=True)) / math.sqrt(
        sum(a*a for a in left) * sum(b*b for b in right))


async def main():
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8891', timeout=600, trust_env=False) as client:
        for _ in range(60):
            try:
                response = await client.get('/health', timeout=5)
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(1)
        else:
            raise ValueError('embedding not ready')
        slots_response = await client.get('/slots')
        slots_response.raise_for_status()
        slots = slots_response.json()
        assert len(slots) == 8 and all(slot['n_ctx'] >= 4096 for slot in slots)
        print(json.dumps({'stage': 'slots_verified', 'slots': len(slots),
                          'context_per_slot': min(slot['n_ctx'] for slot in slots)}), flush=True)
        started = time.monotonic()
        singles = []
        for text in SAMPLES:
            response = await client.post('/v1/embeddings', json={
                'model': 'qwen3-embedding-0.6b-q8_0', 'input': text})
            response.raise_for_status()
            singles.append(response.json()['data'][0]['embedding'])
        serial_seconds = time.monotonic() - started
        started = time.monotonic()
        response = await client.post('/v1/embeddings', json={
            'model': 'qwen3-embedding-0.6b-q8_0', 'input': SAMPLES})
        response.raise_for_status()
        batch = response.json()
        batch_seconds = time.monotonic() - started
        vectors = sorted(batch['data'], key=lambda item: item['index'])
        assert [item['index'] for item in vectors] == list(range(8))
        assert all(len(item['embedding']) == 1024 for item in vectors)
        minimum_cosine = min(cosine(a, b['embedding']) for a, b in zip(singles, vectors, strict=True))
        assert minimum_cosine > 0.999
        print(json.dumps({'stage': 'short_batch_verified', 'serial_seconds': round(serial_seconds, 3),
                          'batch8_seconds': round(batch_seconds, 3),
                          'observed_speedup': round(serial_seconds / batch_seconds, 3),
                          'minimum_cosine_to_serial': minimum_cosine}), flush=True)
        # Reconstruct the exact measured initial source-chunk order from the
        # existing private outbox, without writing another transcript copy.
        cache = json.loads((ROOT / 'hindsight-history-token-inventory.json').read_text())
        maximum_tokens, maximum_index = max(cache['counts'])
        chunks_seen = 0
        long_text = None
        with sqlite3.connect(ROOT / 'hindsight-outbox.sqlite3') as db:
            payloads = db.execute("SELECT payload FROM jobs WHERE session_key NOT LIKE 'profile:%' ORDER BY created_at").fetchall()
        for (payload,) in payloads:
            chunks = chunk_text(json.loads(payload)['items'][0]['content'], 3000)
            if chunks_seen <= maximum_index < chunks_seen + len(chunks):
                long_text = chunks[maximum_index - chunks_seen]
                break
            chunks_seen += len(chunks)
        assert long_text is not None
        tokenized = await client.post('/tokenize', json={'content': long_text})
        tokenized.raise_for_status()
        assert len(tokenized.json()['tokens']) == maximum_tokens == 2120
        mixed = [long_text] + SAMPLES[:7]
        token_counts = []
        for text in mixed:
            response = await client.post('/tokenize', json={'content': text})
            response.raise_for_status()
            token_counts.append(len(response.json()['tokens']))
        print(json.dumps({'stage': 'long_mixed_batch_started', 'texts': 8,
                          'largest_input_tokens': maximum_tokens}), flush=True)
        started = time.monotonic()
        response = await client.post('/v1/embeddings', json={
            'model': 'qwen3-embedding-0.6b-q8_0', 'input': mixed})
        response.raise_for_status()
        data = response.json()
        long_seconds = time.monotonic() - started
        vectors = sorted(data['data'], key=lambda item: item['index'])
        assert [item['index'] for item in vectors] == list(range(8))
        assert all(len(item['embedding']) == 1024 for item in vectors)
        assert data['usage']['prompt_tokens'] == sum(token_counts) + 8
        assert long_seconds < 600
        report = {'passed': True, 'slots': 8, 'context_per_slot': 4096, 'shared_context_pool_tokens': 8192,
                  'serial_short_seconds': round(serial_seconds, 3),
                  'batch8_short_seconds': round(batch_seconds, 3),
                  'short_observed_speedup': round(serial_seconds / batch_seconds, 3),
                  'minimum_cosine_to_serial': minimum_cosine,
                  'long_mixed_batch_seconds': round(long_seconds, 3),
                  'longest_real_input_tokens': maximum_tokens,
                  'mixed_total_reported_tokens': data['usage']['prompt_tokens'],
                  'token_counts_match_without_truncation': True, 'dimensions': 1024}
        path = ROOT / 'hindsight-batch8-report.json'
        temp = path.with_suffix('.tmp')
        import os
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(report, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        print(json.dumps(report), flush=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print(json.dumps({'status': 'needs_attention', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
