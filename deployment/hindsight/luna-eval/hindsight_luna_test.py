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
    import asyncio
    import sys
    import uuid
    sys.path.insert(0, '/tmp/nanobot-hindsight-dev')
    import httpx
    from nanobot.memory_sync.outbox import MemoryOutbox
    from nanobot.memory_sync.worker import HindsightWorker
    from nanobot.memory_sync.profiles import ProfileExporter, PROFILE_QUERIES

    bank_id = 'synthetic-luna-v1'
    bank = '/v1/default/banks/' + bank_id
    call(bank, {'name': 'Synthetic Luna acceptance test'}, 'PUT')
    docs = [
        ('user-baseline', 'Synthetic authorized USER.md baseline. The user Mira explicitly says: I prefer answers in German and I eat vegan food. Do not confuse my preferences with those of other people.'),
        ('soul-baseline', 'Synthetic authorized SOUL.md baseline. Mira explicitly requests: Your name is Lumen. Be calm, concise and direct. Avoid emojis in ordinary replies.'),
        ('atlas-backend', dict(fixture.DOCS)['atlas-backend']),
        ('atlas-frontend', dict(fixture.DOCS)['atlas-frontend']),
        ('notifications', dict(fixture.DOCS)['notifications']),
        ('suggestion', 'Assistant proposed that Mira might enjoy fish restaurants. Mira did not confirm this. It was only an assistant suggestion. Jonas, another person, likes seafood; Mira remains vegan.'),
    ]
    retain_usage = []
    for doc_id, text in docs:
        response = call(bank + '/memories', {'items': [{'content': text,
            'document_id': doc_id, 'timestamp': '2026-10-15T12:00:00Z'}], 'async': False})
        assert response['success'], 'Retain unsuccessful'
        retain_usage.append(response.get('usage'))
        event('luna_retained', document_id=doc_id, usage=response.get('usage'))

    results = []
    for query, expected in [
        ('Welche Sprache bevorzugt Mira fuer Antworten?', 'user-baseline'),
        ('What dietary preferences does Mira have?', 'user-baseline'),
        ('Which database and programming language are used in the Atlas backend?', 'atlas-backend'),
        ('Mit welchem Framework wurde die oeffentliche Atlas-Webseite gebaut?', 'atlas-frontend'),
        ('When should the assistant send notifications?', 'notifications'),
        ('What is the assistant name and communication style requested by Mira?', 'soul-baseline'),
    ]:
        started = time.monotonic()
        response = call(bank + '/memories/recall', {'query': query, 'budget': 'low', 'max_tokens': 750})
        rows = response['results']
        result = {'query': query, 'expected_document': expected,
            'top1_hit': bool(rows and rows[0].get('document_id') == expected),
            'top3_hit': any(r.get('document_id') == expected for r in rows[:3]),
            'seconds': time.monotonic() - started, 'results': rows[:3]}
        results.append(result)
        event('luna_recalled', expected=expected, top1_hit=result['top1_hit'], top3_hit=result['top3_hit'])

    def wait_operation(operation_id, stage):
        deadline = time.monotonic() + 600
        previous = None
        while time.monotonic() < deadline:
            state = call(bank + '/operations/' + operation_id)
            status = state.get('status')
            if status != previous:
                event(stage, status=status)
                previous = status
            if status == 'completed':
                return state
            if status == 'failed':
                # Error bodies can include upstream payloads; keep them private on Pi.
                (ROOT / (stage + '-failure.json')).write_text(json.dumps(state, indent=2))
                raise RuntimeError(stage + ' failed; inspect private diagnostic file')
            time.sleep(10)
        raise TimeoutError(stage + ' did not complete within test deadline')

    consolidation = call(bank + '/consolidate', {}, 'POST')
    wait_operation(consolidation['operation_id'], 'consolidation')
    observations = call(bank + '/memories/recall', {'query': 'What lasting preferences does Mira have?',
        'types': ['observation'], 'budget': 'low', 'max_tokens': 750})
    assert observations['results'], 'Consolidation returned no observations'
    event('observations_recalled', count=len(observations['results']))

    models = {}
    for model_id, query in PROFILE_QUERIES.items():
        response = call(bank + '/mental-models', {'id': model_id, 'name': model_id,
            'source_query': query, 'max_tokens': 750, 'trigger': {'refresh_after_consolidation': False}})
        # Creation queues the initial generation in the pinned v1 API.
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            model = call(bank + '/mental-models/' + model_id)
            if model.get('last_refresh_failed_at'):
                raise RuntimeError('Mental Model generation failed for ' + model_id)
            if model.get('content') and model.get('last_refreshed_at'):
                models[model_id] = model
                break
            time.sleep(10)
        else:
            raise TimeoutError('Mental Model generation did not finish: ' + model_id)
        event('mental_model_ready', model_id=model_id)

    workspace = ROOT / 'synthetic-workspace'
    workspace.mkdir(exist_ok=True)
    (workspace / 'SOUL.md').write_text('# Soul\nInitial synthetic baseline.\n')
    (workspace / 'USER.md').write_text('# User\nInitial synthetic baseline.\n')
    outbox = MemoryOutbox(ROOT / 'outbox.sqlite3', bank_id=bank_id)
    worker = HindsightWorker(outbox, api_url=URL, bank_id=bank_id)
    exporter = ProfileExporter(worker, workspace, ROOT / 'profile-state.json')
    exporter.register_baseline()
    projected = asyncio.run(exporter.sync_once())
    assert set(projected.values()) == {'updated'}, projected
    user_content = (workspace / 'USER.md').read_text()
    soul_content = (workspace / 'SOUL.md').read_text()
    assert 'vegan' in user_content.lower(), 'User profile lost explicit vegan preference'
    assert 'lumen' in soul_content.lower(), 'Soul lost explicit name'
    event('profiles_projected', statuses=projected)
    (workspace / 'USER.md').write_text(user_content + '\nLocal synthetic edit.\n')
    drift = asyncio.run(exporter.sync_once())
    assert drift['user-profile'] == 'local_edit', drift

    records = [json.dumps({'role': 'user', 'content': 'Mira explicitly says: I play the cello in my free time.'})]
    outbox.enqueue('synthetic-cello', 'test:synthetic', records, ['user: I play the cello in my free time.'])
    job = outbox.next_job()
    submitted = asyncio.run(worker.run_once())
    assert submitted == 'submitted', submitted
    wait_operation(job.operation_id, 'outbox_delivery')
    # Recreate both objects to verify durable reconciliation after a process restart.
    outbox = MemoryOutbox(ROOT / 'outbox.sqlite3', bank_id=bank_id)
    time.sleep(16)
    worker = HindsightWorker(outbox, api_url=URL, bank_id=bank_id)
    reconciled = asyncio.run(worker.run_once())
    assert reconciled == 'completed', reconciled
    duplicate = call(bank + '/memories', json.loads(job.payload))
    assert duplicate['operation_id'] == job.operation_id, 'Operation idempotency failed'
    event('outbox_completed_and_duplicate_reconciled')

    quota_box = MemoryOutbox(ROOT / 'quota-test.sqlite3', bank_id=bank_id)
    quota_box.enqueue('quota-synthetic', 'test:quota', ['a'], ['Synthetic queued record'])
    async def quota_test():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(429))) as client:
            quota_worker = HindsightWorker(quota_box, api_url=URL, bank_id=bank_id, client=client)
            status = await quota_worker.run_once()
            assert status == 'retry' and quota_box.status() == {'pending': 1}
            assert quota_box.next_job() is None
            return status
    quota_status = asyncio.run(quota_test())
    # Actual API outage: last good profile files must survive.
    before_outage = {p.name: p.read_text() for p in workspace.glob('*.md')}
    api.terminate()
    api.wait(timeout=30)
    try:
        asyncio.run(exporter.sync_once())
        raise AssertionError('Expected a transport failure while API is stopped')
    except httpx.TransportError:
        pass
    assert before_outage == {p.name: p.read_text() for p in workspace.glob('*.md')}
    report = {'model': 'gpt-6-luna', 'provider': 'openai-codex', 'reasoning_effort': 'low',
        'strict_schema': True, 'synthetic_documents': len(docs),
        'top1_hits': sum(r['top1_hit'] for r in results),
        'top3_hits': sum(r['top3_hit'] for r in results), 'queries': len(results),
        'retains_usage': retain_usage, 'results': results,
        'consolidation_observations': observations['results'],
        'mental_models': {k: v['content'] for k, v in models.items()},
        'profile_projection': projected, 'profile_local_edit_protected': True,
        'profile_api_outage_preserved_files': True,
        'real_async_outbox_completed_after_worker_restart': True,
        'duplicate_operation_id_reused': True,
        'simulated_http_429_preserved_pending_job_with_backoff': quota_status == 'retry',
        'system_available_min_mib': min(p['MemAvailable'] for p in samples) / 1024,
        'additional_swap_mib': (baseline['SwapFree'] - min(p['SwapFree'] for p in samples)) / 1024,
        'temperature_max_c': max(p['temperature_c'] for p in samples)}
    (ROOT / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
    event('luna_complete', **{k: v for k, v in report.items() if k not in {'results', 'mental_models', 'consolidation_observations', 'retains_usage'}})

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
