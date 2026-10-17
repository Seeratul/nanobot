import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, '/opt/hindsight/nanobot-import')
import httpx
from filelock import FileLock
from hindsight_api.engine.retain.fact_extraction import chunk_text
from hindsight_api.engine.token_encoding import count_tokens

from nanobot.session.manager import _migrate_legacy_exec_session_records

ROOT = Path('/home/nanobot/.nanobot/sessions/8679b657a17401f5f41c32a247d800ee')

async def main():
    plan = json.loads((ROOT / 'hindsight-history-plan.json').read_text())
    migrated = 0
    max_native_tokens = 0
    with FileLock(ROOT / '.session-files.lock', timeout=30):
        for source in plan['sessions']:
            if not source['source_file'].endswith('.jsonl'):
                continue
            records = [json.loads(line) for line in (ROOT / source['source_file']).read_text().splitlines() if line.strip()]
            messages = [row for row in records if row.get('role') in {'user', 'assistant', 'tool'} and not row.get('_type')]
            header = next(row for row in records if row.get('_type') == 'metadata')
            migrated += bool(_migrate_legacy_exec_session_records(messages, header.get('metadata', {})))
    for source in plan['sessions']:
        for chunk in chunk_text('\n\n'.join(m for m in source['messages'] if m), 3000):
            max_native_tokens = max(max_native_tokens, count_tokens(chunk))
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8888/v1/default/banks/personal', trust_env=False) as client:
        response = await client.get('/memories/list', params={'limit': 1})
        response.raise_for_status()
        data = response.json()
        rows = data.get('items', data.get('results', data.get('memories', [])))
        print(json.dumps({'memory_list_keys': list(data), 'fact_keys': list(rows[0]) if rows else [],
                          'session_legacy_migrations_needed': migrated,
                          'max_native_cap_tokenizer_tokens': max_native_tokens}))
    assert migrated == 0
    assert max_native_tokens < 8192

asyncio.run(main())
