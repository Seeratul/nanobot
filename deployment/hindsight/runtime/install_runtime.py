"""Install the already evaluated native stack; never modify Nanobot or its data."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import time

OPT = Path('/opt/hindsight')
DATA = Path('/var/lib/hindsight')
MODEL = 'Qwen3-Embedding-0.6B-Q8_0.gguf'
EXPECTED = '06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439'
PG = Path('/usr/lib/postgresql/17/bin')
account = pwd.getpwnam('hindsight')


def run(*args: str, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def write(path: Path, content: str, mode=0o644, owner=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w') as handle:
        os.chmod(path, mode)
        handle.write(content)
    if owner:
        os.chown(path, account.pw_uid, account.pw_gid)


def main():
    if os.geteuid() != 0:
        raise SystemExit('root required')
    if (OPT / 'installed.json').exists():
        raise SystemExit('already installed; inspect before performing an upgrade')
    for port in (8888, 8891, 8892, 8893, 55433):
        import socket
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                raise SystemExit(f'port {port} already occupied')
    source = Path('/var/tmp/nanobot-qwen3-models') / MODEL
    if hashlib.file_digest(source.open('rb'), 'sha256').hexdigest() != EXPECTED:
        raise SystemExit('model hash mismatch')
    OPT.mkdir(mode=0o755, exist_ok=True)
    shutil.copytree('/var/tmp/hindsight-pi-test-venv', OPT / 'venv', symlinks=True)
    for file in (OPT / 'venv/bin').iterdir():
        if file.is_file() and not file.is_symlink():
            content = file.read_bytes()
            if content.startswith(b'#!'):
                file.write_bytes(content.replace(b'/var/tmp/hindsight-pi-test-venv', b'/opt/hindsight/venv'))
    shutil.copytree('/var/tmp/nanobot-qwen3-eval/build/bin', OPT / 'llama', symlinks=True)
    (OPT / 'models').mkdir()
    shutil.copyfile(source, OPT / 'models' / MODEL)
    for directory, dirs, files in os.walk(OPT):
        os.chown(directory, 0, 0)
        os.chmod(directory, 0o755)
        for name in files:
            file = Path(directory) / name
            if not file.is_symlink():
                mode = file.stat().st_mode
                os.chown(file, 0, 0)
                os.chmod(file, 0o755 if mode & 0o111 else 0o644)
    password = secrets.token_urlsafe(32)
    passfile = DATA / 'postgres-password'
    write(passfile, password, 0o600, owner=True)
    pgdata = DATA / 'pgdata'
    run('sudo', '-u', 'hindsight', str(PG / 'initdb'), '-D', str(pgdata), '-U', 'hindsight',
        '--encoding=UTF8', '--locale=C.UTF-8', '--auth-local=peer',
        '--auth-host=scram-sha-256', '--pwfile', str(passfile), stdout=subprocess.DEVNULL)
    with (pgdata / 'postgresql.conf').open('a') as handle:
        handle.write("\nlisten_addresses='127.0.0.1'\nport=55433\n"
                     "unix_socket_directories='/run/hindsight-postgres'\n"
                     "shared_buffers=128MB\nmax_connections=24\nwork_mem=4MB\n")
    pause = DATA / 'control/forget-pending'
    pause.parent.mkdir(mode=0o755, exist_ok=True)
    nanobot = pwd.getpwnam('nanobot')
    os.chown(pause.parent, nanobot.pw_uid, nanobot.pw_gid)
    # Let nanobot reach only the control directory; all private subdirectories
    # retain their original 0700 permissions.
    os.chmod(DATA, 0o711)
    env = {
        'HOME': str(DATA), 'CODEX_HOME': str(DATA / 'codex'),
        'HINDSIGHT_API_DATABASE_URL': f'postgresql://hindsight:{password}@127.0.0.1:55433/hindsight',
        'HINDSIGHT_API_LLM_PROVIDER': 'openai-codex',
        'HINDSIGHT_API_LLM_MODEL': 'gpt-6-luna',
        'HINDSIGHT_API_LLM_REASONING_EFFORT': 'low',
        'HINDSIGHT_API_LLM_STRICT_SCHEMA': 'true',
        'HINDSIGHT_API_LLM_OUTPUT_LANGUAGE': 'English',
        'HINDSIGHT_API_LLM_MAX_RETRIES': '1',
        'HINDSIGHT_API_LLM_TIMEOUT': '180',
        'HINDSIGHT_API_LLM_MAX_CONCURRENT': '1',
        'HINDSIGHT_API_EMBEDDINGS_PROVIDER': 'openai',
        'HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY': 'local-not-a-real-key',
        'HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL': 'http://127.0.0.1:8891/v1',
        'HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL': 'qwen3-embedding-0.6b-q8_0',
        'HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS': '1024',
        'HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE': '8',
        'HINDSIGHT_API_EMBEDDINGS_MAX_CONCURRENT_REQUESTS': '1',
        'HINDSIGHT_API_EMBEDDINGS_QUERY_PREFIX': 'Instruct: Given a question about past conversations, retrieve the relevant memory that answers it.\nQuery: ',
        'HINDSIGHT_API_EMBEDDINGS_PASSAGE_PREFIX': '',
        'HINDSIGHT_API_RERANKER_PROVIDER': 'rrf',
        'HINDSIGHT_API_RECALL_INCLUDE_CHUNKS': 'false',
        'HINDSIGHT_API_DB_POOL_MIN_SIZE': '1',
        'HINDSIGHT_API_DB_POOL_MAX_SIZE': '6',
        'HINDSIGHT_API_WORKER_MAX_SLOTS': '2',
        'HINDSIGHT_API_RETAIN_WALL_TIMEOUT': '86400',
        'HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS': '0',
        'HINDSIGHT_API_WORKER_MAX_RETRIES': '1',
        'HINDSIGHT_API_WORKER_TASK_RETRY_BACKOFF_SECONDS': '300',
        'HINDSIGHT_API_WORKER_ENABLED': 'false',
        'HINDSIGHT_API_ENABLE_OBSERVATIONS': 'true',
        'HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION': 'true',
        'HINDSIGHT_API_SKIP_LLM_VERIFICATION': 'true',
        'HINDSIGHT_API_MCP_ENABLED': 'true',
        'HINDSIGHT_API_MCP_ENABLED_TOOLS': 'recall,get_mental_model',
        'HINDSIGHT_API_LLM_TRACE_ENABLED': 'false',
        'HINDSIGHT_API_ENABLE_BANK_CONFIG_API': 'false',
        'DO_NOT_TRACK': '1', 'HF_HUB_OFFLINE': '1',
    }
    write(DATA / 'runtime.json', json.dumps(env), 0o600, owner=True)
    for name in ['worker_supervisor.py', 'usage_worker.py', 'usage_ledger.py']:
        write(OPT / name, Path(__file__).with_name(name).read_text())
    write(OPT / 'launch.py', '''import json, os, sys
from pathlib import Path
env = {**os.environ, **json.loads(Path('/var/lib/hindsight/runtime.json').read_text())}
mode = sys.argv[1]
commands = {
 'api': ['/opt/hindsight/venv/bin/hindsight-api', '--host', '127.0.0.1', '--port', '8888', '--workers', '1', '--no-access-log'],
 'worker': ['/opt/hindsight/venv/bin/python', '/opt/hindsight/worker_supervisor.py'],
}
command = commands[mode]
os.execvpe(command[0], command, env)
''')
    common = '''User=hindsight
Group=hindsight
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/hindsight
Restart=on-failure
RestartSec=30
TimeoutStopSec=15
KillMode=control-group
# Do not persist provider errors or request contents in the system journal.
StandardOutput=null
StandardError=null
'''
    units = {
        'postgres': (f'ExecStart={PG}/postgres -D {pgdata}\nRuntimeDirectory=hindsight-postgres\nReadWritePaths=/run/hindsight-postgres\nMemoryMax=384M\n', ''),
        'embedding': (f'ExecStart={OPT}/llama/llama-server --model {OPT}/models/{MODEL} --alias qwen3-embedding-0.6b-q8_0 --host 127.0.0.1 --port 8891 --embedding --pooling last --threads 6 --threads-batch 6 --ctx-size 8192 --kv-unified --kv-unified-per-slot 4096 --batch-size 1024 --ubatch-size 1024 --parallel 8\nEnvironment=LD_LIBRARY_PATH=/opt/hindsight/llama\nNice=10\nMemoryMax=2300M\n', ''),
        'api': (f'ExecStart={OPT}/venv/bin/python {OPT}/launch.py api\nMemoryMax=640M\n', 'After=hindsight-postgres.service hindsight-embedding.service\nRequires=hindsight-postgres.service hindsight-embedding.service\n'),
        'worker': (f'ExecStart={OPT}/venv/bin/python {OPT}/launch.py worker\nMemoryMax=1400M\nTimeoutStopSec=50\nNice=10\n', f'After=hindsight-api.service\nRequires=hindsight-api.service\nConditionPathExists=!{pause}\n'),
    }
    for name, (extra, dependencies) in units.items():
        write(Path('/etc/systemd/system') / f'hindsight-{name}.service',
              f'[Unit]\nDescription=Nanobot Hindsight {name}\nAfter=network-online.target\n{dependencies}\n'
              f'[Service]\n{common}{extra}\n[Install]\nWantedBy=multi-user.target\n')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'start', 'hindsight-postgres.service')
    deadline = time.monotonic() + 60
    while subprocess.run([str(PG / 'pg_isready'), '-h', '/run/hindsight-postgres',
                          '-p', '55433'], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL).returncode:
        if time.monotonic() >= deadline:
            raise RuntimeError('PostgreSQL did not become ready')
        time.sleep(1)
    run('sudo', '-u', 'hindsight', str(PG / 'createdb'), '-h', '/run/hindsight-postgres',
        '-p', '55433', 'hindsight')
    run('systemctl', 'enable', '--now', 'hindsight-postgres.service', 'hindsight-embedding.service',
        'hindsight-api.service', 'hindsight-worker.service', stdout=subprocess.DEVNULL)
    write(OPT / 'installed.json', json.dumps({'hindsight': '1.0.0', 'model_sha256': EXPECTED,
                                             'api_worker_enabled': False}))
    print(json.dumps({'installed': True, 'nanobot_modified': False}))


if __name__ == '__main__':
    main()
