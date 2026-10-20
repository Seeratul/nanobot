"""Content-free verification of the installed integration and live MCP reads."""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import Config
from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.profiles import ProfileExporter
from nanobot.memory_sync.worker import HindsightWorker
from nanobot.security.network import configure_ssrf_whitelist

HOME = Path('/home/nanobot')
ROOT = HOME / '.nanobot/sessions/8679b657a17401f5f41c32a247d800ee'
WS = HOME / '.nanobot/workspace'


async def main():
    cfg = Config.model_validate(json.loads((HOME / '.nanobot/config.json').read_text()))
    cfg.bind_source_path(HOME / '.nanobot/config.json')
    assert cfg.agents.defaults.hindsight.enabled and not cfg.agents.defaults.dream.enabled
    assert cfg.tools.mcp_servers['hindsight'].enabled_tools == ['recall', 'get_mental_model']
    configure_ssrf_whitelist(cfg.tools.ssrf_whitelist)
    loop = AgentLoop.from_config(cfg, provider=AsyncMock(), tool_registry=ToolRegistry())
    assert loop.memory_outbox is not None and loop.memory_exporter is not None
    assert loop.memory_outbox.path == ROOT / 'hindsight-outbox.sqlite3'
    assert loop.tools.get('memory') is not None
    assert not loop.context.legacy_memory_enabled
    prompt = loop.context.build_system_prompt(channel='discord')
    for name in ['SOUL.md', 'USER.md']:
        assert (WS / name).read_text().strip() in prompt
    assert '# Hindsight Memory' in prompt
    assert '## Long-term Memory' not in prompt
    print(json.dumps({'runtime_configuration': True, 'memory_tool_registered': True,
                      'archive_observer_active': True, 'both_profiles_in_context': True,
                      'legacy_memory_injection_disabled': True}), flush=True)
    worker = HindsightWorker(MemoryOutbox(ROOT / 'hindsight-outbox.sqlite3', bank_id='personal',
                             pause_path=Path('/var/lib/hindsight/control/forget-pending')),
                             api_url='http://127.0.0.1:8888', bank_id='personal',
                             hold_path=Path('/var/lib/hindsight/import-timeout.hold'))
    exporter = ProfileExporter(worker, WS, ROOT / 'hindsight-profiles.json')
    projection = await exporter.sync_once()
    assert all(v in ('updated', 'unchanged') for v in projection.values())
    print(json.dumps({'profile_projection': projection}), flush=True)
    async with httpx.AsyncClient(base_url=worker.api_url + worker.prefix, timeout=120,
                                 trust_env=False) as client:
        response = await client.get('/memories/list', params={'type': 'world', 'limit': 1})
        response.raise_for_status()
        fact = response.json()['items'][0]
        query = fact['text']
    async with streamablehttp_client('http://127.0.0.1:8888/mcp/personal/') as transport:
        async with ClientSession(transport[0], transport[1]) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {'recall', 'get_mental_model'}
            recall = await session.call_tool('recall', {'query': query, 'budget': 'mid', 'max_tokens': 4000})
            assert not recall.isError
            texts = ''.join(c.text for c in recall.content if c.type == 'text')
            data = json.loads(texts)
            # The actual recalled source stays inside the process.
            results = data.get('results', [])
            assert results
            exact = any(r.get('id') == fact['id'] or r.get('text', '').strip().casefold() == query.strip().casefold()
                        for r in results)
            assert exact
            model = await session.call_tool('get_mental_model', {'mental_model_id': 'user-profile'})
            assert not model.isError and model.content
            print(json.dumps({'scoped_mcp_tools': sorted(t.name for t in tools.tools),
                              'live_recall_round_trip': exact, 'saved_profile_read': True}), flush=True)
    state = json.loads((ROOT / 'hindsight-profiles.json').read_text())
    assert all(hashlib.sha256((WS / name).read_bytes()).hexdigest() == state[mid]['content_hash']
               for mid, name in [('agent-soul', 'SOUL.md'), ('user-profile', 'USER.md')])
    print(json.dumps({'profile_state_matches_files': True}), flush=True)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print(json.dumps({'status': 'needs_attention', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
