"""Install checkpoint hooks only after the importer and workers are stopped."""
import asyncio
import importlib.metadata
import json
import os
import pwd
import subprocess
from pathlib import Path

import asyncpg
from install_parallel_workers import atomic

ROOT = Path('/opt/hindsight')
RUNTIME = Path('/var/lib/hindsight/runtime.json')


async def recovery_limit(config):
    connection = await asyncpg.connect(config['HINDSIGHT_API_DATABASE_URL'], timeout=10)
    try:
        maximum = await connection.fetchval("SELECT max(retry_count) FROM async_operations "
                                            "WHERE status='processing' AND worker_id "
                                            "IN ('felix-pi','felix-pi-2')")
    finally:
        await connection.close()
    configured = int(config.get('HINDSIGHT_API_WORKER_MAX_RETRIES', '1'))
    needed = max(configured, (maximum + 1) if maximum is not None else configured)
    if needed > 5:
        raise RuntimeError('recovery budget needs manual review')
    return needed


def main():
    if os.geteuid() != 0:
        raise SystemExit('root required')
    for unit in ['hindsight-history-import', 'hindsight-worker']:
        state = subprocess.check_output(
            ['systemctl', 'show', unit, '--property=ActiveState', '--value'], text=True).strip()
        if state not in {'inactive', 'failed'}:
            raise SystemExit('stop importer and shared worker before installing checkpoints')
    if Path('/var/lib/hindsight/control/forget-pending').exists():
        raise SystemExit('pending fact retirement blocks migration')
    if importlib.metadata.version('hindsight-api-slim') != '1.0.0':
        raise SystemExit('unsupported native runtime')
    if 'worker_supervisor.py' not in (ROOT / 'launch.py').read_text():
        raise SystemExit('unexpected worker launcher')
    config = json.loads(RUNTIME.read_text())
    limit = asyncio.run(recovery_limit(config))
    account = pwd.getpwnam('hindsight')
    directory = Path('/var/lib/hindsight/checkpoints')
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chown(directory, account.pw_uid, account.pw_gid)
    os.chmod(directory, 0o700)
    for name in ['retain_checkpoints.py', 'usage_worker.py']:
        source = Path(__file__).with_name(name).read_text()
        atomic(ROOT / name, source, 0o644)
        atomic(ROOT / 'maintenance' / name, source, 0o644)
    # This separate interpreter checks native signatures/serializers without
    # starting a worker, connecting a provider, or modifying the live process.
    validation = '''import json, os, sys
from pathlib import Path
os.environ.update(json.loads(Path('/var/lib/hindsight/runtime.json').read_text()))
sys.path.insert(0, '/opt/hindsight')
from retain_checkpoints import install
install('felix-pi')
'''
    subprocess.run(['sudo', '-u', 'hindsight', '/opt/hindsight/venv/bin/python', '-c', validation],
                   check=True)
    # Native startup recovery otherwise fails a task already at its recovery cap.
    # Grant only the one required recovery; never reset task rows or retry counts.
    config['HINDSIGHT_API_WORKER_MAX_RETRIES'] = str(limit)
    metadata = RUNTIME.stat()
    atomic(RUNTIME, json.dumps(config), metadata.st_mode & 0o777,
           metadata.st_uid, metadata.st_gid)
    print(json.dumps({'installed': True, 'native_recovery_limit': limit, 'started': False}))


if __name__ == '__main__':
    main()
