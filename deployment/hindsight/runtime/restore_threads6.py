"""Restore the requested six threads; never start the paused importer/worker."""
import re
import subprocess
from pathlib import Path

path = Path('/etc/systemd/system/hindsight-embedding.service')
original = path.read_text()
updated = re.sub(r'--threads(?:-batch)? \d+', lambda match:
                 match.group(0).rsplit(' ', 1)[0] + ' 6', original)
if original != updated:
    path.write_text(updated)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'restart', 'hindsight-embedding'], check=True)
subprocess.run(['systemctl', 'start', 'hindsight-api'], check=True)
print('{"stage":"restored","threads":6,"import_resumed":false}', flush=True)
