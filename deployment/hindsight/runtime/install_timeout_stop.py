"""Install the requested full-query 120s limit and persistent timeout stop."""
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
OPT = Path('/opt/hindsight')
HOLD = DATA / 'import-timeout.hold'


async def recovery_limit(config):
    db = await asyncpg.connect(config['HINDSIGHT_API_DATABASE_URL'], timeout=10)
    try:
        maximum = await db.fetchval("SELECT max(retry_count) FROM async_operations WHERE status='processing'")
    finally:
        await db.close()
    result = max(int(config.get('HINDSIGHT_API_WORKER_MAX_RETRIES', '2')),
                 0 if maximum is None else maximum + 1)
    if result > 5:
        raise RuntimeError('recovery allowance requires review')
    return result


def main():
    if os.geteuid() != 0:
        raise RuntimeError('root required')
    for unit in ['hindsight-history-import', 'hindsight-worker', 'hindsight-api']:
        status = subprocess.check_output(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'], text=True).strip()
        if status not in ['inactive', 'failed']:
            raise RuntimeError('stop services before installing timeout guard')
    if (HOLD.exists() or (DATA / 'checkpoints/import-timeout.hold').exists()
            or (DATA / 'control/forget-pending').exists()):
        raise RuntimeError('active hold blocks installation')
    if importlib.metadata.version('hindsight-api-slim') != '1.0.0':
        raise RuntimeError('unsupported native version')
    source = Path(__file__).parent
    for name in ['ann_timeout_stop.py', 'timeout_gate.py', 'usage_worker.py']:
        for destination in [OPT / name, OPT / 'maintenance' / name]:
            atomic(destination, (source / name).read_text(), mode=0o644)
    # Remove the rejected batching adapter; it must not be imported or activated.
    for destination in [OPT / 'bounded_ann.py', OPT / 'maintenance/bounded_ann.py']:
        destination.unlink(missing_ok=True)
    atomic(OPT / 'maintenance/resume_scheduled_import.py',
           (source / 'resume_scheduled_import.py').read_text(), mode=0o644)
    config = json.loads((DATA / 'runtime.json').read_text())
    config['HINDSIGHT_API_DB_COMMAND_TIMEOUT'] = '120'
    config['HINDSIGHT_API_DB_STATEMENT_TIMEOUT'] = '120'
    config['HINDSIGHT_API_WORKER_MAX_RETRIES'] = str(asyncio.run(recovery_limit(config)))
    account = pwd.getpwnam('hindsight')
    atomic(DATA / 'runtime.json', json.dumps(config), account.pw_uid, account.pw_gid)
    units = Path('/etc/systemd/system')
    for unit in ['hindsight-worker', 'hindsight-history-import']:
        atomic(units / (unit + '.service.d/timeout-stop.conf'),
               '[Unit]\nConditionPathExists=!' + str(HOLD) + '\n', mode=0o644)
    atomic(units / 'hindsight-timeout-stop.service', '''[Unit]
Description=Stop the Hindsight import on a persistent timeout hold
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/hindsight/timeout_gate.py
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
TimeoutStartSec=90
StandardOutput=null
StandardError=null
''', mode=0o644)
    atomic(units / 'hindsight-timeout-stop.path', '''[Unit]
Description=Watch the fixed Hindsight import timeout hold
[Path]
PathChanged=/var/lib/hindsight/import-timeout.hold
Unit=hindsight-timeout-stop.service
[Install]
WantedBy=multi-user.target
''', mode=0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'hindsight-timeout-stop.path'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    validation = '''import os,json,sys
from pathlib import Path
os.environ.update(json.loads(Path('/var/lib/hindsight/runtime.json').read_text()))
sys.path.insert(0,'/opt/hindsight')
from ann_timeout_stop import install
install('felix-pi')
'''
    subprocess.run(['sudo', '-u', 'hindsight', '/opt/hindsight/venv/bin/python', '-c', validation],
                   cwd=DATA, check=True)
    print(json.dumps({'full_native_query': True, 'ann_limit_seconds': 120,
                      'db_command_timeout': 120, 'db_statement_timeout': 120,
                      'timeout_stop_enabled': True, 'batching_removed': True,
                      'recovery_allowance': config['HINDSIGHT_API_WORKER_MAX_RETRIES']}))


if __name__ == '__main__':
    main()
