"""Install numeric usage tracking without restarting the active workers."""
import json
import os
import pwd
import subprocess
from pathlib import Path

from install_parallel_workers import atomic

OPT = Path('/opt/hindsight')
SOURCE = Path(__file__).parent


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit('root required')
    unit = Path('/etc/systemd/system/hindsight-worker.service').read_text()
    if 'KillMode=control-group' not in unit or 'NoNewPrivileges=true' not in unit:
        raise SystemExit('unexpected worker security boundary')
    before = run('systemctl', 'show', 'hindsight-worker', '--property=MainPID', '--value')
    account = pwd.getpwnam('hindsight')
    directory = Path('/var/lib/hindsight/usage')
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chown(directory, account.pw_uid, account.pw_gid)
    os.chmod(directory, 0o700)
    for name in ['usage_ledger.py', 'usage_worker.py', 'worker_supervisor.py']:
        atomic(OPT / name, (SOURCE / name).read_text(), 0o644)
        atomic(OPT / 'maintenance' / name, (SOURCE / name).read_text(), 0o644)
    service = '''[Unit]
Description=Persist content-free Hindsight usage counters
After=hindsight-worker.service
[Service]
Type=oneshot
User=hindsight
Group=hindsight
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/hindsight/usage
ExecStart=/opt/hindsight/venv/bin/python /opt/hindsight/usage_ledger.py collect
TimeoutStartSec=25
StandardOutput=journal
StandardError=null
'''
    timer = '''[Unit]
Description=Collect Hindsight usage counters every 30 seconds
[Timer]
OnBootSec=20
OnUnitActiveSec=30
AccuracySec=1
Unit=hindsight-usage-collect.service
[Install]
WantedBy=timers.target
'''
    atomic(Path('/etc/systemd/system/hindsight-usage-collect.service'), service, 0o644)
    atomic(Path('/etc/systemd/system/hindsight-usage-collect.timer'), timer, 0o644)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'hindsight-usage-collect.timer')
    run('systemctl', 'start', 'hindsight-usage-collect.service')
    after = run('systemctl', 'show', 'hindsight-worker', '--property=MainPID', '--value')
    if before != after:
        raise RuntimeError('worker changed during installation')
    print(json.dumps({'installed': True, 'worker_restarted': False,
                      'token_hook_activation': 'next worker start'}))


if __name__ == '__main__':
    main()
