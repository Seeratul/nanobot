"""Deliver the durable outbox to the pinned Hindsight v1 HTTP API."""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from filelock import FileLock

from nanobot.memory_sync.outbox import MemoryJob, MemoryOutbox
from nanobot.security.network import PinnedDNSAsyncTransport, validate_url_target


class HindsightWorker:
    """No provider fallbacks: quota failures leave source segments queued."""

    def __init__(
        self, outbox: MemoryOutbox, *, api_url: str, bank_id: str,
        client: httpx.AsyncClient | None = None,
        hold_path: Path | None = None,
        max_delivery_attempts: int = 5,
        retry_window_seconds: int = 21600,
    ):
        parts = urlsplit(api_url)
        if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1", "localhost"}
                or parts.username or parts.password or parts.query or parts.fragment
                or parts.path not in {"", "/"}):
            raise ValueError("memory API must be an explicit HTTP loopback origin")
        self.outbox = outbox
        self.api_url = api_url.rstrip("/")
        self.prefix = f"/v1/default/banks/{quote(bank_id, safe='')}"
        self.client = client
        self.hold_path = hold_path
        if max_delivery_attempts < 1 or retry_window_seconds < 1:
            raise ValueError("delivery retry limits must be positive")
        self.max_delivery_attempts = max_delivery_attempts
        self.retry_window_seconds = retry_window_seconds
        self.lock_path = outbox.path.with_suffix(".delivery.lock")

    @staticmethod
    def backoff(attempts: int) -> float:
        return min(3600, 10 * 2 ** min(attempts, 9) * random.uniform(0.8, 1.2))

    def _retry_allowed(self, job: MemoryJob) -> bool:
        """A persisted budget spans API retries and process restarts.

        Completion/status reads remain allowed. The time window limits new
        attempts; it does not cancel already running remote work.
        """
        code = None
        if job.attempts >= self.max_delivery_attempts:
            code = "retry_budget_exhausted"
        elif (job.first_attempt_at is not None
              and time.time() - job.first_attempt_at >= self.retry_window_seconds):
            code = "retry_window_exhausted"
        if code:
            self.outbox.record(job.document_id, "needs_attention", error_code=code)
            return False
        return True

    async def run_once(self) -> str:
        """One serialized delivery/poll. Never acknowledge HTTP acceptance as completion."""
        with FileLock(self.lock_path, timeout=0):
            if self.hold_path is not None and self.hold_path.exists():
                return "paused"
            job = self.outbox.next_job()
            if job is None:
                return "idle"
            ok, _ = validate_url_target(self.api_url, allow_loopback=True)
            if not ok:
                raise ValueError("configured memory API is blocked by the URL guard")
            owned = self.client is None
            client = self.client or httpx.AsyncClient(
                transport=PinnedDNSAsyncTransport(allow_loopback=True),
                timeout=60, follow_redirects=False, trust_env=False,
            )
            delay = self.backoff(job.attempts)
            resume_state = "submitted" if job.state == "submitted" else "pending"
            operation_path = self.api_url + self.prefix + "/operations/" + quote(job.operation_id, safe="")
            try:
                # The operation UUID was persisted with the job before submission,
                # so a lost POST acknowledgement is reconciled on the next tick.
                response = await client.get(operation_path)
                if response.status_code == 404:
                    state = "not_found"
                else:
                    response.raise_for_status()
                    state = response.json().get("status")
                if state == "completed":
                    self.outbox.record(job.document_id, "completed")
                    return "completed"
                if state in {"pending", "processing"}:
                    self.outbox.record(job.document_id, "submitted", delay=15)
                    return "waiting"
                if state == "failed":
                    # Examine, but never log/store a provider error body. Only
                    # known transient quota/availability failures are retried.
                    error = str(response.json().get("error_message", "")).lower()
                    transient = any(word in error for word in (
                        "429", "rate limit", "rate_limit", "quota", "usage limit",
                        "timeout", "temporarily unavailable", "503", "502",
                        "clientconnectordnserror", "temporary failure in name resolution",
                        "name or service not known", "connection reset", "connection refused",
                    ))
                    if transient:
                        if not self._retry_allowed(job):
                            return "attention"
                        if self.hold_path is not None and self.hold_path.exists():
                            return "paused"
                        retried = await client.post(operation_path + "/retry")
                        retried.raise_for_status()
                        self.outbox.record(job.document_id, "submitted", delay=delay,
                                           attempted=True, error_code="remote_transient")
                        return "retry"
                    self.outbox.record(job.document_id, "needs_attention", error_code="remote_failed")
                    return "attention"
                if state == "not_found":
                    if not self._retry_allowed(job):
                        return "attention"
                    if self.hold_path is not None and self.hold_path.exists():
                        return "paused"
                    response = await client.post(
                        self.api_url + self.prefix + "/memories", json=json.loads(job.payload),
                    )
                    response.raise_for_status()
                    result = response.json()
                    if (result.get("success") is not True or result.get("async") is not True
                            or result.get("operation_id") != job.operation_id):
                        self.outbox.record(job.document_id, "needs_attention",
                                           error_code="unexpected_retain_response")
                        return "attention"
                    self.outbox.record(job.document_id, "submitted", delay=15, attempted=True)
                    return "submitted"
                self.outbox.record(job.document_id, "needs_attention", error_code="unexpected_operation_state")
                return "attention"
            except (httpx.TransportError, httpx.TimeoutException):
                if not self._retry_allowed(job):
                    return "attention"
                self.outbox.record(job.document_id, resume_state, delay=delay,
                                   attempted=True, error_code="transport_error")
                return "retry"
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in {408, 425, 429} or status >= 500:
                    if not self._retry_allowed(job):
                        return "attention"
                    self.outbox.record(job.document_id, resume_state, delay=delay,
                                       attempted=True, error_code=f"http_{status}")
                    return "retry"
                self.outbox.record(job.document_id, "needs_attention", error_code=f"http_{status}")
                return "attention"
            except (ValueError, AttributeError):
                self.outbox.record(job.document_id, "needs_attention", error_code="invalid_response")
                return "attention"
            finally:
                if owned:
                    await client.aclose()


async def serve(outbox_path: Path, api_url: str, bank_id: str) -> None:
    worker = HindsightWorker(MemoryOutbox(outbox_path, bank_id=bank_id),
                             api_url=api_url, bank_id=bank_id)
    while True:
        await worker.run_once()
        await asyncio.sleep(5)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Deliver original Nanobot segments to local Hindsight")
    parser.add_argument("--outbox", required=True, type=Path)
    parser.add_argument("--api-url", default="http://127.0.0.1:8888")
    parser.add_argument("--bank-id", default="personal")
    args = parser.parse_args()
    asyncio.run(serve(args.outbox, args.api_url, args.bank_id))


if __name__ == "__main__":
    main()
