"""Measure native chunks using the actual local Qwen tokenizer, content stays private."""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
from hindsight_api.engine.retain.fact_extraction import chunk_text

ROOT = Path('/home/nanobot/.nanobot/sessions/8679b657a17401f5f41c32a247d800ee')



def write_private_report(path, data):
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(data, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

async def main():
    plan = json.loads((ROOT / 'hindsight-history-plan.json').read_text())
    chunks = []
    for session in plan['sessions']:
        text = '\n\n'.join(m for m in session['messages'] if m)
        chunks.extend(chunk_text(text, 3000))
    adversarial = [('code', ('x = {"matrix": [1,2,3], "node": "abc"}\n' * 100)[:3000]),
                   ('unicode', ('漢字🙂🧪Überprüfung Ελληνικά ' * 200)[:3000])]
    digest = hashlib.sha256((ROOT / 'hindsight-history-plan.json').read_bytes()).hexdigest()
    cache = ROOT / 'hindsight-history-token-inventory.json'
    cached = json.loads(cache.read_text()) if cache.exists() else {}
    worst = cached.get('counts', []) if cached.get('plan_sha256') == digest else []
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8891', timeout=900, trust_env=False) as client:
        if len(worst) != len(chunks):
            worst = []
            for index, text in enumerate(chunks):
                response = await client.post('/tokenize', json={'content': text})
                response.raise_for_status()
                tokens = len(response.json()['tokens'])
                worst.append((tokens, index))
                if (index + 1) % 1000 == 0:
                    print(json.dumps({'component': 'embedding_probe', 'tokenized_chunks': index + 1}), flush=True)
            write_private_report(cache, {'plan_sha256': digest, 'counts': worst})
        tested = []
        for label, text in [(f'actual_chunk_rank_{rank}', chunks[index]) for rank, (_, index)
                            in enumerate(sorted(worst, reverse=True)[:1])] + adversarial[1:]:
            response = await client.post('/tokenize', json={'content': text})
            response.raise_for_status()
            tokens = len(response.json()['tokens'])
            print(json.dumps({'component': 'embedding_probe', 'case': label, 'tokens': tokens, 'status': 'started'}), flush=True)
            started = time.monotonic()
            response = await client.post('/v1/embeddings', json={
                'model': 'qwen3-embedding-0.6b-q8_0', 'input': text})
            item = {'case': label, 'chars': len(text), 'tokens': tokens, 'http_status': response.status_code, 'seconds': round(time.monotonic() - started, 3)}
            if response.is_success:
                data = response.json()
                item.update(dimensions=len(data['data'][0]['embedding']),
                            reported_input_tokens=data.get('usage', {}).get('prompt_tokens'))
            tested.append(item)
    report = {'native_chunks': len(chunks), 'max_chunk_chars': max(map(len, chunks)),
        'max_actual_tokens': max(worst)[0], 'actual_chunks_over_1023_tokens': sum(t > 1023 for t, _ in worst),
        'embedding_cases': tested}
    report['plan_sha256'] = hashlib.sha256((ROOT / 'hindsight-history-plan.json').read_bytes()).hexdigest()
    report['passed'] = all(item['http_status'] == 200 and item.get('dimensions') == 1024
                          and item.get('reported_input_tokens') in (item['tokens'], item['tokens'] + 1)
                          and item['seconds'] < 600
                          for item in tested)
    output = ROOT / 'hindsight-history-embedding-report.json'
    write_private_report(output, report)
    print(json.dumps(report), flush=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print(json.dumps({'status': 'attention', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
