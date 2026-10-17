"""Seed existing authorized profiles without replacing the local originals."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import httpx
from filelock import FileLock

from nanobot.memory_sync.adapter import contains_potential_credentials
from nanobot.memory_sync.profiles import PROFILE_MODELS, PROFILE_QUERIES, ProfileExporter
from nanobot.memory_sync.worker import HindsightWorker
from nanobot.security.network import PinnedDNSAsyncTransport, validate_url_target


def _object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("invalid bootstrap response")
    return cast(dict[str, Any], raw)


class ProfileBootstrap:
    """An idempotent bootstrap, bound to hashes rather than copied profile files."""

    def __init__(self, exporter: ProfileExporter, manifest_path: Path):
        self.exporter = exporter
        self.worker = exporter.worker
        self.manifest_path = manifest_path

    def _sources(self) -> dict[str, str]:
        sources: dict[str, str] = {}
        for model_id, filename in PROFILE_MODELS.items():
            path = self.exporter.workspace / filename
            text = path.read_text(encoding="utf-8")
            if not text.strip() or len(text) > 8000 or contains_potential_credentials(text):
                raise ValueError("profile is empty, too large or may contain credentials")
            sources[model_id] = text
        return sources

    async def run_once(self) -> str:
        with FileLock(self.manifest_path.with_suffix(".lock"), timeout=0):
            sources = self._sources()
            hashes = {key: hashlib.sha256(text.encode()).hexdigest() for key, text in sources.items()}
            identities = {key: str(uuid5(NAMESPACE_URL,
                f"nanobot:{self.worker.prefix}:profile-baseline:{key}:{hashes[key]}")) for key in sources}
            expected = {"version": 1, "bank": self.worker.prefix, "hashes": hashes}
            if self.manifest_path.exists():
                manifest = _object(json.loads(self.manifest_path.read_text(encoding="utf-8")))
                if any(manifest.get(key) != value for key, value in expected.items()):
                    raise ValueError("bootstrap source changed; explicit reconciliation required")
            else:
                if self.exporter.state_path.exists():
                    raise ValueError("existing projection state requires explicit reconciliation")
                ProfileExporter.write_atomic(self.manifest_path, json.dumps(expected, sort_keys=True))
            if self.worker.outbox.forget_pending():
                raise ValueError("cannot bootstrap during forgetting")
            documents: dict[str, str] = {}
            for key, text in sources.items():
                document = self.worker.outbox.enqueue(
                    identities[key], "profile:" + key, [hashes[key]], [
                        "user-authorized initial profile baseline from " + PROFILE_MODELS[key]
                        + ". The user explicitly requested preserving and maintaining this existing "
                        "profile through Hindsight. This is the initial authorized baseline, not "
                        "an assistant proposal. Later explicit user corrections take precedence.\n\n"
                        + text,
                    ],
                ) or self.worker.outbox.latest_document(identities[key])
                if document is None:
                    raise ValueError("bootstrap source has been quarantined")
                documents[key] = document
            ok, _ = validate_url_target(self.worker.api_url, allow_loopback=True)
            if not ok:
                raise ValueError("memory API is blocked")
            own_client = self.worker.client is None
            client = self.worker.client or httpx.AsyncClient(
                transport=PinnedDNSAsyncTransport(allow_loopback=True), timeout=60,
                follow_redirects=False, trust_env=False,
            )
            try:
                base = self.worker.api_url + self.worker.prefix
                response = await client.get(base + "/documents", params={"limit": 100})
                if response.status_code != 404:
                    response.raise_for_status()
                    listing = _object(response.json())
                    items: object = listing.get("items")
                    if not isinstance(items, list):
                        raise ValueError("invalid document listing")
                    rows = cast(list[object], items)
                    if (listing.get("total") != len(rows)
                            or any(_object(item).get("id") not in documents.values() for item in rows)):
                        raise ValueError("bank contains unrelated sources; bootstrap refused")
                (await client.put(base, json={"name": "Nanobot personal memory"})).raise_for_status()
                if self.worker.outbox.status().get("completed", 0) != len(documents):
                    status = await self.worker.run_once()
                    if status == "attention":
                        raise ValueError("retaining baseline requires attention")
                    return "retaining"
                for model_id in PROFILE_MODELS:
                    response = await client.get(base + "/mental-models/" + model_id)
                    if response.status_code == 404:
                        (await client.post(base + "/mental-models", json={
                            "id": model_id, "name": model_id, "source_query": PROFILE_QUERIES[model_id],
                            "max_tokens": 750, "trigger": {
                                "refresh_after_consolidation": True, "include_chunks": False,
                                "exclude_mental_models": True,
                            },
                        })).raise_for_status()
                        return "generating_profiles"
                    response.raise_for_status()
                    model = _object(response.json())
                    if model.get("source_query") != PROFILE_QUERIES[model_id]:
                        raise ValueError("existing model has an unrelated profile query")
                    content = model.get("content")
                    if model.get("last_refresh_failed_at"):
                        raise ValueError("profile generation requires attention")
                    if not content or not model.get("last_refreshed_at"):
                        return "generating_profiles"
                    if (model.get("id") != model_id or not isinstance(content, str)
                            or len(content) > 8000 or "```" in content or contains_potential_credentials(content)
                            or not content.startswith("# Soul" if model_id == "agent-soul" else "# User")
                            or any(ord(c) < 32 and c not in "\n\t\r" for c in content)):
                        raise ValueError("generated profile failed validation")
                # Recheck sources after remote work; never bless a concurrent local edit.
                if self._sources() != sources:
                    raise ValueError("local profiles changed during bootstrap")
                if not self.exporter.state_path.exists():
                    self.exporter.register_baseline()
                state = _object(json.loads(self.exporter.state_path.read_text(encoding="utf-8")))
                if any(_object(state.get(key)).get("content_hash") != digest
                       for key, digest in hashes.items()) or self._sources() != sources:
                    raise ValueError("projection baseline differs from imported profiles")
                ProfileExporter.write_atomic(self.manifest_path, json.dumps(
                    {**expected, "status": "ready", "document_ids": documents}, sort_keys=True,
                ))
                return "ready"
            finally:
                if own_client:
                    await client.aclose()


async def _main() -> None:
    import argparse

    from nanobot.memory_sync.outbox import MemoryOutbox

    parser = argparse.ArgumentParser(description="Seed existing SOUL.md and USER.md; preserve local files")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--outbox", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--profile-state", required=True, type=Path)
    parser.add_argument("--pause-file", required=True, type=Path)
    parser.add_argument("--bank-id", default="personal")
    parser.add_argument("--api-url", default="http://127.0.0.1:8888")
    args = parser.parse_args()
    worker = HindsightWorker(MemoryOutbox(args.outbox, bank_id=args.bank_id, pause_path=args.pause_file),
                             api_url=args.api_url, bank_id=args.bank_id)
    bootstrap = ProfileBootstrap(ProfileExporter(worker, args.workspace, args.profile_state), args.manifest)
    deadline = asyncio.get_running_loop().time() + 1200
    previous = None
    while True:
        status = await bootstrap.run_once()
        if status != previous:
            print(json.dumps({"component": "profile_bootstrap", "status": status}), flush=True)
            previous = status
        if status == "ready":
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("bootstrap remains pending")
        await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception:
        # Never print exception bodies or imported/generated profile text.
        print('{"component":"profile_bootstrap","status":"needs_attention"}', flush=True)
        raise SystemExit(1) from None
