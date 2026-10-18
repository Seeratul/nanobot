"""Install local usage/recovery hooks, then run the native worker entrypoint."""
import functools
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

from usage_ledger import DATABASE, WORKERS, record_estimate


def install_hook(provider_class: Any, worker: str, path: Path = DATABASE) -> None:
    if worker not in WORKERS:
        raise ValueError('unknown worker')
    original = provider_class.call
    if getattr(original, '_numeric_usage_hook', False):
        return

    @functools.wraps(original)
    async def tracked(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, *args, **kwargs)
        if getattr(self, 'model', None) != 'gpt-6-luna':
            return result
        # The third-party provider returns its own character/4 estimates.
        # Never serialize args, result, exceptions, schemas or response content.
        usage = getattr(result, 'usage', None)
        input_tokens = getattr(usage, 'input_tokens', None)
        output_tokens = getattr(usage, 'output_tokens', None)
        if (type(input_tokens) is int and input_tokens >= 0
                and type(output_tokens) is int and output_tokens >= 0):
            try:
                record_estimate(worker, input_tokens, output_tokens, path)
            except (OSError, sqlite3.Error):
                # A ledger failure must not repeat an already paid-for LLM call.
                logging.getLogger(__name__).error('numeric usage ledger write failed')
        return result

    tracked._numeric_usage_hook = True
    provider_class.call = tracked


def main() -> None:
    from hindsight_api.engine.providers.codex_llm import CodexLLM
    from hindsight_api.worker.main import main as native_main

    worker = sys.argv[sys.argv.index('--worker-id') + 1]
    install_hook(CodexLLM, worker)
    from retain_checkpoints import install as install_checkpoints

    install_checkpoints(worker)
    native_main()


if __name__ == '__main__':
    main()
