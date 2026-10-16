"""Install pinned Debian PostgreSQL packages without creating a default cluster."""
import os
import pathlib
import re
import subprocess

if os.geteuid() != 0:
    raise SystemExit('Run with sudo')
environment = {**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'}
subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends',
                'postgresql-common'], env=environment, check=True)
config = pathlib.Path('/etc/postgresql-common/createcluster.conf')
original = config.read_bytes()
text = original.decode()
text = re.sub(r'^\s*create_main_cluster\s*=.*$', '', text, flags=re.MULTILINE)
config.write_text(text + '\ncreate_main_cluster = false\n')
try:
    subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends',
                    'postgresql-17=17.11-0+deb13u1', 'postgresql-17-pgvector=0.8.0-1'],
                   env=environment, check=True)
finally:
    config.write_bytes(original)
