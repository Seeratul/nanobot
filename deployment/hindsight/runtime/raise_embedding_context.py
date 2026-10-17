import subprocess
from pathlib import Path

path = Path('/etc/systemd/system/hindsight-embedding.service')
text = path.read_text()
assert '--ctx-size 2048' in text
path.write_text(text.replace('--ctx-size 2048', '--ctx-size 4096'))
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'restart', 'hindsight-embedding'], check=True)
print('embedding_context_updated=4096')
