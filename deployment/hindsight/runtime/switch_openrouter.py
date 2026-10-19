"""Explicit model migration with a private recovery point; never print credentials/content."""
import argparse
import asyncio
import hashlib
import json
import os
import pwd
import re
import shutil
import sqlite3
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx

DATA = Path('/var/lib/hindsight')
OPT = Path('/opt/hindsight')
ROOT = Path('/home/nanobot/.nanobot/sessions/8679b657a17401f5f41c32a247d800ee')
RUNTIME = DATA / 'runtime.json'
POINTER = DATA / 'openrouter-migration.json'
NEW_DATABASE = 'hindsight_openrouter_20261019'
MODEL = 'qwen/qwen3-embedding-8b'
PG = Path('/usr/lib/postgresql/17/bin')


def atomic(path, content, uid=0, gid=0, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.migration-tmp')
    with temp.open('w') as handle:
        os.fchmod(handle.fileno(), mode)
        os.fchown(handle.fileno(), uid, gid)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def state():
    return json.loads(POINTER.read_text())


def save(value):
    atomic(POINTER, json.dumps(value))


def run(*args):
    return subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stopped():
    for unit in ['hindsight-history-import', 'hindsight-worker', 'hindsight-api']:
        active = subprocess.check_output(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'], text=True).strip()
        if active not in ['inactive', 'failed']:
            raise RuntimeError('migration requires stopped services')
    if (DATA / 'control/forget-pending').exists():
        raise RuntimeError('fact retirement blocks migration')


def key():
    for line in Path('/home/nanobot/.config/nanobot/secrets.env').read_text().splitlines():
        match = re.match(r'^\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.*?)\s*$', line)
        if match and match.group(1):
            return match.group(1).strip("'\"")
    raise RuntimeError('OpenRouter credential unavailable')


def backup_sqlite(source, destination):
    with sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('invalid recovery sqlite')
    destination.chmod(0o600)


def snapshot():
    if POINTER.exists():
        raise RuntimeError('migration already recorded; inspect or rollback explicitly')
    if (DATA / 'control/forget-pending').exists():
        raise RuntimeError('pending forget blocks migration')
    for unit in ['hindsight-history-import', 'hindsight-worker', 'hindsight-api', 'hindsight-embedding']:
        run('systemctl', 'stop', unit)
    stopped()
    account = pwd.getpwnam('hindsight')
    parent = DATA / 'recovery'
    parent.mkdir(mode=0o711, exist_ok=True)
    target = parent / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-local-qwen')
    target.mkdir(mode=0o700)
    os.chown(target, account.pw_uid, account.pw_gid)
    value = {'recovery': str(target), 'stage': 'snapshot_in_progress', 'new_database': NEW_DATABASE}
    save(value)
    copies = {'runtime.json': RUNTIME,
              'history-import.json': ROOT / 'hindsight-history-import.json',
              'bootstrap.json': ROOT / 'hindsight-bootstrap.json',
              'profiles.json': ROOT / 'hindsight-profiles.json',
              'api.service': Path('/etc/systemd/system/hindsight-api.service'),
              'usage_worker.py': OPT / 'usage_worker.py',
              'resume_scheduled_import.py': OPT / 'maintenance/resume_scheduled_import.py'}
    for name, source in copies.items():
        shutil.copy2(source, target / name)
        (target / name).chmod(0o600)
    backup_sqlite(ROOT / 'hindsight-outbox.sqlite3', target / 'outbox.sqlite3')
    backup_sqlite(DATA / 'checkpoints/recovery.sqlite3', target / 'checkpoints.sqlite3')
    run('sudo', '-u', 'hindsight', str(PG / 'pg_dump'), '-h', '/run/hindsight-postgres',
        '-p', '55433', '-U', 'hindsight', '-d', 'hindsight', '-Fc', '-f', str(target / 'database.dump'))
    run(str(PG / 'pg_restore'), '--file=/dev/null', str(target / 'database.dump'))
    manifest = {p.name: hashlib.file_digest(p.open('rb'), 'sha256').hexdigest()
                for p in target.iterdir() if p.is_file()}
    atomic(target / 'checksums.json', json.dumps(manifest))
    for p in target.iterdir():
        with p.open('rb') as handle:
            os.fsync(handle.fileno())
    value['stage'] = 'snapshot_ready'
    save(value)
    print(json.dumps({'stage': value['stage'], 'recovery': str(target), 'archive_verified': True}), flush=True)


async def switch():
    stopped()
    value = state()
    if value['stage'] != 'snapshot_ready':
        raise RuntimeError('verified recovery point required')
    old = json.loads(RUNTIME.read_text())
    db = await asyncpg.connect(old['HINDSIGHT_API_DATABASE_URL'], timeout=10)
    try:
        if await db.fetchval('SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)', NEW_DATABASE):
            raise RuntimeError('new destination database already exists')
        await db.execute('CREATE DATABASE ' + NEW_DATABASE + ' OWNER hindsight')
    finally:
        await db.close()
    new = dict(old)
    parsed = urlsplit(old['HINDSIGHT_API_DATABASE_URL'])
    new['HINDSIGHT_API_DATABASE_URL'] = urlunsplit(parsed._replace(path='/' + NEW_DATABASE))
    new.update({'HINDSIGHT_API_EMBEDDINGS_PROVIDER': 'openai',
                'HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY': key(),
                'HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL': 'https://openrouter.ai/api/v1',
                'HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL': MODEL,
                'HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS': '1024',
                'HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE': '8',
                'HINDSIGHT_API_EMBEDDINGS_MAX_CONCURRENT_REQUESTS': '1',
                'HINDSIGHT_API_WORKER_MAX_RETRIES': '2'})
    account = pwd.getpwnam('hindsight')
    atomic(RUNTIME, json.dumps(new), account.pw_uid, account.pw_gid)
    unit = Path('/etc/systemd/system/hindsight-api.service')
    content = unit.read_text().replace('hindsight-postgres.service hindsight-embedding.service', 'hindsight-postgres.service')
    atomic(unit, content, mode=0o644)
    # A separate empty recovery cache prevents cross-model/old-operation reuse.
    checkpoint = DATA / 'checkpoints/recovery.sqlite3'
    for suffix in ['-wal', '-shm', '-journal']:
        if Path(str(checkpoint) + suffix).exists():
            raise RuntimeError('unexpected live checkpoint sidecar')
    checkpoint.unlink()
    from retain_checkpoints import Store
    Store(checkpoint)
    os.chown(checkpoint, account.pw_uid, account.pw_gid)
    value['stage'] = 'configured'
    save(value)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'disable', 'hindsight-embedding')
    run('systemctl', 'start', 'hindsight-api')
    print(json.dumps({'stage': 'configured', 'embedding_model': MODEL, 'dimensions': 1024,
                      'old_database_preserved': True, 'fresh_database': True}), flush=True)


async def seed():
    value = state()
    if value['stage'] != 'configured':
        raise RuntimeError('unexpected migration stage')
    config = json.loads(RUNTIME.read_text())
    old_config = json.loads((Path(value['recovery']) / 'runtime.json').read_text())
    old = await asyncpg.connect(old_config['HINDSIGHT_API_DATABASE_URL'], timeout=10)
    new = await asyncpg.connect(config['HINDSIGHT_API_DATABASE_URL'], timeout=10)
    try:
        if await new.fetchval('SELECT count(*) FROM memory_units'):
            raise RuntimeError('fresh destination already contains facts')
        bank = await old.fetchrow('SELECT bank_id,name,disposition,mission,config FROM banks WHERE bank_id=$1', 'personal')
        bank_config = json.loads(bank['config']) if isinstance(bank['config'], str) else bank['config']
        if any('embedding' in k.lower() for k in bank_config):
            raise RuntimeError('bank has embedding override requiring reconciliation')
        await new.execute('INSERT INTO banks(bank_id,name,disposition,mission,config) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',
                          *[bank[k] for k in ['bank_id', 'name', 'disposition', 'mission', 'config']])
        # Metadata import bypasses native bank creation, so restore its normal
        # per-fact-type index initialization explicitly before retaining facts.
        os.environ.update(config)
        from hindsight_api.engine.db.postgresql import PostgreSQLBackend
        from hindsight_api.engine.retain.bank_utils import create_bank_vector_indexes

        internal_id = await new.fetchval('SELECT internal_id FROM banks WHERE bank_id=$1', 'personal')
        await create_bank_vector_indexes(new, 'personal', str(internal_id), ops=PostgreSQLBackend().ops)
        models = await old.fetch('SELECT id,bank_id,name,source_query,content,tags,max_tokens,trigger,subtype,description FROM mental_models WHERE bank_id=$1', 'personal')
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            for model in models:
                response = await client.post('https://openrouter.ai/api/v1/embeddings',
                    headers={'Authorization': 'Bearer ' + key()},
                    json={'model': MODEL, 'input': [model['content']], 'dimensions': 1024, 'encoding_format': 'float'})
                if not response.is_success:
                    raise RuntimeError('profile embedding failed')
                vector = response.json()['data'][0]['embedding']
                if len(vector) != 1024:
                    raise RuntimeError('profile embedding dimension mismatch')
                await new.execute('INSERT INTO mental_models(id,bank_id,name,source_query,content,tags,max_tokens,trigger,subtype,description,embedding) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::vector) ON CONFLICT DO NOTHING',
                    *[model[k] for k in ['id','bank_id','name','source_query','content','tags','max_tokens','trigger','subtype','description']],
                    '[' + ','.join(map(str, vector)) + ']')
    finally:
        await old.close()
        await new.close()
    box = ROOT / 'hindsight-outbox.sqlite3'
    history = ROOT / 'hindsight-history-import.json'
    manifest = json.loads(history.read_text())
    with sqlite3.connect(box) as db:
        if db.execute('SELECT count(*) FROM forget_requests').fetchone()[0] or db.execute('SELECT count(*) FROM conversations WHERE suppressed=1').fetchone()[0]:
            raise RuntimeError('retired sources require explicit reconciliation')
        rows = db.execute('SELECT document_id,payload,created_at FROM jobs').fetchall()
        for identity, raw, created in rows:
            payload = json.loads(raw)
            # Historical texts already carry their own source dates. Explicitly
            # mark the enclosing snapshot undated instead of inventing a new
            # conversation date at each execution/retry.
            for item in payload['items']:
                item['timestamp'] = 'unset'
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            db.execute("UPDATE jobs SET payload=?,payload_hash=?,state='pending',attempts=0,next_attempt_at=0,error_code=NULL WHERE document_id=?",
                       (encoded, hashlib.sha256(encoded.encode()).hexdigest(), identity))
    manifest.update(stage='delivering', job_counts={'pending': len(manifest['documents'])},
                    embedding_model=MODEL, embedding_dimensions=1024,
                    restart_at=datetime.now(timezone.utc).isoformat())
    manifest.pop('verification', None)
    owner = history.stat()
    atomic(history, json.dumps(manifest, ensure_ascii=False, indent=2), owner.st_uid, owner.st_gid)
    value['stage'] = 'ready'
    save(value)
    print(json.dumps({'stage': 'ready', 'queued_sources_including_profiles': len(rows),
                      'profile_models_preserved': len(models), 'timestamps': 'explicitly_unset'}), flush=True)


def rollback():
    value = state()
    target = Path(value['recovery'])
    if not (target / 'checksums.json').is_file():
        raise RuntimeError('incomplete recovery point')
    for name, digest in json.loads((target / 'checksums.json').read_text()).items():
        if hashlib.file_digest((target / name).open('rb'), 'sha256').hexdigest() != digest:
            raise RuntimeError('recovery integrity mismatch')
    for unit in ['hindsight-history-import', 'hindsight-worker', 'hindsight-api']:
        run('systemctl', 'stop', unit)
    stopped()
    # An explicit rollback restores the old worker without the new stop adapter.
    # Keep any timeout evidence in the private recovery directory.
    timeout_path = DATA / 'checkpoints/import-timeout.hold'
    if timeout_path.exists():
        shutil.copy2(timeout_path, target / 'rollback-timeout-evidence.json')
        timeout_path.unlink()
    subprocess.run(['systemctl', 'disable', '--now', 'hindsight-timeout-stop.path'],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for unit in ['hindsight-worker', 'hindsight-history-import']:
        (Path('/etc/systemd/system') / (unit + '.service.d/timeout-stop.conf')).unlink(missing_ok=True)
    account = pwd.getpwnam('hindsight')
    nanobot = pwd.getpwnam('nanobot')
    for name, destination, uid, gid, mode in [
        ('runtime.json', RUNTIME, account.pw_uid, account.pw_gid, 0o600),
        ('api.service', Path('/etc/systemd/system/hindsight-api.service'), 0, 0, 0o644),
        ('usage_worker.py', OPT / 'usage_worker.py', 0, 0, 0o644),
        ('resume_scheduled_import.py', OPT / 'maintenance/resume_scheduled_import.py', 0, 0, 0o644),
        ('history-import.json', ROOT / 'hindsight-history-import.json', nanobot.pw_uid, nanobot.pw_gid, 0o600),
    ]:
        atomic(destination, (target / name).read_text(), uid, gid, mode)
    for name, destination, owner in [
        ('outbox.sqlite3', ROOT / 'hindsight-outbox.sqlite3', nanobot),
        ('checkpoints.sqlite3', DATA / 'checkpoints/recovery.sqlite3', account),
    ]:
        # Restore via SQLite backup, including any destination journal state.
        backup_sqlite(target / name, destination)
        os.chown(destination, owner.pw_uid, owner.pw_gid)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'hindsight-embedding')
    # Old processing jobs need one recovery allowance; no task rows are edited.
    config = json.loads(RUNTIME.read_text())
    config['HINDSIGHT_API_WORKER_MAX_RETRIES'] = str(max(3, int(config.get('HINDSIGHT_API_WORKER_MAX_RETRIES', '2'))))
    atomic(RUNTIME, json.dumps(config), account.pw_uid, account.pw_gid)
    value['stage'] = 'rolled_back_stopped'
    save(value)
    print(json.dumps({'stage': value['stage'], 'resume_command': 'sudo /opt/hindsight/venv/bin/python /opt/hindsight/maintenance/resume_scheduled_import.py'}))


def main():
    if os.geteuid() != 0:
        raise RuntimeError('root required')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['snapshot', 'switch', 'seed', 'rollback', 'report'])
    args = parser.parse_args()
    if args.action == 'snapshot':
        snapshot()
    elif args.action == 'switch':
        asyncio.run(switch())
    elif args.action == 'seed':
        asyncio.run(seed())
    elif args.action == 'rollback':
        rollback()
    else:
        print(json.dumps(state()))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        frames = [{'function': frame.name, 'line': frame.lineno}
                  for frame in traceback.extract_tb(error.__traceback__)
                  if Path(frame.filename).name == Path(__file__).name]
        print(json.dumps({'status': 'needs_attention', 'error_type': type(error).__name__,
                          'frames': frames}), flush=True)
        raise SystemExit(1) from None
