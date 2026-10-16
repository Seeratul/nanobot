"""Memory requests use original evidence and durable, fact-specific receipts."""
import json
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.memory_sync.adapter import CONVERSATION_ID_KEY, SessionMemoryExporter
from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.tools import MemoryTool
from nanobot.memory_sync.worker import HindsightWorker
from nanobot.session.manager import SessionManager


@pytest.fixture
def setup_memory(tmp_path):
    sessions = SessionManager(tmp_path)
    outbox = MemoryOutbox(tmp_path / "outbox.sqlite3")
    exporter = SessionMemoryExporter(outbox)
    session = sessions.get_or_create("discord:fictional")
    session.add_message("user", "I play cello and live in Westlake.")
    exporter.capture(session, len(session.messages), "remember")
    sessions.save(session, fsync=True)
    document_id = outbox.latest_document(session.metadata[CONVERSATION_ID_KEY])
    fact_id = str(uuid4())
    ctx = RequestContext(channel="discord", chat_id="fictional", session_key=session.key,
                         original_user_text="Forget my town.")
    return sessions, outbox, exporter, session, document_id, fact_id, ctx


@pytest.mark.asyncio
async def test_forget_only_queues_fact_and_resets_both_source_and_active_session(setup_memory):
    sessions, outbox, exporter, session, doc, fact, ctx = setup_memory
    active = sessions.get_or_create("discord:other")
    active.add_message("user", "Forget my town.")
    ctx = RequestContext(channel="discord", chat_id="other", session_key=active.key,
                         original_user_text=ctx.original_user_text)
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"id": fact, "type": "world", "document_id": doc})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = HindsightWorker(outbox, api_url="http://127.0.0.1:8888", bank_id="personal", client=client)
        tool = MemoryTool(sessions, exporter, worker)
        with request_context(ctx), patch("nanobot.memory_sync.tools.validate_url_target", return_value=(True, "")):
            result = await tool.execute(action="forget", memory_ids=[fact])
    assert json.loads(result)["status"] == "queued"
    assert outbox.fact_targets(session.metadata[CONVERSATION_ID_KEY]) == [fact]
    assert exporter.needs_context_reset(active)
    assert exporter.needs_context_reset(session)
    assert outbox.forget_pending()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,owned", [("observation", True), ("world", False)])
async def test_invalid_later_target_creates_no_receipts(setup_memory, kind, owned):
    sessions, outbox, exporter, _, doc, fact, ctx = setup_memory
    other = str(uuid4())
    def handler(request):
        identity = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json={"id": identity, "type": "world" if identity == fact else kind,
                                        "document_id": doc if identity == fact or owned else "foreign"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tool = MemoryTool(sessions, exporter, HindsightWorker(
            outbox, api_url="http://127.0.0.1:8888", bank_id="personal", client=client))
        with request_context(ctx), patch("nanobot.memory_sync.tools.validate_url_target", return_value=(True, "")):
            result = await tool.execute(action="forget", memory_ids=[fact, other])
    assert result.is_error
    assert not outbox.forget_pending()
    assert outbox.forget_status() == {}


@pytest.mark.asyncio
async def test_remote_errors_redacted_and_whitelist_required(setup_memory):
    sessions, outbox, exporter, _, _, fact, ctx = setup_memory
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(500, text="PRIVATE FACT AND TOKEN"))) as client:
        tool = MemoryTool(sessions, exporter, HindsightWorker(
            outbox, api_url="http://127.0.0.1:8888", bank_id="personal", client=client))
        with request_context(ctx):
            with patch("nanobot.memory_sync.tools.validate_url_target", return_value=(False, "private")):
                assert (await tool.execute(action="forget", memory_ids=[fact])).is_error
            with patch("nanobot.memory_sync.tools.validate_url_target", return_value=(True, "")):
                result = await tool.execute(action="forget", memory_ids=[fact])
        assert result.is_error and "PRIVATE" not in result
    assert not outbox.forget_pending()


@pytest.mark.asyncio
async def test_remember_captures_original_user_evidence_and_requires_request(setup_memory):
    sessions, outbox, exporter, session, _, _, ctx = setup_memory
    tool = MemoryTool(sessions, exporter, HindsightWorker(
        outbox, api_url="http://127.0.0.1:8888", bank_id="personal"))
    assert (await tool.execute(action="remember")).is_error
    session.add_message("user", "I also speak German.")
    with request_context(ctx):
        result = await tool.execute(action="remember")
        status = json.loads(await tool.execute(action="status"))
    assert json.loads(result)["status"] == "queued"
    assert status["delivery"]["pending"] == 2
    with outbox._connect() as db:
        content = " ".join(row[0] for row in db.execute("SELECT payload FROM jobs"))
    assert "German" in content


def test_context_reset_survives_reload_and_never_reexports_forgotten_prefix(setup_memory):
    sessions, outbox, exporter, session, doc, fact, _ = setup_memory
    session.commit_summary_checkpoint("Westlake summary")
    outbox.request_fact_forget(doc, [fact])
    session.add_message("assistant", "Old in-flight result from Westlake")
    exporter.capture(session, len(session.messages), "remember")
    with pytest.raises(ValueError, match="context reset"):
        session._archive_observer = exporter.capture
        session.commit_summary_checkpoint("Stale town summary")
    original_count = len(session.messages)
    assert exporter.reset_context_if_needed(session)
    sessions.save(session, fsync=True)
    restored = sessions.read_session_snapshot(session.key)
    assert restored is not None
    assert restored.get_history() == []
    assert "_last_summary" not in restored.metadata
    assert len(restored.messages) == original_count
    restored.add_message("user", "I enjoy hiking.")
    exporter.capture(restored, len(restored.messages), "remember")
    with outbox._connect() as db:
        payloads = [row[0] for row in db.execute("SELECT payload FROM jobs WHERE state='pending'")]
    assert len(payloads) == 1
    assert "hiking" in payloads[0] and "Westlake" not in payloads[0]
    assert not exporter.reset_context_if_needed(restored)


def test_new_after_forget_does_not_leave_invalid_export_boundary(setup_memory):
    _, outbox, exporter, session, doc, fact, _ = setup_memory
    outbox.request_fact_forget(doc, [fact])
    exporter.reset_context_if_needed(session)
    session.clear()
    session.add_message("user", "A fresh conversation")
    exporter.capture(session, len(session.messages), "remember")
    assert outbox.latest_document(session.metadata[CONVERSATION_ID_KEY])


def test_pending_forget_hides_profiles_and_legacy_memory(tmp_path):
    from nanobot.agent.context import ContextBuilder

    (tmp_path / "SOUL.md").write_text("Private identity from Westlake")
    (tmp_path / "USER.md").write_text("User lives in Westlake")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text("Town Westlake")
    builder = ContextBuilder(tmp_path)
    assert "Westlake" in builder.build_system_prompt()
    builder.profile_reads_allowed = lambda: False
    prompt = builder.build_system_prompt()
    assert "Westlake" not in prompt
    assert "SOUL.md" in prompt and "USER.md" in prompt


def test_fork_preserves_export_fence(setup_memory):
    sessions, outbox, exporter, session, doc, fact, _ = setup_memory
    outbox.request_fact_forget(doc, [fact])
    exporter.reset_context_if_needed(session)
    session.add_message("user", "Fresh hiking preference")
    sessions.save(session, fsync=True)
    target = sessions.fork_session_before_user_index(session.key, "discord:fork", 2)
    assert target is not None
    exporter.capture(target, len(target.messages), "remember")
    with outbox._connect() as db:
        payload = db.execute("SELECT payload FROM jobs WHERE session_key=?", (target.key,)).fetchone()[0]
    assert "hiking" in payload and "Westlake" not in payload
    assert "Westlake" not in json.dumps(target.get_history())
