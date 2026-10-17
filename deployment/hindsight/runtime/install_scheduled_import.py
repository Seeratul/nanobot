"""Pause only the Hindsight pipeline and install the authorized one-shot timer."""
import os
import shutil
import subprocess
from pathlib import Path

STATE = Path('/var/lib/hindsight/scheduled-import.hold')
NAMES = ('hindsight-history-import', 'hindsight-worker', 'hindsight-api', 'hindsight-embedding')
# The hold also blocks enabled units and the worker gate across a reboot before 21:00.
fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, 'w') as stream:
    stream.write('Authorized pause until 2026-10-17 21:00 Europe/Berlin\n')
    stream.flush()
    os.fsync(stream.fileno())
directory = os.open(STATE.parent, os.O_RDONLY | os.O_DIRECTORY)
os.fsync(directory)
os.close(directory)
for name in NAMES:
    dropin = Path('/etc/systemd/system') / (name + '.service.d')
    dropin.mkdir(mode=0o755, exist_ok=True)
    (dropin / 'scheduled-hold.conf').write_text('[Unit]\nConditionPathExists=!/var/lib/hindsight/scheduled-import.hold\n')
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'stop', *NAMES], check=True)
directory = os.open(STATE.parent, os.O_RDONLY | os.O_DIRECTORY)
os.fsync(directory)
os.close(directory)
for name in NAMES:
    pid = subprocess.check_output(['systemctl', 'show', name, '-p', 'MainPID', '--value'], text=True).strip()
    assert pid == '0'
source = Path('/var/tmp')
for name in ('hindsight-scheduled-import.service', 'hindsight-scheduled-import.timer'):
    target = Path('/etc/systemd/system') / name
    shutil.copyfile(source / name, target)
    target.chmod(0o644)
target = Path('/opt/hindsight/maintenance/resume_scheduled_import.py')
shutil.copyfile(source / target.name, target)
target.chmod(0o644)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemd-analyze', 'verify', '/etc/systemd/system/hindsight-scheduled-import.service',
                '/etc/systemd/system/hindsight-scheduled-import.timer'], check=True)
subprocess.run(['systemctl', 'enable', '--now', 'hindsight-scheduled-import.timer'], check=True)
print('scheduled_import_installed=true; pipeline_stopped=true; boot_hold_active=true')
