"""Apply tested retry/readiness/diagnostic changes while memory workers are idle."""
import hashlib
import json
import os
import pathlib
import pwd
import subprocess
import tempfile

STAGE = pathlib.Path('/var/tmp/nanobot-failure-safeguards-20261020')
PACKAGE = pathlib.Path('/home/nanobot/.local/share/nanobot/venv/lib/python3.13/site-packages/nanobot')


def atomic(path, text, uid=0, gid=0, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            os.fchown(out.fileno(), uid, gid)
            os.fchmod(out.fileno(), mode)
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        pathlib.Path(name).unlink(missing_ok=True)


async def install():
    import asyncpg
    assert not pathlib.Path('/var/lib/hindsight/import-timeout.hold').exists()
    assert not pathlib.Path('/var/lib/hindsight/control/forget-pending').exists()
    runtime_path = pathlib.Path('/var/lib/hindsight/runtime.json')
    runtime = json.loads(runtime_path.read_text())
    db = await asyncpg.connect(runtime['HINDSIGHT_API_DATABASE_URL'], timeout=10)
    try:
        assert await db.fetchval("select count(*) from async_operations where status in ('pending','processing')") == 0
    finally:
        await db.close()
    expected = json.loads((STAGE / 'expected-base.json').read_text())
    for name, digest in expected.items():
        path = pathlib.Path(name)
        assert hashlib.sha256(path.read_text().encode()).hexdigest() == digest, 'installed source drift'
    ctl = ['sudo', '-u', 'nanobot', 'env', 'XDG_RUNTIME_DIR=/run/user/1001', 'systemctl', '--user']
    subprocess.run(ctl + ['stop', 'nanobot-discord'], check=True)
    subprocess.run(['systemctl', 'stop', 'nanobot-memory-sync', 'hindsight-worker'], check=True)
    for name in ('outbox.py', 'worker.py', 'service.py'):
        atomic(PACKAGE / 'memory_sync' / name, (STAGE / 'nanobot/memory_sync' / name).read_text())
    for name in ('retry_limits.py', 'usage_worker.py'):
        atomic(pathlib.Path('/opt/hindsight') / name, (STAGE / name).read_text())
    runtime['HINDSIGHT_CONSOLIDATION_MAX_RETRIES'] = '4'
    account = pwd.getpwnam('hindsight')
    atomic(runtime_path, json.dumps(runtime), account.pw_uid, account.pw_gid, 0o600)
    atomic(pathlib.Path('/etc/default/nanobot-memory-sync'),
           '# Limits for new delivery attempts; running work is not cancelled.\n'
           'MEMORY_MAX_DELIVERY_ATTEMPTS=5\nMEMORY_RETRY_WINDOW_SECONDS=21600\n', mode=0o600)
    atomic(pathlib.Path('/etc/systemd/system/nanobot-memory-sync.service.d/30-retry-budget.conf'),
           '[Service]\nEnvironmentFile=/etc/default/nanobot-memory-sync\n')
    root = pathlib.Path('/opt/pi-diagnostics')
    root.mkdir(mode=0o755, exist_ok=True)
    for name in ('pi_outage_diagnostics.py', 'wait_for_hindsight.py'):
        atomic(root / name, (STAGE / name).read_text())
    logs = pathlib.Path('/var/log/pi-diagnostics')
    logs.mkdir(mode=0o700, exist_ok=True)
    logs.chmod(0o700)
    atomic(pathlib.Path('/etc/systemd/system/pi-outage-diagnostics.service'), '''[Unit]
Description=Bounded private Pi kernel and outage diagnostics
After=systemd-journald.service local-fs.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/pi-diagnostics/pi_outage_diagnostics.py
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/log/pi-diagnostics
UMask=0077
Nice=19
MemoryMax=96M
TimeoutStartSec=35
StandardOutput=null
StandardError=null
''')
    atomic(pathlib.Path('/etc/systemd/system/pi-outage-diagnostics.timer'), '''[Unit]
Description=Sample Pi outage evidence every 30 seconds
[Timer]
OnBootSec=20
OnUnitInactiveSec=30
AccuracySec=1
Unit=pi-outage-diagnostics.service
[Install]
WantedBy=timers.target
''')
    account = pwd.getpwnam('nanobot')
    atomic(pathlib.Path('/home/nanobot/.config/systemd/user/nanobot-discord.service.d/30-hindsight-ready.conf'),
           '[Service]\nExecStartPre=/home/nanobot/.local/share/nanobot/venv/bin/python '
           '/opt/pi-diagnostics/wait_for_hindsight.py\nTimeoutStartSec=200\n',
           account.pw_uid, account.pw_gid)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(ctl + ['daemon-reload'], check=True)
    subprocess.run(['systemctl', 'start', 'hindsight-worker', 'nanobot-memory-sync'], check=True)
    subprocess.run(ctl + ['start', 'nanobot-discord'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'pi-outage-diagnostics.timer'], check=True, capture_output=True)
    subprocess.run(['systemctl', 'start', 'pi-outage-diagnostics.service'], check=True)
    print(json.dumps({'installed': True, 'delivery_attempt_limit': 5,
                      'retry_window_seconds': 21600, 'consolidation_retry_limit': 4,
                      'diagnostic_max_bytes': 8 * 1024 * 1024}))


if __name__ == '__main__':
    import asyncio
    try:
        asyncio.run(install())
    except Exception as exc:
        print(json.dumps({'status': 'needs_attention', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
