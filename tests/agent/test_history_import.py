"""Initial history submission gates and crash recovery."""
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from nanobot.memory_sync.outbox import MemoryOutbox


@pytest.fixture
def migration(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'deployment/hindsight/runtime/import_history.py'
    spec = importlib.util.spec_from_file_location('history_import_under_test', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, filename in [('PLAN', 'plan.json'), ('MANIFEST', 'manifest.json'), ('PROBE', 'probe.json')]:
        monkeypatch.setattr(module, name, tmp_path / filename)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    for name in ('SOUL.md', 'USER.md'):
        (workspace / name).write_text('# Existing profile\n', encoding='utf-8')
    monkeypatch.setattr(module, 'WORKSPACE', workspace)
    outbox = MemoryOutbox(tmp_path / 'outbox.sqlite3')
    plan = {'version': 1, 'bank_id': 'personal', 'skipped': {'dream': 2}, 'sessions': [{
        'identity': str(uuid4()), 'key': 'discord:fixture',
        'fingerprints': ['source-a', 'source-b'], 'messages': ['user\nI play cello.', ''],
        'source_file': 'fixture.jsonl', 'source_sha256': 'a' * 64}]}
    module.PLAN.write_text(json.dumps(plan), encoding='utf-8')
    digest = hashlib.sha256(module.PLAN.read_bytes()).hexdigest()
    module.PROBE.write_text(json.dumps({'passed': True, 'plan_sha256': digest}), encoding='utf-8')
    return module, outbox, plan


def test_queues_once_and_preserves_profiles(migration):
    module, outbox, _ = migration
    before = {p.name: p.read_bytes() for p in module.WORKSPACE.iterdir()}
    state = module.queue_plan(outbox)
    assert state['stage'] == 'delivering'
    assert len(state['documents']) == 1
    assert outbox.status() == {'pending': 1}
    assert not module.PLAN.exists()
    assert module.queue_plan(outbox) == state
    assert before == {p.name: p.read_bytes() for p in module.WORKSPACE.iterdir()}


def test_recovers_after_enqueue_before_manifest_commit(migration):
    module, outbox, plan = migration
    source = plan['sessions'][0]
    outbox.enqueue(source['identity'], source['key'], source['fingerprints'], source['messages'])
    state = module.queue_plan(outbox)
    assert len(state['documents']) == 1
    assert outbox.status() == {'pending': 1}


@pytest.mark.parametrize('change', ['rejected', 'changed_plan'])
def test_requires_matching_embedding_acceptance(migration, change):
    module, outbox, _ = migration
    if change == 'rejected':
        module.PROBE.write_text(json.dumps({'passed': False}), encoding='utf-8')
    else:
        module.PLAN.write_text(module.PLAN.read_text() + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='embedding acceptance'):
        module.queue_plan(outbox)
    assert outbox.status() == {}


def test_conflicting_source_is_rejected(migration):
    module, outbox, plan = migration
    source = plan['sessions'][0]
    outbox.enqueue(source['identity'], source['key'], ['other', 'source-b'], ['changed', ''])
    with pytest.raises(ValueError, match='previously exported source'):
        module.queue_plan(outbox)


def test_pending_forget_blocks_history(migration):
    module, outbox, _ = migration
    outbox.pause_path.touch()
    with pytest.raises(ValueError, match='pending forget'):
        module.queue_plan(outbox)
    assert outbox.status() == {}


def test_suppressed_source_is_not_reimported(migration):
    module, outbox, plan = migration
    source = plan['sessions'][0]
    outbox.enqueue(source['identity'], source['key'], source['fingerprints'], source['messages'])
    outbox.suppress_session(source['key'])
    with pytest.raises(ValueError, match='conflicting prior jobs'):
        module.queue_plan(outbox)
    with sqlite3.connect(outbox.path) as db:
        assert db.execute('SELECT payload FROM jobs').fetchone()[0] == ''


def test_slow_embedding_exceeds_native_client_timeout(migration):
    module, outbox, _ = migration
    probe = json.loads(module.PROBE.read_text())
    probe['embedding_cases'] = [{'seconds': 601}]
    module.PROBE.write_text(json.dumps(probe), encoding='utf-8')
    with pytest.raises(ValueError, match='embedding acceptance'):
        module.queue_plan(outbox)
    assert outbox.status() == {}


async def test_verify_accepts_identical_fact_from_another_source(migration, monkeypatch):
    import httpx

    module, outbox, _ = migration
    state = module.queue_plan(outbox)
    identity = state['documents'][0]
    def handler(request):
        if request.url.path.endswith('/documents'):
            data = {'items': [{'id': identity}], 'total': 1}
        elif request.url.path.endswith('/stats'):
            data = {'total_documents': 1, 'nodes_by_fact_type': {'world': 1}, 'operations_by_status': {}}
        elif request.url.path.endswith('/memories/list'):
            data = {'items': [{'text': 'The user plays cello.', 'fact_type': 'world'}]}
        elif request.url.path.endswith('/memories/recall'):
            data = {'results': [{'text': 'The user plays cello.', 'document_id': 'another-source'}]}
        else:
            raise AssertionError(request.url.path)
        return httpx.Response(200, json=data)
    original = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kw: original(
        transport=httpx.MockTransport(handler), base_url=kw['base_url']))
    assert await module.verify(state)
    assert state['stage'] == 'complete'
    assert state['verification']['duplicate_fact_hits'] == 1
    assert state['verification']['local_profiles_unchanged'] is True
