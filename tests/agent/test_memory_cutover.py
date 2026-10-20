import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from nanobot.command.builtin import cmd_dream, cmd_dream_prompt, cmd_dream_restore
from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.worker import HindsightWorker


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [cmd_dream, cmd_dream_prompt, cmd_dream_restore])
async def test_hindsight_blocks_manual_dream_writers_before_access(handler):
    ctx = SimpleNamespace(loop=SimpleNamespace(hindsight_config=SimpleNamespace(enabled=True)),
                          msg=SimpleNamespace(channel="test", chat_id="test"))
    result = await handler(ctx)
    assert "Dream is disabled" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("appears_during_get", [False, True])
@pytest.mark.parametrize("remote_status", ["not_found", "failed"])
async def test_timeout_hold_prevents_submission_or_retry(appears_during_get, remote_status):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        box = MemoryOutbox(root / "outbox.sqlite3")
        box.enqueue("test", "test", ["a"], ["user: synthetic test fact"])
        hold = root / "hold"
        if not appears_during_get:
            hold.touch()

        async def transport(request):
            assert request.method == "GET"
            hold.touch()
            if remote_status == "failed":
                return httpx.Response(200, json={"status": "failed", "error_message": "timeout"})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            worker = HindsightWorker(box, api_url="http://127.0.0.1:8888", bank_id="test",
                                     client=client, hold_path=hold)
            assert await worker.run_once() == "paused"
        assert box.status() == {"pending": 1}


def test_new_segments_use_stable_unset_event_date():
    with tempfile.TemporaryDirectory() as temporary:
        box = MemoryOutbox(Path(temporary) / "outbox.sqlite3")
        box.enqueue("test", "test", ["a"], ["user: synthetic test fact"])
        assert json.loads(box.next_job().payload)["items"][0]["timestamp"] == "unset"
