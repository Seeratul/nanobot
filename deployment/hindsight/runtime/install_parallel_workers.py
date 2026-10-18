"""Apply the authorized two-process setup without changing provider settings."""
import json
import os
import pwd
import subprocess
from pathlib import Path

OPT = Path('/opt/hindsight')
RUNTIME = Path('/var/lib/hindsight/runtime.json')
UNIT = Path('/etc/systemd/system/hindsight-worker.service')
OLD = "'worker': ['/opt/hindsight/venv/bin/hindsight-worker', '--worker-id', 'felix-pi', '--http-host', '127.0.0.1', '--http-port', '8892'],"
NEW = "'worker': ['/opt/hindsight/venv/bin/python', '/opt/hindsight/worker_supervisor.py'],"


def atomic(path, text, mode, uid=0, gid=0):
    temporary = path.with_suffix(path.suffix + '.parallel.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, mode)
    os.fchmod(fd, mode)
    os.fchown(fd, uid, gid)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(fd)
    os.close(fd)


def main():
    if os.geteuid() != 0:
        raise SystemExit('root required')
    for name in ['hindsight-worker', 'hindsight-history-import', 'hindsight-worker-parallel-test']:
        if subprocess.run(['systemctl', 'is-active', '--quiet', name], check=False).returncode == 0:
            raise SystemExit('stop import/test before configuring workers')
    if Path('/var/lib/hindsight/control/forget-pending').exists():
        raise SystemExit('pending forget blocks resume')
    launcher = (OPT / 'launch.py').read_text()
    if OLD not in launcher and NEW not in launcher:
        raise SystemExit('unexpected launcher')
    unit = UNIT.read_text()
    if 'KillMode=control-group' not in unit or 'NoNewPrivileges=true' not in unit:
        raise SystemExit('worker unit security boundary changed')
    account = pwd.getpwnam('hindsight')
    runtime = json.loads(RUNTIME.read_text())
    runtime['HINDSIGHT_API_WORKER_MAX_SLOTS'] = '2'
    for name in ['worker_supervisor.py', 'usage_worker.py', 'usage_ledger.py', 'retain_checkpoints.py']:
        atomic(OPT / name, Path(__file__).with_name(name).read_text(), 0o644)
    atomic(OPT / 'launch.py', launcher.replace(OLD, NEW), 0o644)
    atomic(RUNTIME, json.dumps(runtime), 0o600, account.pw_uid, account.pw_gid)
    atomic(UNIT, unit.replace('MemoryMax=900M', 'MemoryMax=1400M')
           .replace('TimeoutStopSec=15', 'TimeoutStopSec=50'), 0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print(json.dumps({'configured_workers': 2, 'slots_per_worker': 2, 'started': False}))


if __name__ == '__main__':
    main()
