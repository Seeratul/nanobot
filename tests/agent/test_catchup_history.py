"""Incremental catch-up must neither duplicate nor lose queued source tails."""
import base64
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest
from nanobot.memory_sync.adapter import SessionMemoryExporter
from nanobot.session.manager import Session


@pytest.fixture
def catchup(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / 'deployment/hindsight/runtime/catchup_history.py'
    spec = importlib.util.spec_from_file_location('catchup_under_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / 'sessions'
    root.mkdir()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(module, 'WORKSPACE', workspace)
    monkeypatch.setattr(module, 'MANIFEST', root / 'catchup.json')
    monkeypatch.setattr(module, 'PLAN', root / 'plan.json')
    monkeypatch.setattr(module, 'HOLD', tmp_path / 'timeout.hold')
    (root / 'hindsight-history-import.json').write_text(json.dumps(
        {'stage': 'complete', 'profile_hashes': {}}))
    # Production uses the fixed forget marker. Tests route it to this fixture.
    original = module.MemoryOutbox
    monkeypatch.setattr(module, 'MemoryOutbox', lambda path, **kw: original(
        path, bank_id=kw['bank_id'], pause_path=tmp_path / 'forget-pending'))
    box = module.MemoryOutbox(root / 'hindsight-outbox.sqlite3', bank_id='personal')
    key = 'discord:fixture'
    created = '2026-10-17T12:00:00'
    first = {'role': 'user', 'content': 'I play cello.', 'timestamp': created}
    second = {'role': 'user', 'content': 'I also play piano.', 'timestamp': created}
    session = Session(key=key, created_at=datetime.fromisoformat(created), messages=[first])
    SessionMemoryExporter(box).capture(session, 1, 'new')
    filename = base64.urlsafe_b64encode(key.encode()).decode().rstrip('=') + '.jsonl'
    path = root / filename
    path.write_text('\n'.join(json.dumps(r) for r in [
        {'_type': 'metadata', 'created_at': created, 'metadata': {}}, first, second]))
    return module, box, path


def test_only_new_tail_is_queued_and_repeat_is_idempotent(catchup):
    module, box, path = catchup
    before = path.read_bytes()
    report = module.prepare()
    assert report['documents'] == 1 and report['new_messages'] == 1
    assert box.status() == {'pending': 2}
    with box._connect() as db:
        row = db.execute('SELECT payload FROM jobs WHERE first_seq=2').fetchone()
        item = json.loads(row['payload'])['items'][0]
        assert 'piano' in item['content'] and 'cello' not in item['content']
        assert item['timestamp'] == 'unset'
    assert module.prepare() == {'status': 'already_prepared'}
    assert box.status() == {'pending': 2}
    assert path.read_bytes() == before
    assert not module.PLAN.exists()


def test_resume_after_queue_commit_before_manifest_does_not_lose_tail(catchup, monkeypatch):
    module, box, _ = catchup
    original = module.ProfileExporter.write_atomic
    def fail_manifest(path, text):
        if path == module.MANIFEST:
            raise OSError('synthetic interruption')
        original(path, text)
    monkeypatch.setattr(module.ProfileExporter, 'write_atomic', fail_manifest)
    with pytest.raises(OSError):
        module.prepare()
    assert module.PLAN.exists() and box.status() == {'pending': 2}
    monkeypatch.setattr(module.ProfileExporter, 'write_atomic', original)
    assert module.prepare()['documents'] == 1
    assert len(json.loads(module.MANIFEST.read_text())['documents']) == 1
    assert box.status() == {'pending': 2}


def test_changed_exported_prefix_fails_without_queueing(catchup):
    module, box, path = catchup
    path.write_text(path.read_text().replace('I play cello.', 'Changed original.'))
    with pytest.raises(ValueError, match='exported source changed'):
        module.prepare()
    assert box.status() == {'pending': 1}
    assert not module.PLAN.exists() and not module.MANIFEST.exists()


def test_timeout_hold_prevents_preparation(catchup):
    module, box, _ = catchup
    module.HOLD.touch()
    with pytest.raises(ValueError, match='hold requires review'):
        module.prepare()
    assert box.status() == {'pending': 1}
