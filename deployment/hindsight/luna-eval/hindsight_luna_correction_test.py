"""Isolated real Hindsight/PostgreSQL/Qwen test using fictional records only."""
import importlib.util
import json
import os
import pathlib
import secrets
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path('/var/lib/hindsight/luna-test')
VENV = pathlib.Path('/var/tmp/hindsight-pi-test-venv')
PG = pathlib.Path('/usr/lib/postgresql/17/bin')
URL = 'http://127.0.0.1:8889'
ROOT.mkdir(mode=0o700, exist_ok=True)
os.chmod(ROOT, 0o700)
spec = importlib.util.spec_from_file_location('qwen_eval', '/tmp/nanobot-qwen3-benchmark.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
processes = []
handles = []
samples = []
stop = threading.Event()
pg_started = False


def event(stage, **data):
    print(json.dumps({'stage': stage, **data}), flush=True)


def call(path, payload=None, method=None):
    req = urllib.request.Request(URL + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'}, method=method)
    with urllib.request.urlopen(req, timeout=240) as response:
        return json.load(response)


def start(label, command, env=None):
    log = (ROOT / (label + '.log')).open('w')
    handles.append(log)
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=ROOT)
    processes.append((label, process))
    return process


def ready(process, address, label):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(label + ' exited; inspect its isolated log')
        try:
            with urllib.request.urlopen(address, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(2)
    raise TimeoutError(label + ' readiness timeout')


def monitor():
    while not stop.wait(0.5):
        point = fixture.meminfo()
        for label, process in processes.copy():
            point[label + '_rss_kib'] = fixture.status(process.pid).get('VmRSS', 0)
        point['temperature_c'] = int(pathlib.Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000
        samples.append(point)


try:
    password_file = ROOT / 'postgres-password'
    if not password_file.exists():
        password_file.write_text(secrets.token_urlsafe(32))
        password_file.chmod(0o600)
    password = password_file.read_text().strip()
    data = ROOT / 'pgdata'
    socket = ROOT / 'socket'
    socket.mkdir(exist_ok=True)
    if not (data / 'PG_VERSION').exists():
        subprocess.run([str(PG / 'initdb'), '-D', str(data), '-U', 'hindsight_test',
            '--encoding=UTF8', '--locale=C.UTF-8', '--auth-local=trust',
            '--auth-host=scram-sha-256', '--pwfile', str(password_file)], check=True,
            stdout=subprocess.DEVNULL)
    subprocess.run([str(PG / 'pg_ctl'), '-D', str(data), '-l', str(ROOT / 'postgres.log'),
        '-o', f'-h 127.0.0.1 -p 55432 -k {socket} -c shared_buffers=128MB -c max_connections=20 -c work_mem=4MB',
        '-w', 'start'], check=True)
    pg_started = True
    pg_env = {**os.environ, 'PGPASSWORD': password, 'PGHOST': '127.0.0.1',
              'PGPORT': '55432', 'PGUSER': 'hindsight_test'}
    exists = subprocess.check_output([str(PG / 'psql'), '-d', 'postgres', '-Atc',
        "SELECT 1 FROM pg_database WHERE datname='hindsight_luna_synthetic'"], env=pg_env, text=True).strip()
    if not exists:
        subprocess.run([str(PG / 'createdb'), 'hindsight_luna_synthetic'], env=pg_env, check=True)
    baseline = fixture.meminfo()
    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    qwen = start('qwen', ['nice', '-n', '10', '/var/tmp/nanobot-qwen3-eval/build/bin/llama-server',
        '--model', '/var/tmp/nanobot-qwen3-models/Qwen3-Embedding-0.6B-Q8_0.gguf',
        '--alias', fixture.ALIAS, '--host', '127.0.0.1', '--port', '8891', '--embedding',
        '--pooling', 'last', '--threads', '2', '--threads-batch', '2', '--ctx-size', '2048',
        '--batch-size', '1024', '--ubatch-size', '1024', '--parallel', '1'])
    ready(qwen, 'http://127.0.0.1:8891/health', 'qwen')
    env = {**os.environ, 'HOME': str(ROOT), 'CODEX_HOME': '/var/lib/hindsight/codex',
        'HINDSIGHT_API_DATABASE_URL': 'postgresql://hindsight_test:' + urllib.parse.quote(password) + '@127.0.0.1:55432/hindsight_luna_synthetic',
        'HINDSIGHT_API_LLM_PROVIDER': 'openai-codex',
        'HINDSIGHT_API_LLM_MODEL': 'gpt-6-luna',
        'HINDSIGHT_API_LLM_REASONING_EFFORT': 'low',
        'HINDSIGHT_API_LLM_OUTPUT_LANGUAGE': 'English',
        'HINDSIGHT_API_LLM_STRICT_SCHEMA': 'true',
        'HINDSIGHT_API_LLM_MAX_RETRIES': '1',
        'HINDSIGHT_API_LLM_TIMEOUT': '180',
        'HINDSIGHT_API_EMBEDDINGS_PROVIDER': 'openai',
        'HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY': 'local-not-a-real-key',
        'HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL': 'http://127.0.0.1:8891/v1',
        'HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL': fixture.ALIAS,
        'HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS': '1024',
        'HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE': '1',
        'HINDSIGHT_API_EMBEDDINGS_MAX_CONCURRENT_REQUESTS': '1',
        'HINDSIGHT_API_EMBEDDINGS_QUERY_PREFIX': fixture.INSTRUCTION,
        'HINDSIGHT_API_EMBEDDINGS_PASSAGE_PREFIX': '',
        'HINDSIGHT_API_RERANKER_PROVIDER': 'rrf',
        'HINDSIGHT_API_DB_POOL_MIN_SIZE': '1', 'HINDSIGHT_API_DB_POOL_MAX_SIZE': '8',
        'HINDSIGHT_API_LLM_MAX_CONCURRENT': '1',
        'HINDSIGHT_API_WORKER_MAX_SLOTS': '1',
        'HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS': '0',
        'HINDSIGHT_API_WORKER_ENABLED': 'true',
        'HINDSIGHT_API_ENABLE_OBSERVATIONS': 'true',
        'HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION': 'false',
        'HINDSIGHT_API_SKIP_LLM_VERIFICATION': 'true',
        'HINDSIGHT_API_WORKER_ID': 'felix-pi-synthetic-test',
        'DO_NOT_TRACK': '1', 'HF_HUB_OFFLINE': '1'}
    command = [str(VENV / 'bin/hindsight-api'), '--host', '127.0.0.1', '--port', '8889',
               '--workers', '1', '--no-access-log']
    api = start('hindsight', command, env)
    ready(api, URL + '/health', 'hindsight')
    event('stack_ready')
    import re
    bank = '/v1/default/banks/synthetic-luna-v1'
    initial_user = call(bank + '/mental-models/user-profile')
    old_revision = initial_user['last_refreshed_at']
    response = call(bank + '/memories', {'items': [{
        'content': 'User Mira, explicit correction on 16 October 2026: From now on I prefer answers in English, replacing my earlier preference for German answers. I still eat vegan food. The assistant name remains Lumen.',
        'document_id': 'explicit-language-correction',
        'timestamp': '2026-10-16T12:00:00Z',
        'context': 'Explicit user correction. Newer user preference supersedes the older USER.md baseline.'}], 'async': False})
    assert response['success']
    event('correction_retained', usage=response.get('usage'))
    consolidation = call(bank + '/consolidate', {}, 'POST')
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        operation = call(bank + '/operations/' + consolidation['operation_id'])
        if operation['status'] == 'completed':
            break
        if operation['status'] == 'failed':
            raise RuntimeError('Correction consolidation failed; inspect private service log')
        time.sleep(10)
    else:
        raise TimeoutError('Correction consolidation timed out')
    event('correction_consolidated')
    call(bank + '/mental-models/user-profile/refresh', {}, 'POST')
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        updated = call(bank + '/mental-models/user-profile')
        if updated.get('last_refresh_failed_at'):
            raise RuntimeError('Profile correction failed')
        if updated.get('last_refreshed_at') != old_revision:
            break
        time.sleep(10)
    else:
        raise TimeoutError('Profile correction refresh timed out')
    content = updated['content']
    # Keep the generated content in the report for semantic review as well.
    assert 'english' in content.lower(), 'Corrected profile did not mention English'
    assert 'vegan' in content.lower(), 'Unchanged vegan preference disappeared'
    # The new model must not still instruct German answers. Historical references
    # are allowed and are inspected in the saved report.
    suspicious = re.search(r'prefers?\s+(?:responses?\s+)?(?:in\s+)?german', content, re.I)
    assert not suspicious, 'Profile still states the superseded preference'
    soul = call(bank + '/mental-models/agent-soul')['content']
    assert 'Lumen' in soul
    recalled = call(bank + '/memories/recall', {'query': 'Which language does Mira prefer now?',
        'budget': 'low', 'max_tokens': 750})
    report = {'configured_internal_output_language': 'English',
        'user_correction_advanced_saved_model': True, 'user_profile': content,
        'unchanged_soul': soul, 'recall_results': recalled['results'],
        'retain_usage': response.get('usage'),
        'note': 'Synthetic correction only; no production profile files touched.'}
    (ROOT / 'correction-report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
    event('correction_complete', user_profile=content)

finally:
    stop.set()
    for label, process in reversed(processes):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    for handle in handles:
        handle.close()
    if pg_started:
        subprocess.run([str(PG / 'pg_ctl'), '-D', str(ROOT / 'pgdata'), '-m', 'fast', '-w', 'stop'], check=True)
