"""Verify profile cache behavior and failure handling without model calls."""

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.profiles import PROFILE_MODELS, ProfileExporter
from nanobot.memory_sync.worker import HindsightWorker


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state_path = self.root / "private" / "profile-state.json"
        for filename in PROFILE_MODELS.values():
            (self.workspace / filename).write_text("Original profile\n")

    def exporter(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        worker = HindsightWorker(MemoryOutbox(self.root / "outbox.sqlite3"),
                                 api_url="http://127.0.0.1:8888", bank_id="personal", client=client)
        return ProfileExporter(worker, self.workspace, self.state_path)

    @staticmethod
    def valid(request, *, content="# Profile\nConfirmed preference", failed=None):
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1],
                                        "content": content, "last_refreshed_at": "2026-10-15T12:00:00Z",
                                        "last_refresh_failed_at": failed})

    async def test_requires_explicit_baseline_and_keeps_originals(self):
        exporter = self.exporter(self.valid)
        with self.assertRaisesRegex(ValueError, "baseline"):
            await exporter.sync_once()
        self.assertEqual((self.workspace / "SOUL.md").read_text(), "Original profile\n")

    async def test_updates_both_files_using_saved_models_without_reflect(self):
        methods = []
        def handler(request):
            methods.append(request.method)
            return self.valid(request)
        exporter = self.exporter(handler)
        exporter.register_baseline()
        self.assertEqual(await exporter.sync_once(), {key: "updated" for key in PROFILE_MODELS})
        self.assertEqual(methods, ["GET", "GET"])
        self.assertIn("Confirmed preference", (self.workspace / "USER.md").read_text())
        self.assertEqual(await exporter.sync_once(), {key: "unchanged" for key in PROFILE_MODELS})

    async def test_outage_does_not_erase_last_good_profiles(self):
        exporter = self.exporter(lambda _r: httpx.Response(503))
        exporter.register_baseline()
        with self.assertRaises(httpx.HTTPStatusError):
            await exporter.sync_once()
        self.assertEqual((self.workspace / "SOUL.md").read_text(), "Original profile\n")

    async def test_incomplete_or_oversized_refresh_is_rejected(self):
        for content, failed in [("", None), ("x" * 8001, None), ("Unfinished", "2026-10-15"),
                                ("```shell\ncommands\n```", None), ("bad\x00text", None)]:
            with self.subTest(content=content[:12]):
                exporter = self.exporter(lambda r: self.valid(r, content=content, failed=failed))
                if not self.state_path.exists():
                    exporter.register_baseline()
                results = await exporter.sync_once()
                self.assertEqual(set(results.values()), {"invalid_or_unfinished"})
                self.assertEqual((self.workspace / "SOUL.md").read_text(), "Original profile\n")

    async def test_local_edits_are_not_silently_overwritten(self):
        exporter = self.exporter(self.valid)
        exporter.register_baseline()
        (self.workspace / "USER.md").write_text("Explicit local edit\n")
        results = await exporter.sync_once()
        self.assertEqual(results["user-profile"], "local_edit")
        self.assertEqual((self.workspace / "USER.md").read_text(), "Explicit local edit\n")

    async def test_failed_atomic_write_preserves_original(self):
        from unittest.mock import patch
        exporter = self.exporter(self.valid)
        exporter.register_baseline()
        with patch("nanobot.memory_sync.profiles.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await exporter.sync_once()
        self.assertEqual((self.workspace / "SOUL.md").read_text(), "Original profile\n")
        self.assertEqual(list(self.workspace.glob("*.tmp")), [])

    async def test_malformed_state_fails_closed(self):
        exporter = self.exporter(self.valid)
        exporter.register_baseline()
        self.state_path.write_text(json.dumps({"wrong": {}}))
        with self.assertRaisesRegex(ValueError, "state"):
            await exporter.sync_once()
        self.assertEqual((self.workspace / "SOUL.md").read_text(), "Original profile\n")


if __name__ == "__main__":
    unittest.main()
