"""Bound native consolidation retries without changing retain/ANN execution."""
import functools
import os


def install(poller_class=None, max_retries=None):
    if poller_class is None:
        from hindsight_api.worker.poller import WorkerPoller
        poller_class = WorkerPoller
    limit = int(os.environ.get('HINDSIGHT_CONSOLIDATION_MAX_RETRIES', '4')) if max_retries is None else max_retries
    if limit < 0:
        raise ValueError('retry limit must not be negative')
    original = poller_class._schedule_retry_all
    if getattr(original, '_bounded_consolidation_retries', False) is True:
        return

    @functools.wraps(original)
    async def bounded(self, task, retry_at, reason):
        if (task.task_dict.get('operation_type') == 'consolidation'
                and task.task_dict.get('_retry_count', 0) >= limit):
            await self._mark_all_failed(task, 'retry budget exhausted: consolidation')
            return
        return await original(self, task, retry_at, reason)

    bounded._bounded_consolidation_retries = True
    poller_class._schedule_retry_all = bounded
