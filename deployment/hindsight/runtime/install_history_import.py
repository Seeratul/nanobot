"""Install only the dedicated importer; leave the live Nanobot environment alone."""
import shutil
import subprocess
from pathlib import Path

root = Path('/opt/hindsight/nanobot-import')
root.mkdir(mode=0o755, exist_ok=True)
source = Path('/tmp/nanobot-hindsight-dev/nanobot')
assert source.is_dir()
shutil.copytree(source, root / 'nanobot', dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
maintenance = Path('/opt/hindsight/maintenance')
maintenance.mkdir(mode=0o755, exist_ok=True)
for name in ('import_history.py', 'prepare_history_import.py', 'probe_history_embeddings.py',
             'history_import_progress.py'):
    shutil.copyfile(Path('/var/tmp') / name, maintenance / name)
    (maintenance / name).chmod(0o644)
for path in root.rglob('*'):
    path.chmod(0o755 if path.is_dir() else 0o644)
unit = Path('/etc/systemd/system/hindsight-history-import.service')
shutil.copyfile('/var/tmp/hindsight-history-import.service', unit)
unit.chmod(0o644)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
print('dedicated_importer_installed=true; production_nanobot_unchanged=true')
