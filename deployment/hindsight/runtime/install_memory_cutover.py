"""Install only the reviewed Nanobot memory integration; no secret output."""
import hashlib
import json
import os
import pathlib
import pwd
import subprocess
import tempfile
import urllib.request

STAGE = pathlib.Path('/var/tmp/nanobot-cutover-20261020/nanobot')
PACKAGE = pathlib.Path('/home/nanobot/.local/share/nanobot/venv/lib/python3.13/site-packages/nanobot')
HOME = pathlib.Path('/home/nanobot')
WS = HOME / '.nanobot/workspace'
ROOT = HOME / '.nanobot/sessions/8679b657a17401f5f41c32a247d800ee'
HOLD = pathlib.Path('/var/lib/hindsight/import-timeout.hold')
EXISTING = ['session/recovery.py', 'session/manager.py', 'cli/gateway_runtime.py',
            'config/schema.py', 'command/builtin.py', 'agent/memory.py', 'agent/context.py',
            'agent/runner.py', 'agent/autocompact.py', 'agent/loop.py']


def atomic(path, text, *, uid=0, gid=0, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            os.fchmod(out.fileno(), mode)
            os.fchown(out.fileno(), uid, gid)
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        pathlib.Path(name).unlink(missing_ok=True)


def main():
    assert os.geteuid() == 0
    assert not HOLD.exists()
    assert not pathlib.Path('/var/lib/hindsight/control/forget-pending').exists()
    control = ['sudo', '-u', 'nanobot', 'env', 'XDG_RUNTIME_DIR=/run/user/1001',
               'systemctl', '--user']
    assert subprocess.run(control + ['is-active', '--quiet', 'nanobot-discord']).returncode != 0
    review = json.loads((ROOT / 'hindsight-cutover-profile-review.json').read_text())
    assert not any(review['checks'].values()), 'profile preservation review requires resolution'
    for mid, name in [('agent-soul', 'SOUL.md'), ('user-profile', 'USER.md')]:
        assert hashlib.sha256((WS / name).read_bytes()).hexdigest() == review[mid]['local_hash']
        with urllib.request.urlopen('http://127.0.0.1:8888/v1/default/banks/personal/mental-models/' + mid,
                                    timeout=30) as response:
            model = json.load(response)
        assert not model.get('last_refresh_failed_at')
        assert hashlib.sha256(model['content'].encode()).hexdigest() == review[mid]['model_hash']
    manifest = ROOT / 'hindsight-cutover-deployment.json'
    assert not manifest.exists(), 'cutover already installed; inspect receipt before retry'
    config_path = HOME / '.nanobot/config.json'
    cfg = json.loads(config_path.read_text())
    defaults = cfg['agents']['defaults']
    assert not defaults.get('hindsight', {}).get('enabled', False)
    receipt = {'status': 'installing', 'files': {},
               'previous_dream_enabled': defaults.get('dream', {}).get('enabled', True),
               'previous_hindsight': defaults.get('hindsight'),
               'previous_ssrf_whitelist': cfg.get('tools', {}).get('ssrfWhitelist', []),
               'previous_hindsight_mcp': cfg.get('tools', {}).get('mcpServers', {}).get('hindsight')}
    account = pwd.getpwnam('nanobot')
    atomic(manifest, json.dumps(receipt), uid=account.pw_uid, gid=account.pw_gid, mode=0o600)
    files = EXISTING + [str(p.relative_to(STAGE)) for p in (STAGE / 'memory_sync').glob('*.py')]
    for relative in files:
        source = STAGE / relative
        target = PACKAGE / relative
        text = source.read_text()
        atomic(target, text)
        receipt['files'][relative] = hashlib.sha256(text.encode()).hexdigest()
    defaults.setdefault('dream', {})['enabled'] = False
    defaults['hindsight'] = {'enabled': True, 'bankId': 'personal',
                              'apiUrl': 'http://127.0.0.1:8888',
                              'pauseFile': '/var/lib/hindsight/control/forget-pending'}
    tools = cfg.setdefault('tools', {})
    whitelist = tools.setdefault('ssrfWhitelist', [])
    if '127.0.0.1/32' not in whitelist:
        whitelist.append('127.0.0.1/32')
    assert 'hindsight' not in tools.setdefault('mcpServers', {})
    tools['mcpServers']['hindsight'] = {
        'type': 'streamableHttp', 'url': 'http://127.0.0.1:8888/mcp/personal/',
        'enabledTools': ['recall', 'get_mental_model'], 'toolTimeout': 120,
    }
    atomic(config_path, json.dumps(cfg, ensure_ascii=False, indent=2) + '\n',
           uid=account.pw_uid, gid=account.pw_gid, mode=0o600)
    state_path = ROOT / 'hindsight-profiles.json'
    state = json.loads(state_path.read_text())
    for mid in ('agent-soul', 'user-profile'):
        state[mid] = {'content_hash': review[mid]['local_hash'], 'revision': None}
    # Explicit reconciliation is allowed only after review; local files stay as-is
    # until the validated exporter projects the matching reviewed model revision.
    atomic(state_path, json.dumps(state), uid=account.pw_uid, gid=account.pw_gid, mode=0o600)
    unit = '''[Unit]
Description=Nanobot Hindsight delivery, selective forgetting and saved profiles
After=network-online.target hindsight-api.service
Wants=hindsight-api.service
ConditionPathExists=!/var/lib/hindsight/import-timeout.hold
[Service]
Type=exec
User=nanobot
Group=nanobot
WorkingDirectory=/home/nanobot/.nanobot/workspace
Environment=HOME=/home/nanobot
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/home/nanobot/.local/share/nanobot/venv/bin/python -m nanobot.memory_sync.service --outbox ''' + str(ROOT / 'hindsight-outbox.sqlite3') + ''' --api-url http://127.0.0.1:8888 --bank-id personal --workspace /home/nanobot/.nanobot/workspace --profile-state ''' + str(state_path) + ''' --pause-file /var/lib/hindsight/control/forget-pending --hold-file /var/lib/hindsight/import-timeout.hold
NoNewPrivileges=true
UMask=0077
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=null
[Install]
WantedBy=multi-user.target
'''
    atomic(pathlib.Path('/etc/systemd/system/nanobot-memory-sync.service'), unit)
    gate = pathlib.Path('/opt/hindsight/timeout_gate.py')
    old_gate = gate.read_text()
    expected = "['hindsight-history-import.service', 'hindsight-worker.service']"
    assert expected in old_gate
    atomic(gate, old_gate.replace(expected,
           "['nanobot-memory-sync.service', 'hindsight-history-import.service', 'hindsight-worker.service']"))
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    receipt['status'] = 'installed_not_started'
    atomic(manifest, json.dumps(receipt), uid=account.pw_uid, gid=account.pw_gid, mode=0o600)
    print(json.dumps({'status': receipt['status'], 'installed_files': len(files)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'needs_attention', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
