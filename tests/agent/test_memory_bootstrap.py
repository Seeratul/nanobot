"""Bootstrap binds exact initial sources and never silently replaces profiles."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from nanobot.memory_sync.bootstrap import ProfileBootstrap
from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.profiles import PROFILE_QUERIES, ProfileExporter
from nanobot.memory_sync.worker import HindsightWorker


@pytest.fixture
def setup_bootstrap(tmp_path):
    (tmp_path / "SOUL.md").write_text("# Soul\nUse concise German answers.\n")
    (tmp_path / "USER.md").write_text("# User\nMira plays cello.\n")
    original = {name: (tmp_path / name).read_bytes() for name in ("SOUL.md", "USER.md")}
    state = {"foreign": False, "models": {}, "unfinished": False, "requests": []}

    def handler(request):
        state["requests"].append(request.method + " " + request.url.path)
        path = request.url.path
        if path.endswith("/documents"):
            items = [{"id": "foreign-source"}] if state["foreign"] else []
            return httpx.Response(200, json={"items": items, "total": len(items)})
        if request.method == "PUT":
            return httpx.Response(200, json={})
        if request.method == "POST" and path.endswith("/mental-models"):
            data = json.loads(request.content)
            assert data["trigger"] == {"refresh_after_consolidation": True,
                                       "include_chunks": False, "exclude_mental_models": True}
            state["models"][data["id"]] = data
            return httpx.Response(200, json={"operation_id": "created"})
        identity = path.rsplit("/", 1)[1]
        if identity in state["models"]:
            return httpx.Response(200, json={"id": identity, "source_query": PROFILE_QUERIES[identity],
                "content": "" if state["unfinished"] else ("# Soul\nConcise." if identity == "agent-soul" else "# User\nCello."),
                "last_refreshed_at": "2026-10-16T10:00:00Z"})
        return httpx.Response(404, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    outbox = MemoryOutbox(tmp_path / "outbox.sqlite3")
    worker = HindsightWorker(outbox, api_url="http://127.0.0.1:8888", bank_id="personal", client=client)

    async def deliver():
        job = outbox.next_job()
        if job:
            outbox.record(job.document_id, "completed")
        return "completed"

    worker.run_once = AsyncMock(side_effect=deliver)
    exporter = ProfileExporter(worker, tmp_path, tmp_path / "projection.json")
    bootstrap = ProfileBootstrap(exporter, tmp_path / "bootstrap.json")
    return bootstrap, exporter, outbox, state, original, client


@pytest.mark.asyncio
async def test_bootstrap_restarts_idempotently_and_preserves_originals(setup_bootstrap):
    bootstrap, exporter, outbox, state, original, client = setup_bootstrap
    async with client:
        assert await bootstrap.run_once() == "retaining"
        restarted = ProfileBootstrap(exporter, bootstrap.manifest_path)
        for _ in range(8):
            status = await restarted.run_once()
            if status == "ready":
                break
        assert status == "ready"
        assert await restarted.run_once() == "ready"
    assert outbox.status() == {"completed": 2}
    assert len([call for call in state["requests"] if call.startswith("POST")]) == 2
    assert exporter.state_path.exists()
    assert all((exporter.workspace / name).read_bytes() == data for name, data in original.items())
    manifest = json.loads(bootstrap.manifest_path.read_text())
    assert manifest["status"] == "ready" and len(manifest["document_ids"]) == 2
    assert "cello" not in bootstrap.manifest_path.read_text()


@pytest.mark.asyncio
async def test_bootstrap_refuses_source_changes_after_queuing(setup_bootstrap):
    bootstrap, exporter, _, _, _, client = setup_bootstrap
    async with client:
        await bootstrap.run_once()
        (exporter.workspace / "USER.md").write_text("Changed profile")
        with pytest.raises(ValueError, match="source changed"):
            await bootstrap.run_once()
    assert not exporter.state_path.exists()


@pytest.mark.asyncio
async def test_bootstrap_refuses_foreign_bank_without_submission(setup_bootstrap):
    bootstrap, _, _, state, _, client = setup_bootstrap
    state["foreign"] = True
    async with client:
        with pytest.raises(ValueError, match="unrelated sources"):
            await bootstrap.run_once()
    bootstrap.worker.run_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_timestamp_without_generated_content_is_not_ready(setup_bootstrap):
    bootstrap, exporter, _, state, _, client = setup_bootstrap
    state["unfinished"] = True
    async with client:
        for _ in range(5):
            status = await bootstrap.run_once()
    assert status == "generating_profiles"
    assert not exporter.state_path.exists()


@pytest.mark.asyncio
async def test_possible_credentials_never_enter_outbox(setup_bootstrap):
    bootstrap, exporter, outbox, _, _, client = setup_bootstrap
    (exporter.workspace / "USER.md").write_text("password=fictional-test-secret")
    async with client:
        with pytest.raises(ValueError, match="credentials"):
            await bootstrap.run_once()
    assert outbox.status() == {}
