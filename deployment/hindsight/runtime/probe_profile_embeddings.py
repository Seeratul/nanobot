"""Check real profile inputs locally; emit counts only, never content or vectors."""
import asyncio
import json
from pathlib import Path

import httpx


async def main():
    workspace = Path('/home/nanobot/.nanobot/workspace')
    results = []
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8891', timeout=120,
                                 trust_env=False) as client:
        for name in ('SOUL.md', 'USER.md'):
            text = (workspace / name).read_text()
            encoded = await client.post('/tokenize', json={'content': text})
            encoded.raise_for_status()
            response = await client.post('/v1/embeddings', json={
                'model': 'qwen3-embedding-0.6b-q8_0', 'input': text})
            response.raise_for_status()
            data = response.json()
            results.append({'file': name, 'tokens': len(encoded.json()['tokens']),
                            'dimensions': len(data['data'][0]['embedding']),
                            'reported_input_tokens': data.get('usage', {}).get('prompt_tokens')})
    print(json.dumps({'local_profile_embedding_probe': results}))


asyncio.run(main())
