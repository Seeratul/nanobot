"""Capture original session prefixes at the existing checkpoint boundary."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, uuid5

from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.runtime_context import public_history_message
from nanobot.session.history_visibility import is_hidden_history_message

if TYPE_CHECKING:
    from nanobot.session.manager import Session

CONVERSATION_ID_KEY = "_hindsight_conversation_id"
_SECRET = re.compile(
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----.*?-----END (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b"
    r"|(?i:\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b)"
    r"[\"']?\s*[:=]\s*[\"']?[^\s,\"'}]+",
    re.DOTALL,
)


def _objects(value: object) -> list[dict[str, Any]]:
    """Normalize JSON-shaped lists at the persisted-message boundary."""
    if not isinstance(value, list):
        return []
    return [cast(dict[str, Any], item) for item in cast(list[object], value)
            if isinstance(item, dict)]


class SessionMemoryExporter:
    def __init__(self, outbox: MemoryOutbox, *, known_secrets: tuple[str, ...] = ()):
        self.outbox = outbox
        self.known_secrets = tuple(value for value in known_secrets if len(value) >= 8)

    def _redact(self, text: str) -> str:
        for value in self.known_secrets:
            text = text.replace(value, "[REDACTED]")
        return _SECRET.sub("[REDACTED]", text)

    @staticmethod
    def _text(content: object) -> str:
        if isinstance(content, str):
            return content
        return "\n".join(
            block["text"] for block in _objects(content)
            if block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )

    def capture(self, session: Session, boundary: int, reason: str) -> None:
        """Commit before the session mutates; no external calls on this path."""
        if not session.policy.persist or session.key.startswith(("dream:", "subagent:")):
            return
        if not 0 <= boundary <= len(session.messages):
            raise ValueError("invalid memory export boundary")
        conversation_id = session.metadata.get(CONVERSATION_ID_KEY)
        if not isinstance(conversation_id, str) or not conversation_id:
            # Deterministic even if a process dies after queue commit but before
            # session save. /new rotates this ID after the old prefix is queued.
            conversation_id = str(uuid5(
                NAMESPACE_URL, session.key + ":" + session.created_at.isoformat(),
            ))
            session.metadata[CONVERSATION_ID_KEY] = conversation_id
        originals = [m for m in session.messages[:boundary]
                     if m.get("role") in {"user", "assistant", "tool"}
                     and not m.get("_command") and not is_hidden_history_message(m)]
        # A provider may compact just after a tool call. Leave that call and its
        # later results for the next segment rather than splitting the exchange.
        result_ids = {m.get("tool_call_id") for m in originals if m.get("role") == "tool"}
        for index, message in enumerate(originals):
            calls = _objects(message.get("tool_calls"))
            if any(call.get("id") not in result_ids for call in calls):
                if reason == "new":
                    # The source is about to be cleared; preserve the incomplete
                    # exchange explicitly instead of losing it.
                    break
                originals = originals[:index]
                break
        memory_call_ids: set[str] = set()
        for message in originals:
            for call in _objects(message.get("tool_calls")):
                raw_function: object = call.get("function")
                if isinstance(raw_function, dict):
                    function = cast(dict[str, Any], raw_function)
                    name = function.get("name", "")
                    call_id = call.get("id")
                    if (isinstance(name, str) and name.startswith("mcp_hindsight_")
                            and isinstance(call_id, str)):
                        memory_call_ids.add(call_id)
        fingerprints: list[str] = []
        exported: list[str] = []
        for message in originals:
            # Hash original evidence privately. Never include reasoning or
            # provider state in either the hash or exported text.
            source = {k: message[k] for k in (
                "role", "content", "timestamp", "tool_calls", "tool_call_id", "name",
            ) if k in message}
            fingerprints.append(hashlib.sha256(json.dumps(
                source, sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest())
            public = public_history_message(message)
            role = public.get("role")
            if role == "tool" and public.get("tool_call_id") in memory_call_ids:
                exported.append("")
                continue
            text = self._text(public.get("content"))
            calls = [c for c in _objects(public.get("tool_calls"))
                     if c.get("id") not in memory_call_ids]
            if calls:
                text += "\nTool calls: " + json.dumps(calls, ensure_ascii=False)
            if not text.strip():
                exported.append("")
                continue
            timestamp = public.get("timestamp", "time not recorded")
            label = f"{role} [{timestamp}]"
            if role == "tool":
                label += f" attributed tool evidence ({public.get('name', 'unnamed')})"
            exported.append(self._redact(label + "\n" + text))
        self.outbox.enqueue(conversation_id, session.key, fingerprints, exported)
