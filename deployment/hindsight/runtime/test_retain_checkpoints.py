"""Recovery tests against the installed native interfaces, with no model requests."""
import asyncio
import functools
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hindsight_api.engine.embeddings import OpenAIEmbeddings
from retain_checkpoints import (
    CURRENT,
    CheckpointError,
    Scope,
    Store,
    embedding_key,
    generation,
    install,
    install_embedding_hooks,
    install_task_hooks,
)


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control = self.root / 'control'
        self.control.mkdir()
        self.path = self.root / 'private' / 'recovery.sqlite3'
        self.store = Store(self.path)
        self.epoch = generation(self.control)
        self.scope = Scope('operation-a', self.epoch, 'felix-pi', 'code-v1',
                           self.store, self.control)
        self.token = CURRENT.set(self.scope)
        self.addCleanup(CURRENT.reset, self.token)

        class Model(OpenAIEmbeddings):
            async def _embed_batch(self, batch):
                self.calls.append(batch[0])
                if batch[0] == self.fail_on:
                    self.fail_on = None
                    raise self.failure
                return [[float(x), -float(x)] for x in batch]

        install_embedding_hooks(Model)
        self.Model = Model

    def model(self):
        model = self.Model(api_key='synthetic-only', model='synthetic-model',
                           batch_size=8, dimensions=2, base_url='http://127.0.0.1:1')
        model.calls = []
        model.fail_on = None
        model.failure = TimeoutError('PRIVATE_PROVIDER_ERROR')
        return model

    async def encode(self, model, texts=None):
        return await model._encode_batched(
            texts if texts is not None else [str(i) for i in range(24)], model._embed_batch)

    async def test_failed_batch_resumes_after_reopening_database(self):
        first = self.model()
        first.fail_on = '16'
        with self.assertRaises(TimeoutError):
            await self.encode(first)
        progress = self.store.report()['embedding_plans'][0]
        self.assertEqual((progress['planned'], progress['completed'], progress['remaining']),
                         (24, 16, 8))
        self.assertEqual(progress['state'], 'interrupted')
        reopened = Store(self.path)
        CURRENT.set(Scope('operation-a', self.epoch, 'felix-pi', 'code-v1',
                          reopened, self.control))
        retry = self.model()
        result = await self.encode(retry)
        self.assertEqual(retry.calls, ['16'])
        self.assertEqual(result, [[float(i), -float(i)] for i in range(24)])
        self.assertEqual(reopened.report()['embedding_plans'][0]['remaining'], 0)
        self.assertEqual(reopened.report()['embedding_plans'][0]['state'], 'complete')
        again = self.model()
        await self.encode(again)
        self.assertEqual(again.calls, [])

    async def test_cancelled_batch_keeps_previous_batches(self):
        model = self.model()
        model.fail_on = '16'
        model.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.encode(model)
        self.assertEqual(self.store.report()['embedding_plans'][0]['completed'], 16)
        self.assertEqual(self.store.report()['failures'][0]['category'], 'cancelled')

    async def test_duplicate_batches_have_correct_progress(self):
        model = self.model()
        await self.encode(model, [str(i) for i in range(8)] * 2)
        self.assertEqual(model.calls, ['0'])
        row = self.store.report()['embedding_plans'][0]
        self.assertEqual((row['planned'], row['completed'], row['remaining']), (16, 16, 0))

    async def test_scopes_and_model_versions_do_not_share_results(self):
        await self.encode(self.model())
        other = self.model()
        other.model = 'different-model'
        await self.encode(other)
        self.assertEqual(other.calls, ['0', '8', '16'])
        CURRENT.set(Scope('operation-b', self.epoch, 'felix-pi-2', 'code-v1',
                          self.store, self.control))
        other = self.model()
        await self.encode(other)
        self.assertEqual(other.calls, ['0', '8', '16'])

    async def test_forget_marker_invalidates_live_scope_and_new_namespace(self):
        await self.encode(self.model())
        marker = self.control / 'forget-pending'
        marker.touch()
        with self.assertRaises(CheckpointError):
            await self.encode(self.model())
        marker.unlink()
        self.assertNotEqual(generation(self.control), self.epoch)
        with self.assertRaises(CheckpointError):
            await self.encode(self.model())

    async def test_bad_vector_checkpoint_fails_closed(self):
        model = self.model()
        texts = [str(i) for i in range(8)]
        key = embedding_key(model, texts, self.scope.version)
        self.store.put(self.scope.key, 'embedding', key, b'[[1.0]]')
        with self.assertRaises(CheckpointError):
            await self.encode(model, texts)
        self.assertEqual(model.calls, [])

    async def test_prune_only_removes_selected_recovery_scope(self):
        await self.encode(self.model())
        CURRENT.set(Scope('operation-b', self.epoch, 'felix-pi-2', 'code-v1',
                          self.store, self.control))
        await self.encode(self.model())
        self.store.prune({'operation-a'})
        plans = self.store.report()['embedding_plans']
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['worker'], 'felix-pi-2')
        again = self.model()
        await self.encode(again)
        self.assertEqual(again.calls, [])

    async def test_native_task_failure_category_survives_error_clearing(self):
        class Poller:
            async def _mark_failed(self, operation_id, error_message, schema):
                return 'native_failure_handled'

            async def _schedule_retry(self, operation_id, retry_at, error_message, schema):
                return 'native_retry_handled'

        install_task_hooks(Poller, 'felix-pi', self.store, 'v1', self.control)
        poller = Poller()
        identity = '00000000-0000-4000-8000-000000000001'
        result = await poller._mark_failed(identity, 'Task exceeded wall-clock limit PRIVATE', None)
        self.assertEqual(result, 'native_failure_handled')
        result = await poller._schedule_retry(identity, None, '429 PRIVATE', None)
        self.assertEqual(result, 'native_retry_handled')
        failures = self.store.report()['failures']
        self.assertEqual({x['category'] for x in failures}, {'task_wall_limit', 'quota_or_rate_limit'})
        self.assertNotIn('PRIVATE', json.dumps(failures))
        self.assertNotIn(b'PRIVATE', self.path.read_bytes())

    async def test_report_and_failure_storage_do_not_contain_input_or_errors(self):
        model = self.model()
        model.fail_on = 'PRIVATE_INPUT'
        with self.assertRaises(TimeoutError):
            await self.encode(model, ['PRIVATE_INPUT'])
        self.assertNotIn('PRIVATE', json.dumps(self.store.report()))
        self.assertNotIn(b'PRIVATE', self.path.read_bytes())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    async def test_native_extraction_hook_reuses_validated_facts_without_fake_usage(self):
        from hindsight_api.engine.retain import fact_extraction as facts
        from hindsight_api.engine.retain import orchestrator
        from hindsight_api.worker.poller import WorkerPoller

        native_chunk = facts._extract_facts_with_auto_split
        native_scope = orchestrator._extract_and_embed
        calls = []

        @functools.wraps(native_chunk)
        async def fake_chunk(*args, **kwargs):
            calls.append(1)
            return [facts.Fact(fact='Synthetic fact', fact_type='world')], facts.TokenUsage(
                input_tokens=17, output_tokens=3, total_tokens=20)

        @functools.wraps(native_scope)
        async def fake_scope(*args, **kwargs):
            return await facts._extract_facts_with_auto_split(
                chunk='Synthetic source', chunk_index=0, total_chunks=1, event_date=None,
                context='Synthetic context', llm_config=SimpleNamespace(model='test', provider='test'),
                config=SimpleNamespace(), metadata={},
                extraction_prompt=SimpleNamespace(system_prompt='Synthetic prompt',
                                                  response_schema=facts.Fact))

        with patch.object(facts, '_extract_facts_with_auto_split', fake_chunk), \
                patch.object(orchestrator, '_extract_and_embed', fake_scope), \
                patch.object(OpenAIEmbeddings, '_encode_batched', OpenAIEmbeddings._encode_batched), \
                patch.object(OpenAIEmbeddings, '_embed_batch', OpenAIEmbeddings._embed_batch), \
                patch.object(WorkerPoller, '_mark_failed', WorkerPoller._mark_failed), \
                patch.object(WorkerPoller, '_schedule_retry', WorkerPoller._schedule_retry):
            install('felix-pi', self.path, self.control)
            kwargs = {'contents': [], 'llm_config': None, 'config': None,
                      'embeddings_model': None, 'format_date_fn': None,
                      'fact_type_override': None, 'log_buffer': [],
                      'operation_id': '00000000-0000-4000-8000-000000000001'}
            first = await orchestrator._extract_and_embed(**kwargs)
            retry = await orchestrator._extract_and_embed(**kwargs)
            self.assertEqual(first[0], retry[0])
            self.assertEqual(first[1].input_tokens, 17)
            self.assertEqual(retry[1].input_tokens, 0)
            self.assertEqual(calls, [1])
            self.assertEqual(self.store.report()['checkpointed_extraction_units'], 1)

    def test_payload_corruption_is_detected(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO entries VALUES (?,?,?,?,?)', ('a', 'b', 'c', b'not-zlib', 0))
        with self.assertRaises(CheckpointError):
            self.store.get('a', 'b', 'c')


if __name__ == '__main__':
    unittest.main()
