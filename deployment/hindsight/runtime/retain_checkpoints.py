"""Private, operation-scoped recovery data; public reports contain counters only.

This is a startup adapter for the pinned Hindsight runtime, not a replacement
retain implementation. Native extraction, ordering, writes and forget gates stay
in charge. JSON/Pydantic validation is used; checkpoints never execute pickle.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import inspect
import json
import logging
import math
import os
import re
import sqlite3
import time
import uuid
import zlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATABASE = Path('/var/lib/hindsight/checkpoints/recovery.sqlite3')
CONTROL = Path('/var/lib/hindsight/control')
CURRENT: ContextVar[Scope | None] = ContextVar('retain_checkpoint_scope', default=None)
FORMAT = 'retain-checkpoint-v1'


class CheckpointError(RuntimeError):
    """Fail closed without including a prompt, response or provider error."""


def digest(value: Any) -> str:
    from pydantic_core import to_jsonable_python

    raw = json.dumps(to_jsonable_python(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                     allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def generation(control: Path = CONTROL) -> str:
    if (control / 'forget-pending').exists():
        raise CheckpointError('checkpoint access blocked by fact retirement')
    stat = control.stat()
    # Creation AND removal of the existing forget marker invalidate the namespace.
    # Invalidation also covers an interrupted retirement across worker restarts.
    return digest([stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns])


def category(error: BaseException) -> str:
    if isinstance(error, asyncio.CancelledError):
        return 'cancelled'
    if isinstance(error, TimeoutError) or 'timeout' in type(error).__name__.lower():
        return 'timeout'
    if any(k in type(error).__name__.lower() for k in ['ratelimit', 'quota']):
        return 'quota_or_rate_limit'
    status = getattr(error, 'status_code', None)
    if status == 429:
        return 'quota_or_rate_limit'
    if status in {401, 403}:
        return 'authentication'
    if isinstance(status, int) and 500 <= status < 600:
        return 'upstream_unavailable'
    if isinstance(status, int) and 400 <= status < 500:
        return 'request_rejected'
    if isinstance(error, CheckpointError):
        return 'checkpoint'
    return 'unclassified'


class Store:
    def __init__(self, path: Path = DATABASE):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS entries (
                    scope TEXT, kind TEXT, key TEXT, payload BLOB,
                    updated REAL NOT NULL, PRIMARY KEY(scope,kind,key));
                CREATE TABLE IF NOT EXISTS plans (
                    scope TEXT, key TEXT, worker TEXT, total INTEGER,
                    updated REAL, state TEXT, PRIMARY KEY(scope,key));
                CREATE TABLE IF NOT EXISTS members (
                    scope TEXT, plan TEXT, key TEXT, n INTEGER,
                    PRIMARY KEY(scope,plan,key));
                CREATE TABLE IF NOT EXISTS failures (
                    scope TEXT, worker TEXT, phase TEXT, category TEXT, at REAL);
                CREATE TABLE IF NOT EXISTS scopes (
                    scope TEXT PRIMARY KEY, operation TEXT, epoch TEXT);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA secure_delete=ON')
            with db:
                yield db
        finally:
            db.close()

    def get(self, scope: str, kind: str, key: str) -> bytes | None:
        with self.connect() as db:
            row = db.execute('SELECT payload FROM entries WHERE scope=? AND kind=? AND key=?',
                             (scope, kind, key)).fetchone()
        if row is None:
            return None
        try:
            return zlib.decompress(row[0])
        except (ValueError, TypeError, zlib.error) as error:
            raise CheckpointError('invalid checkpoint payload') from error

    def put(self, scope: str, kind: str, key: str, payload: bytes) -> None:
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?)',
                       (scope, kind, key, zlib.compress(payload), time.time()))

    def plan(self, scope: str, key: str, worker: str, batches: list[tuple[str, int]]) -> None:
        members: dict[str, int] = {}
        for batch, n in batches:
            members[batch] = members.get(batch, 0) + n
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?,?)',
                       (scope, key, worker, sum(n for _, n in batches), time.time(), 'in_progress'))
            db.executemany('INSERT OR REPLACE INTO members VALUES (?,?,?,?)',
                           [(scope, key, batch, n) for batch, n in members.items()])

    def finish(self, scope: str, key: str, state: str) -> None:
        with self.connect() as db:
            db.execute('UPDATE plans SET state=?,updated=? WHERE scope=? AND key=?',
                       (state, time.time(), scope, key))

    def failure(self, scope: str, worker: str, phase: str, error: BaseException) -> None:
        self.event(scope, worker, phase, category(error))

    def event(self, scope: str, worker: str, phase: str, kind: str) -> None:
        with self.connect() as db:
            db.execute('INSERT INTO failures VALUES (?,?,?,?,?)',
                       (scope, worker, phase, kind, time.time()))

    def register(self, scope: str, operation: str, epoch: str) -> None:
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO scopes VALUES (?,?,?)', (scope, operation, epoch))

    def prune(self, scopes: set[str]) -> None:
        # Delete recovery data only; original archives and Hindsight tables are untouched.
        with self.connect() as db:
            for scope in scopes:
                for table in ['entries', 'members', 'plans', 'scopes']:
                    db.execute(f'DELETE FROM {table} WHERE scope=?', (scope,))

    def report(self) -> dict[str, Any]:
        with self.connect() as db:
            plans = db.execute('''SELECT p.worker,p.total,coalesce(sum(
                CASE WHEN e.key IS NOT NULL THEN m.n ELSE 0 END),0),p.updated,p.state,max(e.updated)
                FROM plans p LEFT JOIN members m ON m.scope=p.scope AND m.plan=p.key
                LEFT JOIN entries e ON e.scope=m.scope AND e.key=m.key AND e.kind='embedding'
                GROUP BY p.scope,p.key ORDER BY p.updated''').fetchall()
            failures = db.execute('''SELECT worker,phase,category,count(*),max(at)
                FROM failures GROUP BY worker,phase,category''').fetchall()
            chunks = db.execute("SELECT count(*) FROM entries WHERE kind='extraction'").fetchone()[0]
        return {'checkpointed_extraction_units': chunks,
                'embedding_plans': [{'worker': worker, 'planned': total, 'completed': done,
                                     'remaining': total - done, 'updated_at_unix': updated,
                                     'state': state, 'last_checkpoint_at_unix': last}
                                    for worker, total, done, updated, state, last in plans],
                'failures': [{'worker': worker, 'phase': phase, 'category': kind,
                              'count': count, 'last_at_unix': at}
                             for worker, phase, kind, count, at in failures],
                'meaning': 'recovery checkpoints, not committed Hindsight source chunks'}


@dataclass(frozen=True)
class Scope:
    key: str
    epoch: str
    worker: str
    version: str
    store: Store
    control: Path

    def check(self) -> None:
        if generation(self.control) != self.epoch:
            raise CheckpointError('fact retirement invalidated checkpoints')


def embedding_key(model: Any, batch: list[str], version: str) -> str:
    return digest([version, getattr(model, 'model', None), getattr(model, 'base_url', None),
                   getattr(model, 'dimensions', None), batch])


def validate_vectors(value: Any, count: int, dimensions: int | None) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != count:
        raise CheckpointError('invalid checkpoint vector count')
    for vector in value:
        if (not isinstance(vector, list) or not vector
                or (dimensions is not None and len(vector) != dimensions)
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)):
            raise CheckpointError('invalid checkpoint vector')
    return value


def install_embedding_hooks(model_class: Any) -> None:
    if getattr(model_class._embed_batch, '_retain_checkpoint_hook', False):
        return
    original_batch = model_class._embed_batch
    original_encode = model_class._encode_batched

    @functools.wraps(original_batch)
    async def batch(self, texts):
        scope = CURRENT.get()
        if scope is None:
            return await original_batch(self, texts)
        scope.check()
        key = embedding_key(self, texts, scope.version)
        dimensions = getattr(self, 'dimensions', None)
        saved = scope.store.get(scope.key, 'embedding', key)
        if saved is not None:
            try:
                result = validate_vectors(json.loads(saved), len(texts), dimensions)
            except (ValueError, TypeError) as error:
                raise CheckpointError('invalid checkpoint vector payload') from error
            scope.check()
            return result
        try:
            result = validate_vectors(await original_batch(self, texts), len(texts), dimensions)
            scope.check()
            scope.store.put(scope.key, 'embedding', key,
                            json.dumps(result, allow_nan=False).encode())
            return result
        except BaseException as error:
            scope.store.failure(scope.key, scope.worker, 'embedding', error)
            raise

    @functools.wraps(original_encode)
    async def encode(self, texts, encode_batch, *, batch_size=None):
        scope = CURRENT.get()
        key = None
        if scope is not None:
            scope.check()
            size = batch_size if batch_size is not None else getattr(self, 'batch_size', 0)
            size = size if size and size > 0 else len(texts) or 1
            batches = [texts[i:i + size] for i in range(0, len(texts), size)]
            key = digest([embedding_key(self, texts, scope.version), size])
            scope.store.plan(scope.key, key, scope.worker,
                             [(embedding_key(self, part, scope.version), len(part)) for part in batches])
        try:
            result = await original_encode(self, texts, encode_batch, batch_size=batch_size)
        except BaseException:
            if scope is not None and key is not None:
                scope.store.finish(scope.key, key, 'interrupted')
            raise
        if scope is not None and key is not None:
            scope.store.finish(scope.key, key, 'complete')
        return result

    batch._retain_checkpoint_hook = True
    model_class._embed_batch = batch
    model_class._encode_batched = encode


def install_task_hooks(poller_class: Any, worker: str, store: Store,
                       version: str, control: Path) -> None:
    """Preserve a category before native retries clear the operation's error."""
    def wrap(name: str, phase: str) -> None:
        original = getattr(poller_class, name)
        if getattr(original, '_retain_checkpoint_hook', False):
            return
        signature = inspect.signature(original)

        @functools.wraps(original)
        async def recorded(self, *args, **kwargs):
            bound = signature.bind(self, *args, **kwargs).arguments
            # Inspect transiently; never write error text to the checkpoint database.
            message = str(bound.get('error_message', '')).lower()
            kind = 'unclassified'
            for tokens, value in [
                (['wall-clock limit'], 'task_wall_limit'),
                (['429', 'quota', 'rate limit', 'rate_limit'], 'quota_or_rate_limit'),
                (['timeout', 'timed out'], 'timeout'),
                (['deadlock', 'database'], 'database'),
                (['context size', 'context length'], 'input_limit'),
                (['502', '503'], 'upstream_unavailable'),
            ]:
                if any(token in message for token in tokens):
                    kind = value
                    break
            if bound.get('schema') in {None, 'public'}:
                # A failure must still reach native state handling if a forget
                # marker appeared concurrently; no recovery data is read here.
                try:
                    epoch = generation(control)
                    identity = str(uuid.UUID(str(bound['operation_id'])))
                    scope = digest([identity, epoch, version])
                    store.event(scope, worker, phase, kind)
                except (CheckpointError, OSError, sqlite3.Error):
                    logging.getLogger(__name__).error('numeric failure history unavailable')
            return await original(self, *args, **kwargs)

        recorded._retain_checkpoint_hook = True
        setattr(poller_class, name, recorded)

    wrap('_mark_failed', 'task_failure')
    wrap('_schedule_retry', 'task_retry')


def install(worker: str, path: Path = DATABASE, control: Path = CONTROL) -> None:
    """Bind private hooks to the installed pinned native modules at worker startup."""
    from hindsight_api.engine import embeddings
    from hindsight_api.engine.retain import fact_extraction, orchestrator
    from hindsight_api.worker.poller import WorkerPoller
    from pydantic import TypeAdapter

    if worker not in {'felix-pi', 'felix-pi-2'}:
        raise CheckpointError('unknown checkpoint worker')
    if getattr(orchestrator._extract_and_embed, '_retain_checkpoint_hook', False):
        return
    store = Store(path)
    source_version = digest([FORMAT,
                            {k: os.environ.get('HINDSIGHT_API_' + k) for k in [
                                'LLM_PROVIDER', 'LLM_MODEL', 'LLM_REASONING_EFFORT',
                                'RETAIN_LLM_PROVIDER', 'RETAIN_LLM_MODEL', 'RETAIN_LLM_REASONING_EFFORT',
                            ]}] + [hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                                   for module in [embeddings, fact_extraction, orchestrator]])
    original_scope = orchestrator._extract_and_embed
    scope_signature = inspect.signature(original_scope)
    original_chunk = fact_extraction._extract_facts_with_auto_split
    chunk_signature = inspect.signature(original_chunk)
    expected_chunk = {'chunk', 'chunk_index', 'total_chunks', 'event_date', 'context',
                      'llm_config', 'config', 'agent_name', 'metadata', 'attachment_loader',
                      'vlm_config', 'extraction_prompt'}
    if set(chunk_signature.parameters) != expected_chunk:
        raise CheckpointError('unsupported native extraction interface')
    if not {'contents', 'operation_id', 'schema'} <= set(scope_signature.parameters):
        raise CheckpointError('unsupported native retain interface')
    adapter = TypeAdapter(tuple[list[fact_extraction.Fact], fact_extraction.TokenUsage])
    config_fields = sorted(set(re.findall(
        r'config\.([a-z_]+)', Path(fact_extraction.__file__).read_text())))
    config_fields = [k for k in config_fields if k.startswith(('retain_', 'llm_', 'entities_'))
                     or k == 'entity_labels']

    @functools.wraps(original_scope)
    async def scoped(*args, **kwargs):
        bound = scope_signature.bind(*args, **kwargs)
        operation = bound.arguments.get('operation_id')
        if operation is None or bound.arguments.get('schema') not in {None, 'public'}:
            return await original_scope(*args, **kwargs)
        identity = str(uuid.UUID(str(operation)))
        epoch = generation(control)
        scope = Scope(digest([identity, epoch, source_version]), epoch, worker,
                      source_version, store, control)
        store.register(scope.key, identity, epoch)
        token = CURRENT.set(scope)
        try:
            return await original_scope(*args, **kwargs)
        except BaseException as error:
            store.failure(scope.key, worker, 'extraction_and_embedding', error)
            raise
        finally:
            CURRENT.reset(token)

    @functools.wraps(original_chunk)
    async def chunk(*args, **kwargs):
        scope = CURRENT.get()
        if scope is None:
            return await original_chunk(*args, **kwargs)
        scope.check()
        bound = chunk_signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        # Never cache attachment processing without fingerprinting the actual asset.
        if list(fact_extraction.attachment_content.iter_placeholder_ids(values['chunk'])):
            return await original_chunk(*args, **kwargs)
        config = values['config']
        prompt = values.get('extraction_prompt') or fact_extraction._build_extraction_prompt_and_schema(config)
        llm = values['llm_config']
        key = digest([scope.version,
                      {k: (v.isoformat() if hasattr(v, 'isoformat') else v)
                       for k, v in values.items() if k in {'chunk', 'chunk_index', 'total_chunks',
                                                         'event_date', 'context', 'metadata', 'agent_name'}},
                      {k: getattr(config, k, None) for k in config_fields},
                      getattr(llm, 'model', None), getattr(llm, 'provider', None),
                      prompt.system_prompt, prompt.response_schema.model_json_schema()])
        saved = store.get(scope.key, 'extraction', key)
        if saved is not None:
            try:
                facts, _usage = adapter.validate_json(saved)
            except ValueError as error:
                raise CheckpointError('invalid extraction checkpoint') from error
            scope.check()
            # Reusing an extraction must not create fabricated new model usage.
            return facts, fact_extraction.TokenUsage()
        try:
            result = await original_chunk(*args, **kwargs)
            encoded = adapter.dump_json(result, warnings='error')
            adapter.validate_json(encoded)
            scope.check()
            store.put(scope.key, 'extraction', key, encoded)
            return result
        except BaseException as error:
            store.failure(scope.key, worker, 'extraction', error)
            raise

    scoped._retain_checkpoint_hook = True
    orchestrator._extract_and_embed = scoped
    fact_extraction._extract_facts_with_auto_split = chunk
    install_embedding_hooks(embeddings.OpenAIEmbeddings)
    install_task_hooks(WorkerPoller, worker, store, source_version, control)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['report', 'prune'])
    args = parser.parse_args()
    store = Store()
    if args.action == 'prune':
        async def terminal_scopes() -> set[str]:
            import asyncpg

            config = json.loads(Path('/var/lib/hindsight/runtime.json').read_text())
            connection = await asyncpg.connect(config['HINDSIGHT_API_DATABASE_URL'], timeout=10)
            try:
                rows = await connection.fetch("SELECT operation_id FROM async_operations "
                                              "WHERE status IN ('completed','cancelled')")
                terminal = {str(row['operation_id']) for row in rows}
            finally:
                await connection.close()
            epoch = generation()
            with store.connect() as db:
                scopes = db.execute('SELECT scope,operation,epoch FROM scopes').fetchall()
            return {scope for scope, operation, old_epoch in scopes
                    if operation in terminal or old_epoch != epoch}

        obsolete = asyncio.run(terminal_scopes())
        store.prune(obsolete)
        print(json.dumps({'pruned_recovery_scopes': len(obsolete)}))
    else:
        print(json.dumps(store.report(), indent=2))


if __name__ == '__main__':
    main()
