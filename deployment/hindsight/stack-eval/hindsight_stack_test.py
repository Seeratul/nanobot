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

ROOT = pathlib.Path('/var/tmp/hindsight-pi-stack-test')
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
        "SELECT 1 FROM pg_database WHERE datname='hindsight_synthetic'"], env=pg_env, text=True).strip()
    if not exists:
        subprocess.run([str(PG / 'createdb'), 'hindsight_synthetic'], env=pg_env, check=True)
    baseline = fixture.meminfo()
    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    qwen = start('qwen', ['nice', '-n', '10', '/var/tmp/nanobot-qwen3-eval/build/bin/llama-server',
        '--model', '/var/tmp/nanobot-qwen3-models/Qwen3-Embedding-0.6B-Q8_0.gguf',
        '--alias', fixture.ALIAS, '--host', '127.0.0.1', '--port', '8891', '--embedding',
        '--pooling', 'last', '--threads', '2', '--threads-batch', '2', '--ctx-size', '2048',
        '--batch-size', '1024', '--ubatch-size', '1024', '--parallel', '1'])
    ready(qwen, 'http://127.0.0.1:8891/health', 'qwen')
    env = {**os.environ, 'HOME': str(ROOT), 'CODEX_HOME': str(ROOT / 'codex-unused'),
        'HINDSIGHT_API_DATABASE_URL': 'postgresql://hindsight_test:' + urllib.parse.quote(password) + '@127.0.0.1:55432/hindsight_synthetic',
        'HINDSIGHT_API_LLM_PROVIDER': 'none',
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
        'HINDSIGHT_API_ENABLE_OBSERVATIONS': 'false',
        'HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION': 'false',
        'HINDSIGHT_API_SKIP_LLM_VERIFICATION': 'true',
        'HINDSIGHT_API_WORKER_ID': 'felix-pi-synthetic-test',
        'DO_NOT_TRACK': '1', 'HF_HUB_OFFLINE': '1'}
    command = [str(VENV / 'bin/hindsight-api'), '--host', '127.0.0.1', '--port', '8889',
               '--workers', '1', '--no-access-log']
    api = start('hindsight', command, env)
    ready(api, URL + '/health', 'hindsight')
    event('stack_ready')
    bank = '/v1/default/banks/synthetic-qwen3'
    call(bank, {'name': 'Synthetic Qwen3 test'}, 'PUT')
    ingestion = []
    for doc_id, text in fixture.DOCS:
        before = time.monotonic()
        response = call(bank + '/memories', {'items': [{'content': text, 'document_id': doc_id}], 'async': False})
        assert response['success'], response
        elapsed = time.monotonic() - before
        ingestion.append(elapsed)
        event('retained', document_id=doc_id, seconds=round(elapsed, 3))
    results = []
    for query, expected in fixture.QUERIES:
        before = time.monotonic()
        response = call(bank + '/memories/recall', {'query': query, 'budget': 'low', 'max_tokens': 512})
        elapsed = time.monotonic() - before
        # Use original text for attribution; the chunk-only provider has no LLM extraction.
        texts = [item['text'] for item in response['results']]
        expected_text = dict(fixture.DOCS)[expected]
        hit = bool(texts and expected_text in texts[0])
        result = {'query': query, 'expected': expected, 'top1_hit': hit,
                  'seconds': elapsed, 'returned_texts': texts[:3]}
        results.append(result)
        event('recalled', expected=expected, top1_hit=hit, seconds=round(elapsed, 3))
    api.terminate()
    api.wait(timeout=30)
    api = start('hindsight-restarted', command, env)
    ready(api, URL + '/health', 'hindsight-restarted')
    response = call(bank + '/memories/recall', {'query': fixture.QUERIES[0][0], 'budget': 'low', 'max_tokens': 512})
    persisted = any(dict(fixture.DOCS)['language'] in item['text'] for item in response['results'])
    assert persisted, 'Data did not survive API restart'
    report = {'mode': 'LLM none: chunks and recall only; no Luna extraction/consolidation',
        'hindsight_version': '1.0.0', 'postgresql_version': '17.11', 'pgvector_version': '0.8.0',
        'qwen_threads': 2, 'synthetic_documents': len(fixture.DOCS),
        'top1_hits': sum(r['top1_hit'] for r in results), 'queries': len(results),
        'query_median_seconds': statistics.median(r['seconds'] for r in results),
        'query_max_seconds': max(r['seconds'] for r in results),
        'ingestion_median_seconds': statistics.median(ingestion),
        'api_restart_persistence_passed': persisted,
        'system_available_before_mib': baseline['MemAvailable'] / 1024,
        'system_available_min_mib': min(p['MemAvailable'] for p in samples) / 1024,
        'additional_swap_mib': (baseline['SwapFree'] - min(p['SwapFree'] for p in samples)) / 1024,
        'qwen_peak_rss_mib': max(p.get('qwen_rss_kib', 0) for p in samples) / 1024,
        'hindsight_peak_rss_mib': max(max(p.get('hindsight_rss_kib', 0), p.get('hindsight-restarted_rss_kib', 0)) for p in samples) / 1024,
        'temperature_max_c': max(p['temperature_c'] for p in samples), 'results': results}
    (ROOT / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
    event('complete', **{k: v for k, v in report.items() if k != 'results'})
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
