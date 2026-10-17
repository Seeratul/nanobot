"""Read-only profile/runtime inventory; never emit private profile text."""
import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, '/tmp/nanobot-hindsight-dev')
import httpx
from nanobot.memory_sync.adapter import _SECRET
from nanobot.session.manager import SessionManager


async def main():
    workspace = Path('/home/nanobot/.nanobot/workspace')
    profiles = {}
    for name in ('SOUL.md', 'USER.md', 'memory/MEMORY.md'):
        path = workspace / name
        text = path.read_text() if path.exists() else ''
        profiles[name] = {'exists': path.exists(), 'chars': len(text),
                          'sha256': hashlib.sha256(text.encode()).hexdigest(),
                          'potential_secret_matches': len(_SECRET.findall(text))}
    sessions = SessionManager(workspace)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        response = await client.get('http://127.0.0.1:8888/v1/default/banks')
        response.raise_for_status()
        raw = response.json()
        banks = raw.get('banks', raw.get('items', []))
        print(json.dumps({'profiles': profiles, 'sessions_dir': str(sessions.sessions_dir),
                          'banks': [row.get('bank_id', row.get('id')) for row in banks]}))


asyncio.run(main())
