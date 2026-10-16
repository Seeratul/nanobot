"""Select one fact from a shared source, preserve another, then regenerate profiles."""
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, '/tmp/nanobot-hindsight-dev')
import httpx
from nanobot.memory_sync.forget import ForgetWorker, SystemdWorkerControl
from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.profiles import ProfileExporter
from nanobot.memory_sync.worker import HindsightWorker

ROOT = Path('/var/tmp/hindsight-fact-forget-v4')
PAUSE = Path('/var/lib/hindsight/control/forget-pending')
URL = 'http://127.0.0.1:8888'
BANK = 'synthetic-fact-forget'
BASE = '/v1/default/banks/' + BANK


async def main():
    assert 'NoNewPrivs:\t1' in Path('/proc/self/status').read_text()
    ROOT.mkdir(mode=0o700, exist_ok=True)
    control = SystemdWorkerControl()
    async with httpx.AsyncClient(base_url=URL, timeout=180, trust_env=False) as client:
        ready_deadline = time.monotonic() + 120
        while True:
            try:
                ready = await client.get('/health')
                if ready.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < ready_deadline, 'API readiness timeout'
            await asyncio.sleep(2)
        PAUSE.touch()
        await control.stop()
        response = await client.delete(BASE)
        assert response.status_code in (200, 404)
        (await client.put(BASE, json={'name': 'Fictional selective forgetting'})).raise_for_status()
        PAUSE.unlink()
        await control.start()
        outbox = MemoryOutbox(ROOT / 'outbox.sqlite3', bank_id=BANK, pause_path=PAUSE)
        document = outbox.enqueue('synthetic-shared-source', 'cli:fictional', ['v1'], [
            'user [2026-10-15T12:00:00Z]: My name is Mira. I live in Westlake.\n\n'
            'user [2026-10-16T12:00:00Z]: A separate personal fact: I play the cello.'
        ])
        worker = HindsightWorker(outbox, api_url=URL, bank_id=BANK)
        assert await worker.run_once() == 'submitted'
        operation = str(uuid5(NAMESPACE_URL, document))
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            state = (await client.get(BASE + '/operations/' + operation)).json()['status']
            assert state not in ('failed', 'cancelled')
            if state == 'completed':
                break
            await asyncio.sleep(2)
        assert state == 'completed'
        response = await client.get(BASE + '/memories/list', params={'document_id': document, 'limit': 100})
        response.raise_for_status()
        data = response.json()
        facts = data.get('items', data.get('results', data.get('memories', [])))
        targets = [fact for fact in facts if 'westlake' in fact['text'].lower()]
        keep = [fact for fact in facts if 'cello' in fact['text'].lower()
                and 'westlake' not in fact['text'].lower()]
        assert targets and keep, 'fixture needs separate town and instrument facts'
        assert not any('cello' in fact['text'].lower() for fact in targets)
        workspace = ROOT / 'workspace'
        workspace.mkdir()
        for name in ('SOUL.md', 'USER.md'):
            (workspace / name).write_text('Synthetic profile: Mira lives in Westlake and plays cello.')
        exporter = ProfileExporter(worker, workspace, ROOT / 'profiles.json')
        exporter.register_baseline()
        target_ids = [fact['id'] for fact in targets]
        keep_ids = {fact['id'] for fact in keep}
        outbox.request_fact_forget(document, target_ids)
        assert await ForgetWorker(worker, exporter, control).run_once() == 'forgotten'
        assert (workspace / 'USER.md').read_text() == '# User\n'
        assert (await client.get(BASE + '/documents/' + document)).status_code == 200
        # Restored objects verify durable targets/source quarantine after restart.
        reopened = MemoryOutbox(outbox.path, bank_id=BANK)
        assert reopened.fact_targets('synthetic-shared-source') == sorted(target_ids)
        assert reopened.enqueue('synthetic-shared-source', 'cli:fictional', ['v1'], ['original']) is None
        response = await client.get(BASE + '/memories/list', params={'document_id': document, 'limit': 100})
        remaining = response.json().get('items', [])
        assert keep_ids.issubset({fact['id'] for fact in remaining})
        assert not any('westlake' in fact['text'].lower() for fact in remaining)
        response = await client.post(BASE + '/memories/recall', json={
            'query': 'Where does Mira live and which instrument does she play?',
            'budget': 'low', 'max_tokens': 1000})
        response.raise_for_status()
        recalled = response.json()
        assert 'westlake' not in json.dumps(recalled).lower()
        assert 'cello' in json.dumps(recalled).lower()
        # Wait for the newly recreated saved profiles; their internal recall
        # must not reuse the original paragraph containing the forgotten fact.
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            user = (await client.get(BASE + '/mental-models/user-profile')).json()
            soul = (await client.get(BASE + '/mental-models/agent-soul')).json()
            if (user.get('content') and soul.get('content')
                    and not user.get('last_refresh_failed_at') and not soul.get('last_refresh_failed_at')):
                break
            await asyncio.sleep(5)
        assert user.get('last_refreshed_at') and soul.get('last_refreshed_at')
        assert 'westlake' not in (user.get('content', '') + soul.get('content', '')).lower()
        assert 'cello' in user['content'].lower()
        await exporter.sync_once()
        assert 'cello' in (workspace / 'USER.md').read_text().lower()
        assert 'westlake' not in (workspace / 'USER.md').read_text().lower()
        report = {'no_new_privileges': True, 'selected_facts_retired': len(target_ids),
                  'other_fact_ids_preserved': len(keep_ids), 'shared_document_preserved': True,
                  'reimport_blocked': True, 'recall_target_absent_other_present': True,
                  'regenerated_profiles_target_absent_other_present': True}
        PAUSE.touch()
        await control.stop()
        (await client.delete(BASE)).raise_for_status()
        PAUSE.unlink()
        await control.start()
        report['synthetic_bank_removed'] = True
        (ROOT / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)


asyncio.run(main())
