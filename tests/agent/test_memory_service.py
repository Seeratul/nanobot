"""Background profile recovery and lifecycle boundaries."""

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from nanobot.memory_sync.profiles import ProfileExporter
from nanobot.memory_sync.service import run_profiles, serve


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_polling_recovers_after_outage_without_logging_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            exporter = ProfileExporter(AsyncMock(), path, path / "state.json")
            resumed = asyncio.Event()

            async def recovered():
                resumed.set()
                return {"agent-soul": "unchanged", "user-profile": "updated"}

            calls = 0

            async def poll():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise httpx.ConnectError("private transport payload")
                return await recovered()

            with patch.object(exporter, "sync_once", side_effect=poll), patch("builtins.print") as logged:
                task = asyncio.create_task(run_profiles(exporter, interval=0.001))
                try:
                    await asyncio.wait_for(resumed.wait(), timeout=1)
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                output = str(logged.call_args_list)
                self.assertIn("api_unavailable", output)
                self.assertIn("updated", output)
                self.assertNotIn("private transport payload", output)

    async def test_service_requires_initialized_profile_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            with self.assertRaisesRegex(ValueError, "baseline registration"):
                await serve(path / "outbox.sqlite3", "http://127.0.0.1:8888", "test",
                            path, path / "missing.json")
            self.assertFalse((path / "outbox.sqlite3").exists())
