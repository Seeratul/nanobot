"""Apply the authorized two-worker, four-slot, four-call configuration while stopped."""
import asyncio
import importlib.metadata
import json
import os
import pwd
import subprocess
from pathlib import Path

import asyncpg
from switch_openrouter import atomic

DATA = Path('/var/lib/hindsight')


async def recovery_allowance(config):
    connection = await asyncpg.connect(config['HINDSIGHT_API_DATABASE_URL'], timeout=10)
    try:
        maximum = await connection.fetchval(
            "SELECT max(retry_count) FROM async_operations WHERE status='processing'")
    finally:
        await connection.close()
    result = max(int(config.get('HINDSIGHT_API_WORKER_MAX_RETRIES', '2')),
                 maximum + 1 if maximum is not None else 0)
    if result > 5:
        raise RuntimeError('task recovery allowance requires review')
    return result


def main():
    if os.geteuid() != 0:
        raise RuntimeError('root required')
    for name in ['hindsight-history-import', 'hindsight-worker']:
        status = subprocess.check_output(
            ['systemctl', 'show', name, '-p', 'ActiveState', '--value'], text=True).strip()
        if status not in ['inactive', 'failed']:
            raise RuntimeError('stop importer and workers before configuration')
    for relative in ['import-timeout.hold', 'checkpoints/import-timeout.hold',
                     'control/forget-pending']:
        if (DATA / relative).exists():
            raise RuntimeError('active memory hold blocks configuration')
    if importlib.metadata.version('hindsight-api-slim') != '1.0.0':
        raise RuntimeError('unsupported native version')
    path = DATA / 'runtime.json'
    config = json.loads(path.read_text())
    for phase in ['RETAIN', 'CONSOLIDATION', 'REFLECT', 'MENTAL_MODEL_REFRESH']:
        if config.get(f'HINDSIGHT_API_{phase}_LLM_MAX_CONCURRENT') not in [None, '', '4', 4]:
            raise RuntimeError('phase-specific concurrency override requires review')
    previous = {key: config.get(key) for key in [
        'HINDSIGHT_API_WORKER_MAX_SLOTS', 'HINDSIGHT_API_LLM_MAX_CONCURRENT']}
    config['HINDSIGHT_API_WORKER_MAX_SLOTS'] = '4'
    config['HINDSIGHT_API_LLM_MAX_CONCURRENT'] = '4'
    config['HINDSIGHT_API_WORKER_MAX_RETRIES'] = str(asyncio.run(recovery_allowance(config)))
    owner = pwd.getpwnam('hindsight')
    atomic(path, json.dumps(config), owner.pw_uid, owner.pw_gid)
    print(json.dumps({'workers': 2, 'slots_per_worker': 4, 'llm_calls_per_worker': 4,
                      'previous': previous, 'recovery_allowance':
                      config['HINDSIGHT_API_WORKER_MAX_RETRIES'], 'started': False}))


if __name__ == '__main__':
    main()
