import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ann_timeout_stop import install, is_timeout, signal_timeout


class TimeoutStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'hold'
        self.calls = []

    def poller(self):
        calls = self.calls

        class Poller:
            async def claim_batch(self):
                calls.append('claim')
                return ['synthetic']

            async def _mark_failed(self, operation_id, error_message, schema):
                calls.append('failed')

            async def _schedule_retry(self, operation_id, retry_at, error_message, schema):
                calls.append('retry')

        return Poller

    async def test_full_arrays_single_unchanged_call(self):
        ids = [str(i) for i in range(2853)]
        vectors = [[i] for i in range(2853)]
        result = object()

        async def native(conn, bank_id, unit_ids, embeddings, fact_types=None, *, threshold):
            self.assertIs(unit_ids, ids)
            self.assertIs(embeddings, vectors)
            self.assertEqual(threshold, 0.83)
            self.calls.append('ann')
            return result

        links = SimpleNamespace(compute_semantic_links_ann=native)
        install('felix-pi', path=self.path, links=links, poller=self.poller())
        actual = await links.compute_semantic_links_ann(None, 'synthetic', ids, vectors, threshold=0.83)
        self.assertIs(actual, result)
        self.assertEqual(self.calls, ['ann'])
        self.assertFalse(self.path.exists())

    async def test_deadline_creates_private_hold_and_blocks_claim_retry(self):
        async def native(conn, bank_id, unit_ids, embeddings, fact_types=None, *, threshold):
            await asyncio.sleep(1)

        links = SimpleNamespace(compute_semantic_links_ann=native)
        poller = self.poller()
        install('felix-pi', path=self.path, limit=0.01, links=links, poller=poller)
        with self.assertRaises(TimeoutError):
            await links.compute_semantic_links_ann(None, 'synthetic', ['a'], [[1]], threshold=0.5)
        event = json.loads(self.path.read_text())
        self.assertEqual(event['phase'], 'ann_timeout')
        self.assertEqual(event['seeds'], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(await poller().claim_batch(), [])
        await poller()._schedule_retry('synthetic', 0, 'TimeoutError private text', None)
        self.assertEqual(self.calls, ['failed'])
        self.assertNotIn('private', self.path.read_text())

    async def test_other_failures_do_not_hold_or_change_retry(self):
        async def native(conn, bank_id, unit_ids, embeddings, fact_types=None, *, threshold):
            raise ValueError('synthetic failure')

        links = SimpleNamespace(compute_semantic_links_ann=native)
        poller = self.poller()
        install('felix-pi-2', path=self.path, links=links, poller=poller)
        with self.assertRaises(ValueError):
            await links.compute_semantic_links_ann(None, 'synthetic', [], [], threshold=0.5)
        await poller()._schedule_retry('synthetic', 0, 'unavailable', None)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.calls, ['retry'])

    async def test_task_timeout_without_ann_is_terminal(self):
        async def native(conn, bank_id, unit_ids, embeddings, fact_types=None, *, threshold):
            return []

        links = SimpleNamespace(compute_semantic_links_ann=native)
        poller = self.poller()
        install('felix-pi-2', path=self.path, links=links, poller=poller)
        await poller()._schedule_retry('synthetic', 0, 'TimeoutError', None)
        self.assertEqual(self.calls, ['failed'])
        self.assertEqual(json.loads(self.path.read_text())['phase'], 'task_timeout')

    async def test_marker_keeps_first_signal_and_error_chain(self):
        signal_timeout('felix-pi', 'ann_timeout', path=self.path, seeds=2853)
        first = self.path.read_bytes()
        signal_timeout('felix-pi-2', 'task_timeout', path=self.path)
        self.assertEqual(first, self.path.read_bytes())
        wrapped = RuntimeError('synthetic')
        wrapped.__cause__ = TimeoutError()
        self.assertTrue(is_timeout(wrapped))


if __name__ == '__main__':
    unittest.main()
