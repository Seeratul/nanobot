import json
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pi_outage_diagnostics import collect_once, write_event
from retry_limits import install
from wait_for_hindsight import wait_ready

from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.worker import HindsightWorker


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['dns', 'quota', 'transport'])
async def test_retry_budget_persists_across_process_reopen(failure):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / 'box.sqlite3'
        box = MemoryOutbox(path)
        box.enqueue('test', 'test', ['one'], ['synthetic evidence'])

        async def handler(request):
            if failure == 'transport':
                raise httpx.ConnectError('private exception content')
            if request.method == 'GET':
                return httpx.Response(200, json={'status': 'failed', 'error_message':
                    'ClientConnectorDNSError: Temporary failure in name resolution' if failure == 'dns' else '429 quota'})
            return httpx.Response(200, json={'success': True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for expected in ('retry', 'retry', 'attention'):
                box = MemoryOutbox(path)
                with box._connect() as db:
                    db.execute('UPDATE jobs SET next_attempt_at=0')
                worker = HindsightWorker(box, api_url='http://127.0.0.1:8888', bank_id='personal',
                                         client=client, max_delivery_attempts=2)
                assert await worker.run_once() == expected
            assert box.status() == {'needs_attention': 1}
            assert box.next_job() is None
            assert b'private exception content' not in path.read_bytes()


@pytest.mark.asyncio
async def test_age_budget_stops_retry_but_can_acknowledge_completion():
    with tempfile.TemporaryDirectory() as temporary:
        box = MemoryOutbox(Path(temporary) / 'box.sqlite3')
        document = box.enqueue('test', 'test', ['one'], ['synthetic evidence'])
        box.record(document, 'submitted', attempted=True)
        with box._connect() as db:
            db.execute('UPDATE jobs SET first_attempt_at=?', (time.time() - 100,))
        state = 'failed'

        async def handler(request):
            assert request.method == 'GET'
            return httpx.Response(200, json={'status': state, 'error_message': 'timeout'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = HindsightWorker(box, api_url='http://127.0.0.1:8888', bank_id='personal',
                                     client=client, retry_window_seconds=10)
            assert await worker.run_once() == 'attention'
            # Explicitly restore only the synthetic fixture to test a late receipt.
            box.record(document, 'submitted')
            state = 'completed'
            assert await worker.run_once() == 'completed'


@pytest.mark.asyncio
async def test_consolidation_budget_does_not_change_retains():
    class Poller:
        _schedule_retry_all = AsyncMock()
        _mark_all_failed = AsyncMock()

    original = Poller._schedule_retry_all
    install(Poller, max_retries=2)
    poller = Poller()
    for kind, count in [('consolidation', 1), ('retain', 99)]:
        await poller._schedule_retry_all(SimpleNamespace(task_dict={'operation_type': kind, '_retry_count': count}), None, 'private')
    assert original.await_count == 2
    await poller._schedule_retry_all(SimpleNamespace(task_dict={'operation_type': 'consolidation', '_retry_count': 2}), None, 'private')
    assert poller._mark_all_failed.await_count == 1
    assert original.await_count == 2
    assert 'private' not in str(poller._mark_all_failed.call_args)


def test_startup_readiness_waits_and_times_out():
    now = [0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    response = SimpleNamespace(status=200)
    assert wait_ready(opener=lambda *a, **k: nullcontext(response), clock=lambda: now[0], sleep=sleep)

    def unavailable(*args, **kwargs):
        raise OSError('private connection details')

    assert not wait_ready(timeout=4, opener=unavailable, clock=lambda: now[0], sleep=sleep)
    assert sleeps == [2, 2]


def test_rotated_diagnostics_remain_bounded_and_private():
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        for index in range(30):
            write_event(directory, {'sequence': index, 'kind': 'sample'}, limit=100)
        paths = list(directory.glob('outages.jsonl*'))
        assert len(paths) == 4
        assert all(p.stat().st_size <= 100 for p in paths)
        assert json.loads((directory / 'outages.jsonl').read_text().splitlines()[-1])['sequence'] == 29


def test_diagnostics_exclude_application_logs_and_redact_kernel_credentials():
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        records = [
            {'_TRANSPORT': 'stdout', 'PRIORITY': '3', 'MESSAGE': 'PRIVATE_PROMPT_SENTINEL', '__CURSOR': 'a'},
            {'_TRANSPORT': 'kernel', 'PRIORITY': '3', 'MESSAGE': 'USB cannot enable token=private_value', '__CURSOR': 'b'},
        ]
        with patch('pi_outage_diagnostics.snapshot', return_value={}), patch(
            'pi_outage_diagnostics.command', return_value=(0, '\n'.join(json.dumps(r) for r in records))
        ):
            collect_once(directory)
        text = (directory / 'outages.jsonl').read_text()
        assert 'PRIVATE_PROMPT_SENTINEL' not in text
        assert 'private_value' not in text
        assert '[REDACTED]' in text
