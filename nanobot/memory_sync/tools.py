"""Explicit memory requests from the main, persisted user conversation."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID

import httpx

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import current_request_context
from nanobot.memory_sync.adapter import CONVERSATION_ID_KEY, SessionMemoryExporter
from nanobot.memory_sync.worker import HindsightWorker
from nanobot.security.network import PinnedDNSAsyncTransport, validate_url_target
from nanobot.session.manager import SessionManager

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["remember", "forget", "status"]},
        "memory_ids": {"type": "array", "items": {"type": "string"},
                       "minItems": 1, "maxItems": 50,
                       "description": "Required for forget: exact world/experience fact IDs from recall."},
    },
    "required": ["action"],
    "additionalProperties": False,
}


class MemoryTool(Tool):
    _plugin_discoverable = False

    def __init__(self, sessions: SessionManager, exporter: SessionMemoryExporter,
                 worker: HindsightWorker):
        self.sessions = sessions
        self.exporter = exporter
        self.worker = worker

    @property
    def parameters(self) -> dict[str, Any]:
        return deepcopy(_PARAMETERS)

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return (
            "On explicit user request, remember queues original conversation evidence; "
            "forget queues retirement of specific facts selected via Hindsight recall. "
            "Select all matching duplicate/paraphrase fact IDs; derived observations cannot be targets. "
            "Other stored facts and the chat archive remain. After forget, active chat context "
            "starts fresh. Queued is not completed; use status to check background processing."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> ToolResult:
        errors = self.validate_params(kwargs)
        if errors:
            return self.error("Invalid memory parameters.")
        ctx = current_request_context()
        if (ctx is None or not ctx.session_key or not ctx.original_user_text
                or ctx.channel in {"system", "subagent", "cron"}
                or ctx.session_key.startswith(("dream:", "subagent:"))):
            return self.error("Memory requests require a persisted user conversation.")
        session = self.sessions.get_or_create(ctx.session_key)
        if not session.policy.persist:
            return self.error("Memory requests require a persisted user conversation.")
        outbox = self.exporter.outbox
        action = kwargs["action"]
        try:
            if action == "status":
                return ToolResult(json.dumps({
                    "delivery": outbox.status(), "forget": outbox.forget_status(),
                    "worker_paused": outbox.forget_pending(),
                    "worker_resume_pending": outbox.resume_needed(),
                }))
            if action == "remember":
                if self.exporter.needs_context_reset(session) or outbox.forget_pending():
                    return self.error("Forget is pending; retry remembering in the next turn.")
                self.exporter.capture(session, len(session.messages), "remember")
                self.sessions.save(session, fsync=True)
                conversation_id = session.metadata.get(CONVERSATION_ID_KEY)
                document_id = (outbox.latest_document(conversation_id)
                               if isinstance(conversation_id, str) else None)
                return ToolResult(json.dumps({"status": "queued" if document_id else "no_evidence",
                                              "document_id": document_id}))
            ids_value: object = kwargs.get("memory_ids")
            if not isinstance(ids_value, list) or not ids_value:
                return self.error("Forget requires exact fact IDs from recall.")
            ids: list[str] = []
            for value in cast(list[object], ids_value):
                if not isinstance(value, str):
                    raise ValueError("invalid fact ID")
                ids.append(str(UUID(value)))
            groups = await self._resolve_targets(list(dict.fromkeys(ids)))
            # Resolve every target before creating the first durable request.
            for document_id, memory_ids in groups.items():
                outbox.request_fact_forget(document_id, memory_ids,
                                          active_session_key=session.key)
            return ToolResult(json.dumps({"status": "queued", "fact_ids": ids,
                                          "context_reset_after_turn": True}))
        except (httpx.HTTPError, ValueError, OSError, sqlite3.Error):
            # A later source may fail after an earlier receipt committed. Never
            # claim rollback or disclose remote bodies/original memory content.
            return self.error("Memory request could not finish. Check status before retrying.")

    async def _resolve_targets(self, ids: list[str]) -> dict[str, list[str]]:
        ok, _ = validate_url_target(self.worker.api_url)
        if not ok:
            raise ValueError("memory endpoint requires the explicit SSRF whitelist")
        own_client = self.worker.client is None
        client = self.worker.client or httpx.AsyncClient(
            transport=PinnedDNSAsyncTransport(), timeout=30,
            follow_redirects=False, trust_env=False,
        )
        try:
            groups: dict[str, list[str]] = {}
            for memory_id in ids:
                response = await client.get(self.worker.api_url + self.worker.prefix
                                            + "/memories/" + quote(memory_id, safe=""))
                response.raise_for_status()
                raw: object = response.json()
                if not isinstance(raw, dict):
                    raise ValueError("invalid fact response")
                data = cast(dict[str, object], raw)
                document_id = data.get("document_id")
                if (data.get("id") != memory_id or data.get("type") not in ("world", "experience")
                        or not isinstance(document_id, str)
                        or not self.exporter.outbox.known_document(document_id)):
                    raise ValueError("fact is not an owned source fact")
                groups.setdefault(document_id, []).append(memory_id)
            return groups
        finally:
            if own_client:
                await client.aclose()
