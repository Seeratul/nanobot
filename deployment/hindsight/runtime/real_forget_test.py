"""Synthetic in-flight retain deletion against the dedicated systemd stack."""
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

ROOT = Path('/var/tmp/hindsight-forget-test-v4')
PAUSE = Path('/var/lib/hindsight/control/forget-pending')
ROOT.mkdir(mode=0o700, exist_ok=True)
URL = 'http://127.0.0.1:8888'
BANK = 'synthetic-forget-race'
BASE = '/v1/default/banks/' + BANK


async def main():
    nnp = 'NoNewPrivs:\t1' in Path('/proc/self/status').read_text()
    assert nnp, 'acceptance test must retain NoNewPrivileges'
    report = {'no_new_privileges': nnp}
    control = SystemdWorkerControl()
    async with httpx.AsyncClient(base_url=URL, timeout=180, trust_env=False) as client:
        PAUSE.touch()
        await control.stop()
        response = await client.delete(BASE)
        assert response.status_code in (200, 404)
        response = await client.put(BASE, json={'name': 'Fictional forget race only'})
        response.raise_for_status()
        PAUSE.unlink()
        await control.start()
        outbox = MemoryOutbox(ROOT / 'outbox.sqlite3', bank_id=BANK, pause_path=PAUSE)
        document = outbox.enqueue('fictional-race-v1', 'cli:fictional-test',
            ['fictional-source-v1'], [
                'The fictional user Mira says: My private test phrase is cobalt-otter-742. '
                'I live in the imaginary town of Westlake and play cello. '
                'This is synthetic test data, unrelated to any real person. '
                + ' '.join(f'Fictional project number {n} uses a blue notebook.' for n in range(15))
            ])
        worker = HindsightWorker(outbox, api_url=URL, bank_id=BANK)
        assert await worker.run_once() == 'submitted'
        operation_id = str(uuid5(NAMESPACE_URL, document))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            operation = (await client.get(BASE + '/operations/' + operation_id)).json()
            state = operation['status']
            children = operation.get('child_operations', [])
            if any(child['status'] == 'processing' for child in children):
                break
            assert state == 'pending', 'operation finished before race could be exercised'
            await asyncio.sleep(0.5)
        assert any(child['status'] == 'processing' for child in children)
        report['operation_before_forget'] = state
        report['child_before_forget'] = 'processing'
        workspace = ROOT / 'workspace'
        workspace.mkdir(exist_ok=True)
        for name in ('SOUL.md', 'USER.md'):
            (workspace / name).write_text('Synthetic cached cobalt-otter-742')
        exporter = ProfileExporter(worker, workspace, ROOT / 'profiles.json')
        exporter.register_baseline()
        outbox.request_forget(document)
        # First deletion fails after the real process stop, exercising restart.
        failed_once = False

        async def forward(request):
            nonlocal failed_once
            if request.method == 'DELETE' and '/documents/' in request.url.path and not failed_once:
                failed_once = True
                return httpx.Response(503)
            return await client.send(client.build_request(request.method, str(request.url),
                                                         content=request.content))

        async with httpx.AsyncClient(transport=httpx.MockTransport(forward)) as injected:
            worker.client = injected
            try:
                await ForgetWorker(worker, exporter, control).run_once()
                raise AssertionError('failure injection did not trigger')
            except httpx.HTTPStatusError as exc:
                assert exc.response.status_code == 503
        assert outbox.forget_pending()
        assert outbox.next_job() is None
        assert (workspace / 'USER.md').read_text() == '# User\n'
        report['failure_kept_paused_and_profiles_cleared'] = True
        worker = HindsightWorker(MemoryOutbox(outbox.path, bank_id=BANK, pause_path=PAUSE), api_url=URL, bank_id=BANK)
        exporter = ProfileExporter(worker, workspace, ROOT / 'profiles.json')
        result = await ForgetWorker(worker, exporter, control).run_once()
        assert result == 'forgotten'
        report['fresh_worker_retry'] = result
        assert not worker.outbox.forget_pending()
        assert not worker.outbox.resume_needed()
        assert worker.outbox.enqueue('fictional-race-v1', 'cli:fictional-test', ['old'], ['old']) is None
        report['reimport_blocked'] = True
        for delay in (0, 15):
            await asyncio.sleep(delay)
            response = await client.get(BASE + '/documents/' + document)
            assert response.status_code == 404
        report['document_absent_after_worker_restart'] = True
        # Recall must not find a fact from this now-empty bank.
        response = await client.post(BASE + '/memories/recall', json={
            'query': 'What is Mira private test phrase?', 'budget': 'low', 'max_tokens': 500})
        response.raise_for_status()
        results = response.json().get('results', [])
        assert not results, 'forgotten source still has recall results'
        report['recall_results_after_delete'] = len(results)
        PAUSE.touch()
        await control.stop()
        response = await client.delete(BASE)
        response.raise_for_status()
        PAUSE.unlink()
        await control.start()
        report['synthetic_bank_removed'] = True
    (ROOT / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


asyncio.run(main())
