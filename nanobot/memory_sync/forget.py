"""Quiesce the dedicated worker before deleting a source conversation."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote
from uuid import UUID

import httpx
from filelock import FileLock

from nanobot.memory_sync.profiles import PROFILE_MODELS, PROFILE_QUERIES, ProfileExporter
from nanobot.memory_sync.worker import HindsightWorker
from nanobot.security.network import PinnedDNSAsyncTransport


class WorkerControl(Protocol):
    async def stop(self) -> None: ...
    async def start(self) -> None: ...


class SystemdWorkerControl:
    """Observe a root-owned path unit; never escalate this process's privileges."""

    def __init__(self, pause_path: Path = Path("/var/lib/hindsight/control/forget-pending")):
        self.pause_path = pause_path

    async def _wait(self, *, stopped: bool) -> None:
        deadline = asyncio.get_running_loop().time() + 45
        while True:
            process = await asyncio.create_subprocess_exec(
                "systemctl", "show", "hindsight-worker.service",
                "--property=ActiveState,MainPID,ControlPID,KillMode,SendSIGKILL",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            state = dict(line.split("=", 1) for line in output.decode().splitlines() if "=" in line)
            inactive = (
                state.get("ActiveState") in {"inactive", "failed"}
                and state.get("MainPID") == "0" and state.get("ControlPID") == "0"
                and state.get("SendSIGKILL") == "yes" and state.get("KillMode") == "control-group"
            )
            if process.returncode == 0 and (
                inactive if stopped else state.get("ActiveState") == "active"
            ):
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("worker quiescence/resume could not be verified")
            await asyncio.sleep(1)

    async def stop(self) -> None:
        if not self.pause_path.exists():
            raise ValueError("durable worker pause signal is required")
        await self._wait(stopped=True)

    async def start(self) -> None:
        if self.pause_path.exists():
            raise ValueError("worker pause signal is still present")
        await self._wait(stopped=False)


class ForgetWorker:
    def __init__(self, worker: HindsightWorker, exporter: ProfileExporter, control: WorkerControl):
        self.worker = worker
        self.exporter = exporter
        self.control = control

    async def _cancel_tree(self, client: httpx.AsyncClient, base: str, operation_id: str) -> None:
        pending = [operation_id]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            UUID(current)
            seen.add(current)
            if len(seen) > 10000:
                raise ValueError("operation tree exceeds safety limit")
            operation = base + "/operations/" + quote(current, safe="")
            response = await client.get(operation)
            if response.status_code != 404:
                response.raise_for_status()
                raw: object = response.json()
                if not isinstance(raw, dict):
                    raise ValueError("invalid operation response")
                data = cast(dict[str, Any], raw)
                children = cast(object, data.get("child_operations", []))
                if children is None:
                    children = []
                if not isinstance(children, list):
                    raise ValueError("invalid child operation list")
                for child in cast(list[object], children):
                    if not isinstance(child, dict):
                        raise ValueError("invalid child operation")
                    child_id = cast(dict[str, Any], child).get("operation_id")
                    if not isinstance(child_id, str):
                        raise ValueError("invalid child operation identity")
                    pending.append(child_id)
            # Parent cancellation does not cancel child retains in Hindsight.
            response = await client.delete(operation)
            if response.status_code not in {200, 404, 409}:
                response.raise_for_status()

    async def run_once(self) -> str:
        with FileLock(self.worker.lock_path, timeout=0):
            request = self.worker.outbox.next_forget()
            if request is None and not self.worker.outbox.forget_pending():
                if self.worker.outbox.resume_needed():
                    await self.control.start()
                    self.worker.outbox.clear_resume()
                    return "resumed"
                return "idle"
            # Recover a crash after committing deletion but before removing the
            # pause marker. An orphan marker without this receipt stays closed.
            if request is None and self.worker.outbox.resume_needed():
                if self.worker.outbox.unpause_if_complete():
                    await self.control.start()
                    self.worker.outbox.clear_resume()
                    return "resumed"
            # A marker without a committed request is deliberately fail-closed.
            await self.control.stop()
            self.exporter.invalidate()
            if request is None:
                return "needs_attention"
            conversation_id, receipts = request
            owned = self.worker.client is None
            client = self.worker.client or httpx.AsyncClient(
                transport=PinnedDNSAsyncTransport(allow_loopback=True),
                timeout=60, follow_redirects=False, trust_env=False,
            )
            base = self.worker.api_url + self.worker.prefix
            try:
                version = await client.get(self.worker.api_url + "/version")
                version.raise_for_status()
                if version.json().get("features", {}).get("worker") is not False:
                    raise ValueError("forget requires API worker disabled")
                for document_id, operation_id in receipts:
                    await self._cancel_tree(client, base, operation_id)
                    response = await client.delete(base + "/documents/" + quote(document_id, safe=""))
                    if response.status_code != 404:
                        response.raise_for_status()
                # Delete saved generated content and any stale refresh result.
                # The stopped process cannot finish a prior generation later.
                for model_id in PROFILE_MODELS:
                    response = await client.delete(base + "/mental-models/" + model_id)
                    if response.status_code != 404:
                        response.raise_for_status()
                    response = await client.post(base + "/mental-models", json={
                        "id": model_id, "name": model_id,
                        "source_query": PROFILE_QUERIES[model_id],
                        "max_tokens": 750, "refresh_after_consolidation": True,
                    })
                    response.raise_for_status()
                self.worker.outbox.complete_forget(conversation_id)
                if self.worker.outbox.unpause_if_complete():
                    await self.control.start()
                    self.worker.outbox.clear_resume()
                return "forgotten"
            finally:
                if owned:
                    await client.aclose()
