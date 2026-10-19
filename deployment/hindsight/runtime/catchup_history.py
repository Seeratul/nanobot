"""Queue a frozen incremental catch-up, preserving source archives and export cursors."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock
from nanobot.memory_sync.adapter import SessionMemoryExporter
from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.profiles import ProfileExporter
from nanobot.session.manager import Session, _migrate_legacy_exec_session_records

ROOT = Path('/home/nanobot/.nanobot/sessions/8679b657a17401f5f41c32a247d800ee')
WORKSPACE = Path('/home/nanobot/.nanobot/workspace')
MANIFEST = ROOT / 'hindsight-history-catchup.json'
PLAN = ROOT / 'hindsight-catchup-plan.json'
HOLD = Path('/var/lib/hindsight/import-timeout.hold')


class Collector:
    def __init__(self, outbox):
        self.outbox = outbox

    def context_generation(self, key):
        return self.outbox.context_generation(key)

    def enqueue(self, identity, key, fingerprints, messages):
        self.result = (identity, key, fingerprints, messages)


def prepare():
    if HOLD.exists() or Path('/var/lib/hindsight/control/forget-pending').exists():
        raise ValueError('memory hold requires review')
    if MANIFEST.exists():
        return {'status': 'already_prepared'}
    initial = json.loads((ROOT / 'hindsight-history-import.json').read_text())
    if initial.get('stage') != 'complete':
        raise ValueError('initial import verification must finish first')
    box = MemoryOutbox(ROOT / 'hindsight-outbox.sqlite3', bank_id='personal',
                       pause_path=Path('/var/lib/hindsight/control/forget-pending'))
    collector = Collector(box)
    known = tuple(v for k, v in os.environ.items()
                  if any(word in k.lower() for word in ('key', 'token', 'secret', 'password')))
    exporter = SessionMemoryExporter(collector, known_secrets=known)
    plans = []
    source_hashes = {}
    with FileLock(box.path.with_suffix('.delivery.lock'), timeout=0), \
            FileLock(ROOT / '.session-files.lock', timeout=30):
        with sqlite3.connect(box.path) as db:
            db.row_factory = sqlite3.Row
            cursors = {r['id']: r for r in db.execute('SELECT * FROM conversations')}
        if box.forget_pending():
            raise ValueError('forget request blocks catch-up')
        for path in ([] if PLAN.exists() else sorted(ROOT.glob('*.jsonl'))):
            key = base64.urlsafe_b64decode(path.stem + '=' * (-len(path.stem) % 4)).decode()
            if key.startswith(('dream:', 'subagent:')):
                continue
            raw = path.read_bytes()
            records = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
            headers = [r for r in records if r.get('_type') == 'metadata']
            if len(headers) != 1:
                raise ValueError('ambiguous source identity')
            header = headers[0]
            messages = [r for r in records if r.get('role') in {'user', 'assistant', 'tool'}
                        and not r.get('_type')]
            _migrate_legacy_exec_session_records(messages, header.get('metadata', {}))
            session = Session(key=key, created_at=datetime.fromisoformat(header['created_at']),
                              messages=messages, metadata=header.get('metadata', {}))
            collector.result = None
            exporter.capture(session, len(messages), 'new')
            if collector.result is None:
                raise ValueError('source context requires explicit reconciliation')
            identity, key, fingerprints, exported = collector.result
            old = cursors.get(identity)
            if old and old['suppressed']:
                continue
            start = old['queued_count'] if old else 0
            if start > len(fingerprints) or (start and
                    box.digest(fingerprints[:start]) != old['prefix_hash']):
                raise ValueError('exported source changed; explicit reconciliation required')
            if start == len(fingerprints):
                continue
            plans.append((identity, key, fingerprints, exported, start))
            source_hashes[path.name] = hashlib.sha256(raw).hexdigest()
        if PLAN.exists():
            saved = json.loads(PLAN.read_text())
            plans, source_hashes = saved['plans'], saved['source_hashes']
        else:
            ProfileExporter.write_atomic(PLAN, json.dumps(
                {'plans': plans, 'source_hashes': source_hashes}, ensure_ascii=False))
        documents = []
        for identity, key, fingerprints, exported, start in plans:
            box.enqueue(identity, key, fingerprints, exported)
            content = '\n\n'.join(m for m in exported[start:] if m)
            if content:
                document = f'conversation:{identity}:messages:{start + 1}-{len(fingerprints)}'
                # Source messages carry their own dates. Never invent a new event
                # date on retry, which would invalidate extraction checkpoints.
                with box._connect() as db:
                    payload = json.loads(db.execute('SELECT payload FROM jobs WHERE document_id=?',
                                                    (document,)).fetchone()['payload'])
                    if payload['items'][0]['content'] != content:
                        raise ValueError('queued catch-up differs from frozen plan')
                    for item in payload['items']:
                        item['timestamp'] = 'unset'
                    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    db.execute('UPDATE jobs SET payload=?,payload_hash=? WHERE document_id=?',
                               (raw, hashlib.sha256(raw.encode()).hexdigest(), document))
                documents.append(document)
        state = {'version': 1, 'stage': 'delivering', 'documents': documents,
                 'source_count': len(documents), 'source_hashes': source_hashes,
                 'profile_hashes': initial['profile_hashes'],
                 'snapshot_prepared_at': datetime.now(timezone.utc).isoformat(),
                 'job_counts': {'pending': len(documents)},
                 'exported_chars': sum(len('\n\n'.join(m for m in exported[start:] if m))
                                       for _, _, _, exported, start in plans)}
        ProfileExporter.write_atomic(MANIFEST, json.dumps(state, ensure_ascii=False, indent=2))
        PLAN.unlink()
    return {'status': 'prepared', 'documents': len(documents),
            'new_messages': sum(len(fingerprints) - start for _, _, fingerprints, _, start in plans),
            'exported_chars': state['exported_chars']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'deliver'])
    args = parser.parse_args()
    if args.action == 'prepare':
        print(json.dumps(prepare()), flush=True)
    else:
        if HOLD.exists():
            raise ValueError('timeout hold requires review')
        import import_history
        import_history.MANIFEST = MANIFEST
        asyncio.run(import_history.main())


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'needs_attention', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
