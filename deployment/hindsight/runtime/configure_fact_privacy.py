"""Keep raw archived chunks out of automatic reflection/profile generation."""
import json
from pathlib import Path
import subprocess

path = Path('/var/lib/hindsight/runtime.json')
env = json.loads(path.read_text())
env['HINDSIGHT_API_RECALL_INCLUDE_CHUNKS'] = 'false'
path.write_text(json.dumps(env))
path.chmod(0o600)
subprocess.run(['systemctl', 'restart', 'hindsight-api', 'hindsight-worker'], check=True)
print('internal raw-chunk recall disabled; Nanobot unchanged')
