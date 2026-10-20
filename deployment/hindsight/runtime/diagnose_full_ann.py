"""Replay cached native ANN inputs; no provider calls or persistent memory writes."""
import argparse
import asyncio
import collections
import dataclasses
import functools
import importlib.util
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import traceback
import zlib
from pathlib import Path
from types import SimpleNamespace

import asyncpg

DATA = Path('/var/lib/hindsight')
ROOT = Path('/var/tmp/hindsight-ann-replay-20261019')


def require_stopped():
    for name in ['hindsight-worker', 'hindsight-history-import']:
        status = subprocess.check_output(
            ['systemctl', 'show', name, '-p', 'ActiveState', '--value'], text=True).strip()
        if status not in ['inactive', 'failed']:
            raise RuntimeError('diagnosis requires stopped workers and importer')
    if not (DATA / 'import-timeout.hold').exists():
        raise RuntimeError('preserve the real timeout hold during diagnosis')
    if (DATA / 'control/forget-pending').exists():
        raise RuntimeError('fact retirement blocks checkpoint access')


async def prepare(config):
    sys.path.insert(0, '/opt/hindsight')
    import retain_checkpoints as checkpoints
    from hindsight_api.config import _get_raw_config
    from hindsight_api.engine.retain import fact_extraction, orchestrator

    cache = sqlite3.connect('file:' + str(DATA / 'checkpoints/recovery.sqlite3') + '?mode=ro',
                            uri=True)
    saved_scope, saved_plan = cache.execute(
        "SELECT scope,key FROM plans WHERE total=910 AND state='complete' "
        "ORDER BY updated DESC LIMIT 1").fetchone()
    operation = cache.execute('SELECT operation FROM scopes WHERE scope=?',
                              (saved_scope,)).fetchone()[0]

    class ReadOnlyStore:
        def __init__(self, path):
            pass

        def get(self, scope, kind, key):
            row = cache.execute('SELECT payload FROM entries WHERE scope=? AND kind=? AND key=?',
                                (scope, kind, key)).fetchone()
            return zlib.decompress(row[0]) if row else None

        def register(self, scope, identity, epoch):
            if scope != saved_scope or identity != operation:
                raise RuntimeError('checkpoint scope differs')

        def put(self, *args, **kwargs):
            raise RuntimeError('checkpoint write forbidden')

        def failure(self, *args, **kwargs):
            pass

        event = failure

    original_chunk = fact_extraction._extract_facts_with_auto_split

    @functools.wraps(original_chunk)
    async def forbid_model_call(*args, **kwargs):
        raise RuntimeError('cache miss; model call forbidden')

    fact_extraction._extract_facts_with_auto_split = forbid_model_call
    checkpoints.Store = ReadOnlyStore
    checkpoints.install('felix-pi')

    class CachedEmbeddings:
        model = config['HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL']
        base_url = config['HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL']
        dimensions = int(config['HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS'])

        async def embed_documents_async(self, texts):
            scope = checkpoints.CURRENT.get()
            if scope is None:
                raise RuntimeError('missing checkpoint scope')
            size = int(config['HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE'])
            key = checkpoints.digest([checkpoints.embedding_key(self, texts, scope.version), size])
            if key != saved_plan:
                raise RuntimeError('embedding input order differs from failed plan')
            vectors = []
            for offset in range(0, len(texts), size):
                part = texts[offset:offset + size]
                raw = scope.store.get(scope.key, 'embedding',
                                      checkpoints.embedding_key(self, part, scope.version))
                if raw is None:
                    raise RuntimeError('embedding cache miss; provider call forbidden')
                vectors.extend(checkpoints.validate_vectors(json.loads(raw), len(part), self.dimensions))
            return vectors

    db = await asyncpg.connect(config['HINDSIGHT_API_DATABASE_URL'], timeout=15)
    try:
        row = await db.fetchrow('SELECT task_payload FROM async_operations WHERE operation_id=$1',
                                operation)
        task = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        overrides = await db.fetchval('SELECT config FROM banks WHERE bank_id=$1', task['bank_id'])
        overrides = json.loads(overrides) if isinstance(overrides, str) else overrides or {}
        raw_config = _get_raw_config()
        resolved = dataclasses.replace(raw_config, **overrides)
        contents = orchestrator._build_contents(task['contents'], None)
        chunks = orchestrator._chunk_contents_for_delta(contents, resolved)
        stored = {r['chunk_index']: r['chunk_text'] for r in await db.fetch(
            'SELECT chunk_index,chunk_text FROM chunks WHERE bank_id=$1 AND document_id=$2',
            task['bank_id'], task['contents'][0]['document_id'])}
    finally:
        await db.close()
    missing = [i for i, text in chunks.items() if stored.get(i) != text]
    delta, _ = orchestrator._build_delta_contents(contents, chunks, missing)
    embedded = await orchestrator._extract_and_embed(
        delta, SimpleNamespace(model=resolved.llm_model, provider=resolved.llm_provider),
        resolved, CachedEmbeddings(), lambda date: date.strftime('%B %Y'), None, [],
        operation_id=operation, schema=None)
    facts = embedded.processed_facts
    payload = {'bank': task['bank_id'], 'vectors': [[float(v) for v in f.embedding] for f in facts],
               'types': [f.fact_type for f in facts],
               'threshold': resolved.semantic_link_min_similarity}
    if len(facts) != 910:
        raise RuntimeError('replayed fact count differs')
    ROOT.mkdir(mode=0o700, exist_ok=True)
    temporary = ROOT / 'inputs.json.z.tmp'
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(zlib.compress(json.dumps(payload).encode()))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, ROOT / 'inputs.json.z')
    print(json.dumps({'cache_replay': True, 'vectors': len(facts),
                      'types': dict(collections.Counter(payload['types'])),
                      'missing_chunks': len(missing), 'exact_embedding_plan': True,
                      'model_calls': 0, 'persistent_memory_writes': 0}), flush=True)


def summarize_plan(node):
    result = []
    def visit(item):
        index = item.get('Index Name', '')
        result.append({'node': item['Node Type'], 'estimated_rows': item.get('Plan Rows'),
                       'vector_index': 'hnsw' in index or index.startswith('idx_mu_emb_')})
        for child in item.get('Plans', []):
            visit(child)
    visit(node)
    return result


async def run(config, prefer_index, patched=False):
    from hindsight_api.engine.memories.pg import links as native_links

    if patched:
        spec = importlib.util.spec_from_file_location(
            'ann_plan_patch_under_test',
            '/var/tmp/hindsight-ann-plan-fix-20261020/ann_timeout_stop.py')
        patch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(patch)
        patch.install('felix-pi', path=ROOT / 'patched-test.hold')
    compute_semantic_links_ann = native_links.compute_semantic_links_ann

    payload = json.loads(zlib.decompress((ROOT / 'inputs.json.z').read_bytes()))
    if len(payload['vectors']) != 910 or len(payload['types']) != 910:
        raise RuntimeError('failed input is incomplete')
    conn = await asyncpg.connect(config['HINDSIGHT_API_DATABASE_URL'], timeout=15,
                                 command_timeout=120,
                                 server_settings={'statement_timeout': '120000'})
    plans = []

    class MeasuredConnection:
        def __getattr__(self, name):
            return getattr(conn, name)

        async def fetch(self, query, *args, **kwargs):
            if 'WITH seeds AS MATERIALIZED' in query:
                raw = await conn.fetchval('EXPLAIN (FORMAT JSON) ' + query, *args)
                plan = summarize_plan(json.loads(raw)[0]['Plan'])
                plans.append({'fact_type': args[1], 'plan': plan})
                print(json.dumps({'phase': 'query_plan', 'prefer_index': prefer_index,
                                  'fact_type': args[1], 'plan': plan}), flush=True)
            return await conn.fetch(query, *args, **kwargs)

    started = time.monotonic()
    result = {'prefer_index': prefer_index, 'patched_wrapper': patched,
              'seeds': len(payload['vectors']),
              'types': dict(collections.Counter(payload['types'])),
              'search_settings_changed': False, 'model_calls': 0}
    print(json.dumps({'phase': 'full_query_started', **result}), flush=True)
    try:
        async with asyncio.timeout(120), conn.transaction():
            if prefer_index:
                # Discourage a per-seed sort only in this transaction. ef_search,
                # iterative_scan, seed count, threshold and native top-k stay intact.
                await conn.execute('SET LOCAL enable_sort=off')
            links = await compute_semantic_links_ann(
                MeasuredConnection(), payload['bank'],
                [str(i) for i in range(len(payload['vectors']))], payload['vectors'],
                fact_types=payload['types'], threshold=payload['threshold'], log_buffer=[])
            result.update(result='success', links=len(links))
            if patched:
                result['planner_setting_restored'] = await conn.fetchval('SHOW enable_sort') == 'on'
            prefix = 'patched' if patched else 'index' if prefer_index else 'native'
            link_path = ROOT / (prefix + '-links.json.z')
            descriptor = os.open(link_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(zlib.compress(json.dumps(links).encode()))
    except Exception as error:
        result.update(result=type(error).__name__, frames=[
            {'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
            for f in traceback.extract_tb(error.__traceback__)
            if 'hindsight_api' in f.filename or 'asyncpg' in f.filename])
    finally:
        result['seconds'] = round(time.monotonic() - started, 3)
        result['plans'] = plans
        await conn.close()
    prefix = 'patched' if patched else 'index' if prefer_index else 'native'
    report_path = ROOT / (prefix + '-report.json')
    descriptor = os.open(report_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(result, handle)
    print(json.dumps(result), flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'native', 'prefer-index', 'patched'])
    args = parser.parse_args()
    require_stopped()
    config = json.loads((DATA / 'runtime.json').read_text())
    os.environ.update(config)
    logging.disable(logging.CRITICAL)
    if args.action == 'prepare':
        await prepare(config)
    else:
        await run(config, args.action == 'prefer-index', args.action == 'patched')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as error:
        print(json.dumps({'error_type': type(error).__name__, 'frames': [
            {'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
            for f in traceback.extract_tb(error.__traceback__)]}), flush=True)
        raise SystemExit(1) from None
