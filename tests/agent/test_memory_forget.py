import json

import httpx
import pytest

from nanobot.memory_sync.forget import ForgetWorker
from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.profiles import PROFILE_MODELS, ProfileExporter
from nanobot.memory_sync.worker import HindsightWorker


class Control:
    def __init__(self):
        self.stopped = False
        self.starts = 0
        self.fail_start = False

    async def stop(self):
        self.stopped = True

    async def start(self):
        if self.fail_start:
            raise RuntimeError('simulated systemd outage')
        self.starts += 1
        self.stopped = False


def setup(tmp_path, *, api_worker=False, child=False):
    outbox = MemoryOutbox(tmp_path / 'outbox.sqlite3')
    document = outbox.enqueue('conversation-a', 'cli:test', ['original'], ['user: fictional secret'])
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    for name in PROFILE_MODELS.values():
        (workspace / name).write_text('fictional secret')
    control = Control()
    requests = []
    fail = [False]

    def handle(request):
        requests.append(request)
        assert control.stopped, 'no API request before process quiescence'
        if request.url.path == '/version':
            return httpx.Response(200, json={'features': {'worker': api_worker}})
        if request.method == 'GET' and '/operations/' in request.url.path:
            return httpx.Response(200, json={'child_operations': (
                [{'operation_id': '00000000-0000-0000-0000-000000000001', 'status': 'processing'}]
                if child and not request.url.path.endswith('000000000001') else None
            )})
        if '/documents/' in request.url.path and fail[0]:
            return httpx.Response(503)
        return httpx.Response(200, json={'success': True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    worker = HindsightWorker(outbox, api_url='http://127.0.0.1:8888', bank_id='personal', client=client)
    exporter = ProfileExporter(worker, workspace, tmp_path / 'profiles.json')
    exporter.register_baseline()
    return outbox, document, control, requests, fail, client, exporter


@pytest.mark.asyncio
async def test_forget_quiesces_worker_invalidates_profiles_and_blocks_reimport(tmp_path):
    outbox, document, control, requests, _, client, exporter = setup(tmp_path)
    async with client:
        outbox.request_forget(document)
        assert outbox.next_job() is None
        assert await exporter.sync_once() == {'status': 'forget_pending'}
        forgetter = ForgetWorker(exporter.worker, exporter, control)
        assert await forgetter.run_once() == 'forgotten'
        assert control.starts == 1
        assert outbox.next_forget() is None
        assert not outbox.forget_pending()
        assert outbox.enqueue('conversation-a', 'cli:test', ['original', 'new'], ['old', 'new']) is None
        assert outbox.status() == {'suppressed': 1}
        assert all('fictional secret' not in p.read_text() for p in exporter.workspace.iterdir())
        assert json.loads(exporter.state_path.read_text())['user-profile']['revision'] is None
        assert [r.method for r in requests] == ['GET', 'GET', 'DELETE', 'DELETE', 'DELETE', 'POST', 'DELETE', 'POST']
        assert '/operations/' in requests[2].url.path


@pytest.mark.asyncio
async def test_forget_failure_stays_paused_and_resumes_after_restart(tmp_path):
    outbox, document, control, _, fail, client, exporter = setup(tmp_path)
    async with client:
        outbox.request_forget(document)
        fail[0] = True
        with pytest.raises(httpx.HTTPStatusError):
            await ForgetWorker(exporter.worker, exporter, control).run_once()
        assert control.stopped
        assert outbox.forget_pending()
        assert (exporter.workspace / 'USER.md').read_text() == '# User\n'
        # A fresh object finds the persisted receipts despite erased payloads.
        reopened = MemoryOutbox(outbox.path)
        exporter.worker.outbox = reopened
        fail[0] = False
        assert await ForgetWorker(exporter.worker, exporter, control).run_once() == 'forgotten'
        assert control.starts == 1


@pytest.mark.asyncio
async def test_forget_refuses_api_with_embedded_worker(tmp_path):
    outbox, document, control, requests, _, client, exporter = setup(tmp_path, api_worker=True)
    async with client:
        outbox.request_forget(document)
        with pytest.raises(ValueError, match='worker disabled'):
            await ForgetWorker(exporter.worker, exporter, control).run_once()
        assert control.stopped and outbox.forget_pending()
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_failed_worker_restart_has_durable_resume_receipt(tmp_path):
    outbox, document, control, _, _, client, exporter = setup(tmp_path)
    async with client:
        outbox.request_forget(document)
        control.fail_start = True
        with pytest.raises(RuntimeError):
            await ForgetWorker(exporter.worker, exporter, control).run_once()
        assert outbox.resume_needed()
        assert not outbox.forget_pending()
        control.fail_start = False
        assert await ForgetWorker(exporter.worker, exporter, control).run_once() == 'resumed'
        assert not outbox.resume_needed()


@pytest.mark.asyncio
async def test_orphan_pause_marker_fails_closed(tmp_path):
    outbox, _, control, requests, _, client, exporter = setup(tmp_path)
    async with client:
        outbox.pause_path.touch()
        assert await ForgetWorker(exporter.worker, exporter, control).run_once() == 'needs_attention'
        assert control.stopped and outbox.forget_pending()
        assert not requests


def test_unknown_document_does_not_pause_or_delete(tmp_path):
    outbox = MemoryOutbox(tmp_path / 'outbox.sqlite3')
    with pytest.raises(ValueError, match='unknown source'):
        outbox.request_forget('invented')
    assert not outbox.forget_pending()


@pytest.mark.asyncio
async def test_parent_and_child_operations_are_both_cancelled(tmp_path):
    outbox, document, control, requests, _, client, exporter = setup(tmp_path, child=True)
    async with client:
        outbox.request_forget(document)
        assert await ForgetWorker(exporter.worker, exporter, control).run_once() == 'forgotten'
        cancelled = [r for r in requests if r.method == 'DELETE' and '/operations/' in r.url.path]
        assert len(cancelled) == 2
        assert cancelled[1].url.path.endswith('00000000-0000-0000-0000-000000000001')


@pytest.mark.asyncio
async def test_worker_control_keeps_nnp_and_only_observes_fixed_unit(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from nanobot.memory_sync.forget import SystemdWorkerControl

    marker = tmp_path / 'pause'
    marker.touch()
    stopped = AsyncMock()
    stopped.returncode = 0
    stopped.communicate.return_value = (
        b'ActiveState=failed\nMainPID=0\nControlPID=0\nKillMode=control-group\nSendSIGKILL=yes\n', None,
    )
    running = AsyncMock()
    running.returncode = 0
    running.communicate.return_value = (b'ActiveState=active\n', None)
    spawn = AsyncMock(side_effect=[stopped, running])
    monkeypatch.setattr('asyncio.create_subprocess_exec', spawn)
    control = SystemdWorkerControl(marker)
    await control.stop()
    with pytest.raises(ValueError, match='still present'):
        await control.start()
    marker.unlink()
    await control.start()
    for call in spawn.call_args_list:
        assert call.args[:3] == ('systemctl', 'show', 'hindsight-worker.service')
        assert 'sudo' not in call.args
    with pytest.raises(ValueError, match='pause signal'):
        await control.stop()


def test_pause_signal_survives_reopen_and_rejects_silent_reconfiguration(tmp_path):
    marker = tmp_path / 'global-pause'
    path = tmp_path / 'outbox.sqlite3'
    outbox = MemoryOutbox(path, pause_path=marker)
    document = outbox.enqueue('one', 'cli:test', ['one'], ['fictional'])
    outbox.request_forget(document)
    reopened = MemoryOutbox(path)
    assert reopened.pause_path == marker.resolve()
    assert reopened.forget_pending()
    with pytest.raises(ValueError, match='pause signal changed'):
        MemoryOutbox(path, pause_path=tmp_path / 'wrong-marker')


@pytest.mark.asyncio
async def test_worker_status_timeout_reaps_the_readonly_process(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from nanobot.memory_sync.forget import SystemdWorkerControl

    marker = tmp_path / 'pause'
    marker.touch()
    process = MagicMock()
    process.returncode = None
    process.communicate = AsyncMock(side_effect=TimeoutError)
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr('asyncio.create_subprocess_exec', AsyncMock(return_value=process))
    with pytest.raises(TimeoutError):
        await SystemdWorkerControl(marker).stop()
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_between_completed_receipt_and_pause_removal(tmp_path):
    outbox, document, control, requests, _, client, exporter = setup(tmp_path)
    async with client:
        conversation = outbox.request_forget(document)
        exporter.invalidate()
        outbox.complete_forget(conversation)
        # Simulate process death before unpause_if_complete was reached.
        assert outbox.forget_pending() and outbox.resume_needed()
        assert await ForgetWorker(exporter.worker, exporter, control).run_once() == 'resumed'
        assert control.starts == 1
        assert not outbox.forget_pending() and not outbox.resume_needed()
        assert not requests
