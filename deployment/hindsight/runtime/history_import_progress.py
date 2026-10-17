"""Safe, one-shot progress counters for the initial historical import."""
import asyncio
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path('/home/nanobot/.nanobot/sessions/8679b657a17401f5f41c32a247d800ee')

async def main():
    with sqlite3.connect(ROOT / 'hindsight-outbox.sqlite3') as db:
        jobs = list(db.execute('SELECT state,operation_id,created_at FROM jobs '
                               "WHERE session_key NOT LIKE 'profile:%'"))
    state_path = ROOT / 'hindsight-history-import.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    progress = []
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8888/v1/default/banks/personal',
                                 timeout=30, trust_env=False) as client:
        async def inspect(identity):
            response = await client.get('/operations/' + identity)
            if response.status_code == 404:
                return
            response.raise_for_status()
            data = response.json()
            item = {'status': data.get('status'), 'type': data.get('operation_type')}
            fields = data.get('progress') or {}
            if fields:
                item['progress'] = {key: fields.get(key) for key in ('stage', 'processed', 'total', 'detail')}
            progress.append(item)
            for child in data.get('child_operations') or []:
                await inspect(child['operation_id'])
        for status, operation, created in jobs:
            if status != 'pending':
                await inspect(operation)
        stats_response = await client.get('/stats')
        stats_response.raise_for_status()
        stats = stats_response.json()
    print(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'stage': state.get('stage'),
        'outbox': dict(Counter(row[0] for row in jobs)),
        'elapsed_seconds': round(datetime.now(timezone.utc).timestamp() - min(row[2] for row in jobs), 1) if jobs else None,
        'documents': stats.get('total_documents'), 'facts_by_type': stats.get('nodes_by_fact_type'),
        'native_operations': stats.get('operations_by_status'), 'operation_progress': progress}), flush=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print(json.dumps({'status': 'attention', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
