"""Prepare a dedicated service identity and Codex auth directory, without logging in."""
import os
import pathlib
import pwd
import subprocess

if os.geteuid() != 0:
    raise SystemExit('Run with sudo')
try:
    account = pwd.getpwnam('hindsight')
except KeyError:
    subprocess.run(['useradd', '--system', '--user-group', '--create-home',
        '--home-dir', '/var/lib/hindsight', '--shell', '/usr/sbin/nologin', 'hindsight'], check=True)
    account = pwd.getpwnam('hindsight')
if account.pw_dir != '/var/lib/hindsight':
    raise RuntimeError('Existing hindsight user has an unexpected home')
auth = pathlib.Path('/var/lib/hindsight/codex')
auth.mkdir(mode=0o700, parents=True, exist_ok=True)
auth.chmod(0o700)
os.chown(auth, account.pw_uid, account.pw_gid)
config = auth / 'config.toml'
if not config.exists():
    config.write_text('cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n')
    config.chmod(0o600)
    os.chown(config, account.pw_uid, account.pw_gid)
print('Dedicated hindsight service user and private Codex directory ready')
print('Authentication file present:', (auth / 'auth.json').is_file())
