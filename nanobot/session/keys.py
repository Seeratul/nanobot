"""Shared session key constants and helpers."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

UNIFIED_SESSION_KEY = "unified:default"
LAST_CHANNEL_METADATA_KEY = "last_channel"


def session_key_for_channel(channel: str, chat_id: str, *, unified_session: bool = False) -> str:
    """Return the session key for a channel/chat pair."""
    if unified_session:
        return UNIFIED_SESSION_KEY
    return f"{channel}:{chat_id}"


def remember_last_channel(
    metadata: MutableMapping[str, Any],
    channel: str,
    chat_id: str,
) -> None:
    """Persist the latest concrete delivery route in session metadata."""
    if not channel or not chat_id:
        return
    metadata[LAST_CHANNEL_METADATA_KEY] = f"{channel}:{chat_id}"


def delivery_route_from_session_key(key: object) -> tuple[str, str] | None:
    """Resolve a concrete destination without changing the session's identity.

    Discord stores threads as discord:<parent>:thread:<thread>, but its
    transport requires the numeric thread id. Other channel address formats,
    including colon-bearing addresses, must remain unchanged.
    """
    if not isinstance(key, str) or ":" not in key:
        return None
    channel, chat_id = key.split(":", 1)
    if not channel or not chat_id:
        return None
    if channel == "discord":
        parts = chat_id.split(":")
        if (
            len(parts) == 3
            and parts[1] == "thread"
            and parts[0].isascii() and parts[0].isdecimal()
            and parts[2].isascii() and parts[2].isdecimal()
        ):
            chat_id = parts[2]
    return channel, chat_id


def last_channel_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> tuple[str, str] | None:
    """Return a concrete delivery route from persisted session metadata."""
    if not isinstance(metadata, Mapping):
        return None
    return delivery_route_from_session_key(metadata.get(LAST_CHANNEL_METADATA_KEY))
