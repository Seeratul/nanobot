"""Resume the explicitly authorized initial history import through the existing outbox.

Runs locally as nanobot. Source archives and profile files are never modified.
Only aggregate progress is written to stdout; private manifests contain identifiers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
from filelock import FileLock

from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.profiles import ProfileExporter
from nanobot.memory_sync.worker import HindsightWorker
from nanobot.security.network import PinnedDNSAsyncTransport

ROOT = Path('/home/nanobot/.nanobot/sessions/8679b657a17401f5f41c32a247d800ee')
PLAN = ROOT / 'hindsight-history-plan.json'
MANIFEST = ROOT / 'hindsight-history-import.json'
PROBE = ROOT / 'hindsight-history-embedding-report.json'
WORKSPACE = Path('/home/nanobot/.nanobot/workspace')
API = 'http://127.0.0.1:8888'
PREFIX = '/v1/default/banks/personal'


def save(state):
    ProfileExporter.write_atomic(MANIFEST, json.dumps(state, ensure_ascii=False, indent=2))


def queue_plan(outbox: MemoryOutbox):
    state = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else None
    if state and state['stage'] != 'preparing':
        return state
    raw = PLAN.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    probe = json.loads(PROBE.read_text())
    if (not probe.get('passed') or probe.get('plan_sha256') != digest
            or any(case.get('seconds', 0) >= 600 for case in probe.get('embedding_cases', []))):
        raise ValueError('embedding acceptance does not match this plan')
    plan = json.loads(raw)
    if plan['version'] != 1 or plan['bank_id'] != 'personal':
        raise ValueError('unexpected plan destination')
    if outbox.forget_pending():
        raise ValueError('pending forget blocks import')
    if state and state['plan_sha256'] != digest:
        raise ValueError('plan changed during import')
    if not state:
        state = {'version': 1, 'stage': 'preparing', 'plan_sha256': digest,
                 'documents': [], 'source_count': len(plan['sessions']),
                 'skipped': plan['skipped'],
                 'snapshot_prepared_at': datetime.fromtimestamp(PLAN.stat().st_mtime, timezone.utc).isoformat(),
                 'exported_chars': sum(len(m) for s in plan['sessions'] for m in s['messages']),
                 'profile_hashes': {name: hashlib.sha256((WORKSPACE / name).read_bytes()).hexdigest()
                                    for name in ('SOUL.md', 'USER.md')},
                 'source_hashes': {s['source_file']: s['source_sha256'] for s in plan['sessions']}}
        save(state)
    documents = []
    for source in plan['sessions']:
        identity, key = source['identity'], source['key']
        fingerprints, messages = source['fingerprints'], source['messages']
        if len(messages) != len(fingerprints):
            raise ValueError('invalid source alignment')
        outbox.enqueue(identity, key, fingerprints, messages)
        with sqlite3.connect(outbox.path) as db:
            rows = db.execute('SELECT document_id,payload,state FROM jobs WHERE conversation_id=?',
                              (identity,)).fetchall()
        if not any(messages):
            if rows:
                raise ValueError('empty source has unexpected jobs')
            continue
        if len(rows) != 1 or rows[0][2] == 'suppressed':
            raise ValueError('source has conflicting prior jobs')
        payload = json.loads(rows[0][1])
        if payload['items'][0]['content'] != '\n\n'.join(m for m in messages if m):
            raise ValueError('source payload differs')
        documents.append(rows[0][0])
    if len(documents) != len(set(documents)):
        raise ValueError('duplicate identities in import plan')
    state.update(stage='delivering', documents=documents)
    save(state)
    # This temporary sanitized submission plan is no longer needed. The real
    # outbox now owns all delivery payloads; original archives remain untouched.
    PLAN.unlink()
    return state


def job_counts(outbox: MemoryOutbox, documents: list[str]):
    with sqlite3.connect(outbox.path) as db:
        rows = dict(db.execute('SELECT document_id,state FROM jobs'))
    counts = {}
    for identity in documents:
        value = rows.get(identity, 'missing')
        counts[value] = counts.get(value, 0) + 1
    return counts


async def verify(state):
    async with httpx.AsyncClient(transport=PinnedDNSAsyncTransport(allow_loopback=True),
                                 base_url=API, timeout=600, trust_env=False,
                                 follow_redirects=False) as client:
        remote = set()
        offset = 0
        while True:
            response = await client.get(PREFIX + '/documents', params={'limit': 100, 'offset': offset})
            response.raise_for_status()
            listing = response.json()
            rows = listing['items']
            remote.update(item['id'] for item in rows)
            offset += len(rows)
            if offset >= listing['total']:
                break
            if not rows:
                raise ValueError('incomplete document listing')
        expected = set(state['documents'])
        if not expected <= remote:
            raise ValueError('completed delivery missing remote document')
        response = await client.get(PREFIX + '/stats')
        response.raise_for_status()
        stats = response.json()
        active = stats.get('operations_by_status', {})
        if active.get('pending', 0) or active.get('processing', 0):
            return False
        report = {'documents_expected': len(expected), 'documents_present': len(expected & remote),
                  'bank_documents_total': stats['total_documents'],
                  'facts_by_type': stats['nodes_by_fact_type'], 'operations_by_status': active}
        report['local_profiles_unchanged'] = all(
            hashlib.sha256((WORKSPACE / name).read_bytes()).hexdigest() == digest
            for name, digest in state['profile_hashes'].items())
        # Every source gets an index round trip using an actual stored fact as
        # the query. This proves source/index accessibility, not semantic fidelity.
        round_trips = 0
        sources_without_facts = 0
        direct_source_hits = 0
        duplicate_fact_hits = 0
        for identity in state['documents']:
            response = await client.get(PREFIX + '/memories/list',
                                        params={'document_id': identity, 'limit': 100})
            response.raise_for_status()
            rows = response.json()['items']
            rows = [row for row in rows if row.get('fact_type', row.get('type')) in ('world', 'experience')]
            if not rows:
                sources_without_facts += 1
                continue
            response = await client.post(PREFIX + '/memories/recall', json={
                'query': rows[0]['text'], 'budget': 'low', 'max_tokens': 2000,
                'types': ['world', 'experience']})
            response.raise_for_status()
            hits = response.json()['results']
            if any(row.get('document_id') == identity for row in hits):
                direct_source_hits += 1
            elif any(row.get('text', '').strip().casefold() == rows[0]['text'].strip().casefold()
                     for row in hits):
                # Identical facts can legitimately rank a different source first.
                # The listing above already proves this source owns its fact.
                duplicate_fact_hits += 1
            else:
                raise ValueError('source fact not found by recall')
            round_trips += 1
        report.update(index_round_trips=round_trips, direct_source_hits=direct_source_hits,
                      duplicate_fact_hits=duplicate_fact_hits, sources_without_extracted_facts=sources_without_facts,
                      semantic_fidelity_proven=False)
        state.update(stage='complete', verification=report)
        save(state)
        return True


async def main():
    outbox = MemoryOutbox(ROOT / 'hindsight-outbox.sqlite3', bank_id='personal',
                          pause_path=Path('/var/lib/hindsight/control/forget-pending'))
    with FileLock(ROOT / 'hindsight-history-import.lock', timeout=0):
        state = queue_plan(outbox)
        worker = HindsightWorker(outbox, api_url=API, bank_id='personal')
        previous = None
        while state['stage'] != 'complete':
            counts = job_counts(outbox, state['documents'])
            if counts != previous:
                print(json.dumps({'component': 'history_import', 'sources': state['source_count'],
                                  'documents': len(state['documents']), 'jobs': counts}), flush=True)
                previous = counts
                state['job_counts'] = counts
                save(state)
            if any(counts.get(status) for status in ('missing', 'suppressed', 'needs_attention')):
                raise ValueError('import requires attention')
            if counts.get('completed', 0) == len(state['documents']):
                if await verify(state):
                    break
            else:
                await worker.run_once()
            await asyncio.sleep(5)
        print(json.dumps({'component': 'history_import', 'status': 'complete',
                          'verification': state['verification']}), flush=True)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print(json.dumps({'component': 'history_import', 'status': 'needs_attention',
                          'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
