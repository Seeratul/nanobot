"""Focused durability, counter reset, privacy and provider-wrapper checks."""
import asyncio
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from usage_ledger import parse_metrics, record_estimate, record_snapshot, report
from usage_worker import install_hook


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'usage.sqlite3'

    def test_restart_and_duplicate_scrapes(self):
        for calls in [10, 10, 12, 11]:
            record_snapshot('felix-pi', 'boot:pid:start', calls, 1, self.db)
        record_snapshot('felix-pi', 'boot:next:start', 3, 0, self.db)
        result = report(self.db)
        self.assertEqual(result['observed_successful_calls'], 15)
        self.assertEqual(result['observed_failed_calls'], 1)
        self.assertIsNone(result['estimates_first_recorded_utc'])
        self.assertIsNone(result['estimated_input_tokens'])

    def test_concurrent_durable_updates(self):
        record_estimate('felix-pi', 0, 0, self.db)
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: record_estimate('felix-pi', 10, 2, self.db), range(30)))
        result = report(self.db)
        self.assertEqual(result['calls_with_token_estimates'], 31)
        self.assertEqual(result['estimated_input_tokens'], 300)
        self.assertEqual(result['estimated_output_tokens'], 60)

    def test_metrics_discard_arbitrary_labels(self):
        text = ('hindsight_llm_calls_total{model="gpt-6-luna",success="true",private="DO_NOT_STORE"} 4.0\n'
                'hindsight_llm_calls_total{model="gpt-6-luna",success="false"} 2.0\n'
                'hindsight_llm_calls_total{model="another-model",success="true"} 99.0\n')
        self.assertEqual(parse_metrics(text), (4, 2))

    def test_hook_preserves_result_and_does_not_store_content(self):
        result = SimpleNamespace(usage=SimpleNamespace(input_tokens=17, output_tokens=5),
                                 content='PRIVATE_OUTPUT')

        class Provider:
            model = 'gpt-6-luna'

            async def call(self, messages):
                return result

        install_hook(Provider, 'felix-pi', self.db)
        install_hook(Provider, 'felix-pi', self.db)
        self.assertIs(asyncio.run(Provider().call('PRIVATE_PROMPT')), result)
        self.assertEqual(report(self.db)['calls_with_token_estimates'], 1)
        self.assertNotIn(b'PRIVATE_', self.db.read_bytes())
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_hook_preserves_failure_without_recording_fake_usage(self):
        class Provider:
            async def call(self):
                raise RuntimeError('PRIVATE_ERROR')

        install_hook(Provider, 'felix-pi', self.db)
        with self.assertRaises(RuntimeError):
            asyncio.run(Provider().call())
        self.assertEqual(report(self.db)['calls_with_token_estimates'], 0)


if __name__ == '__main__':
    unittest.main()
