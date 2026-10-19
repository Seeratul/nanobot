"""Keep the full native ANN query; stop the import durably after a timeout."""
import asyncio
import functools
import inspect
import json
import os
import time
from pathlib import Path

HOLD = Path('/var/lib/hindsight/checkpoints/import-timeout.hold')
LIMIT = 120


def signal_timeout(worker, phase, *, path=HOLD, seeds=None, limit=LIMIT):
    if worker not in {'felix-pi', 'felix-pi-2'} or phase not in {'ann_timeout', 'task_timeout'}:
        raise ValueError('invalid timeout signal')
    event = {'worker': worker, 'phase': phase, 'at_unix': time.time(), 'limit_seconds': limit}
    if type(seeds) is int and seeds >= 0:
        event['seeds'] = seeds
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(event, handle)
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def is_timeout(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, TimeoutError) or getattr(error, 'sqlstate', None) == '57014':
            return True
        error = error.__cause__ or error.__context__
    return False


def install(worker, *, path=HOLD, limit=LIMIT, links=None, poller=None):
    if worker not in {'felix-pi', 'felix-pi-2'} or not 0 < limit <= LIMIT:
        raise ValueError('invalid timeout guard configuration')
    if links is None:
        from hindsight_api.engine.memories.pg import links
    if poller is None:
        from hindsight_api.worker.poller import WorkerPoller as poller
    original = links.compute_semantic_links_ann
    if getattr(original, '_import_timeout_stop', False):
        return
    signature = inspect.signature(original)
    if not {'unit_ids', 'embeddings', 'fact_types'} <= set(signature.parameters):
        raise RuntimeError('unsupported native ANN interface')

    @functools.wraps(original)
    async def full_query(*args, **kwargs):
        if path.exists():
            raise RuntimeError('import halted after timeout')
        bound = signature.bind(*args, **kwargs)
        try:
            # One call with the COMPLETE original arrays: no slicing, reduced
            # top-k, changed threshold, dropped seeds or substitute search.
            async with asyncio.timeout(limit):
                return await original(*args, **kwargs)
        except Exception as error:
            if is_timeout(error):
                signal_timeout(worker, 'ann_timeout', path=path,
                               seeds=len(bound.arguments['unit_ids']), limit=limit)
            raise

    full_query._import_timeout_stop = True
    links.compute_semantic_links_ann = full_query
    original_claim = poller.claim_batch

    @functools.wraps(original_claim)
    async def claim(self, *args, **kwargs):
        if path.exists():
            return []
        return await original_claim(self, *args, **kwargs)

    poller.claim_batch = claim
    original_failed = poller._mark_failed
    original_retry = poller._schedule_retry
    failed_signature = inspect.signature(original_failed)
    retry_signature = inspect.signature(original_retry)

    def timeout_message(message):
        lowered = str(message).lower()
        return any(token in lowered for token in ['timeout', 'timed out', 'wall-clock limit',
                                                  'statement cancellation', 'statement cancel'])

    @functools.wraps(original_failed)
    async def failed(self, *args, **kwargs):
        values = failed_signature.bind(self, *args, **kwargs).arguments
        if timeout_message(values.get('error_message', '')):
            signal_timeout(worker, 'task_timeout', path=path, limit=limit)
        return await original_failed(self, *args, **kwargs)

    @functools.wraps(original_retry)
    async def retry(self, *args, **kwargs):
        values = retry_signature.bind(self, *args, **kwargs).arguments
        if path.exists() or timeout_message(values.get('error_message', '')):
            signal_timeout(worker, 'task_timeout', path=path, limit=limit)
            # Do not schedule another native attempt. The root-owned path gate
            # stops both workers and the importer; unit conditions block restart.
            return await original_failed(self, values['operation_id'], values['error_message'], values['schema'])
        return await original_retry(self, *args, **kwargs)

    poller._mark_failed = failed
    poller._schedule_retry = retry
