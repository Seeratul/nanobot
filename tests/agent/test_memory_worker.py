"""Pinned HTTP contract and uncertain-delivery regression tests."""

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.worker import HindsightWorker


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.outbox = MemoryOutbox(Path(self.temp.name) / "outbox.sqlite3")
        self.outbox.enqueue("c1", "discord:test", ["a", "b"], ["Hello", "Hi"])
        self.job = self.outbox.next_job()

    def worker(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return HindsightWorker(self.outbox, api_url="http://127.0.0.1:8888",
                               bank_id="personal", client=client)

    async def test_acceptance_is_not_completion_and_operation_id_is_stable(self):
        seen = []
        def handler(request):
            seen.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"status": "not_found"})
            body = json.loads(request.content)
            self.assertEqual(body["operation_id"], self.job.operation_id)
            self.assertEqual(body["items"][0]["document_id"], self.job.document_id)
            return httpx.Response(200, json={"success": True, "async": True,
                                           "operation_id": self.job.operation_id})
        self.assertEqual(await self.worker(handler).run_once(), "submitted")
        self.assertEqual(self.outbox.status(), {"submitted": 1})
        self.assertEqual([r.method for r in seen], ["GET", "POST"])

    async def test_lost_ack_is_reconciled_before_any_replacement(self):
        calls = []
        def handler(request):
            calls.append(request.method)
            return httpx.Response(200, json={"status": "completed"})
        self.assertEqual(await self.worker(handler).run_once(), "completed")
        self.assertEqual(calls, ["GET"])
        self.assertEqual(self.outbox.status(), {"completed": 1})

    async def test_inflight_operation_is_only_polled(self):
        def handler(request):
            self.assertEqual(request.method, "GET")
            return httpx.Response(200, json={"status": "processing"})
        self.assertEqual(await self.worker(handler).run_once(), "waiting")
        self.assertEqual(self.outbox.status(), {"submitted": 1})

    async def test_quota_rejection_preserves_job_with_backoff(self):
        def handler(_request):
            return httpx.Response(429, json={"error": "quota"})
        self.assertEqual(await self.worker(handler).run_once(), "retry")
        self.assertEqual(self.outbox.status(), {"pending": 1})
        self.assertIsNone(self.outbox.next_job())
        self.assertIsNotNone(self.outbox.next_job(now=1e20))

    async def test_configuration_error_requires_attention(self):
        def handler(_request):
            return httpx.Response(401, json={"secret": "must not be persisted"})
        self.assertEqual(await self.worker(handler).run_once(), "attention")
        self.assertEqual(self.outbox.status(), {"needs_attention": 1})
        self.assertNotIn(b"must not be persisted", self.outbox.path.read_bytes())

    async def test_remote_quota_failure_retries_existing_operation(self):
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.method == "GET":
                return httpx.Response(200, json={"status": "failed", "error_message": "429 quota"})
            return httpx.Response(200, json={"success": True})
        self.assertEqual(await self.worker(handler).run_once(), "retry")
        self.assertTrue(paths[-1].endswith("/retry"))
        self.assertFalse(any(p.endswith("/memories") for p in paths))

    async def test_network_timeout_keeps_payload(self):
        def handler(request):
            raise httpx.ConnectTimeout("unavailable", request=request)
        self.assertEqual(await self.worker(handler).run_once(), "retry")
        self.assertEqual(self.outbox.status(), {"pending": 1})

    async def test_wrong_operation_id_is_not_acknowledged(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(404)
            return httpx.Response(200, json={"success": True, "async": True, "operation_id": "wrong"})
        self.assertEqual(await self.worker(handler).run_once(), "attention")
        self.assertEqual(self.outbox.status(), {"needs_attention": 1})

    def test_only_explicit_loopback_origins_are_allowed(self):
        for url in ["https://example.org", "http://192.168.0.1", "http://127.0.0.1@evil.example",
                    "http://127.0.0.1/path", "http://127.0.0.1?redirect=1"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                HindsightWorker(self.outbox, api_url=url, bank_id="personal")


if __name__ == "__main__":
    unittest.main()
