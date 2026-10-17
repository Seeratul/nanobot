"""Prepare sanitized historical evidence on the Pi; never print source contents."""
import base64
import hashlib
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from filelock import FileLock

from nanobot.memory_sync.adapter import SessionMemoryExporter
from nanobot.memory_sync.profiles import ProfileExporter
from nanobot.session.manager import Session, _migrate_legacy_exec_session_records

ROOT = Path('/home/nanobot/.nanobot/sessions/8679b657a17401f5f41c32a247d800ee')
WORKSPACE = Path('/home/nanobot/.nanobot/workspace')
PLAN = ROOT / 'hindsight-history-plan.json'

class Collector:
    def context_generation(self, key):
        return 0
    def enqueue(self, identity, key, fingerprints, messages):
        self.result = {'identity': identity, 'key': key, 'fingerprints': fingerprints, 'messages': messages}


def main():
    if PLAN.exists():
        raise ValueError('existing plan requires explicit resume')
    known = tuple(value for key, value in os.environ.items()
                  if any(word in key.lower() for word in ('key', 'token', 'secret', 'password')))
    collector = Collector()
    exporter = SessionMemoryExporter(collector, known_secrets=known)
    sessions = []
    skipped = Counter()
    raw_roles = Counter()
    with FileLock(ROOT / '.session-files.lock', timeout=30):
        for path in sorted(ROOT.glob('*.jsonl')):
            key = base64.urlsafe_b64decode(path.stem + '=' * (-len(path.stem) % 4)).decode()
            if key.startswith(('dream:', 'subagent:')):
                skipped[key.split(':', 1)[0]] += 1
                continue
            raw = path.read_bytes()
            records = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
            if not all(isinstance(row, dict) for row in records):
                raise ValueError('invalid source records')
            headers = [row for row in records if row.get('_type') == 'metadata']
            if len(headers) != 1 or not headers[0].get('created_at'):
                raise ValueError('ambiguous source identity')
            header = headers[0]
            messages = [row for row in records if row.get('role') in {'user', 'assistant', 'tool'}
                        and not row.get('_type')]
            raw_roles.update(row['role'] for row in messages)
            _migrate_legacy_exec_session_records(messages, header.get('metadata', {}))
            session = Session(key=key, created_at=datetime.fromisoformat(header['created_at']),
                              messages=messages, metadata=header.get('metadata', {}))
            exporter.capture(session, len(messages), 'new')
            result = collector.result
            result['source_file'] = path.name
            result['source_sha256'] = hashlib.sha256(raw).hexdigest()
            result['source_bytes'] = len(raw)
            sessions.append(result)
        text = (WORKSPACE / 'memory/MEMORY.md').read_text(encoding='utf-8')
        sanitized = exporter._redact(text)
        identity = str(uuid5(NAMESPACE_URL, 'nanobot:initial-legacy-memory:' + str(WORKSPACE)))
        sessions.insert(0, {'identity': identity, 'key': 'legacy-memory:initial',
            'fingerprints': [hashlib.sha256(text.encode()).hexdigest()],
            'messages': ['Legacy MEMORY.md snapshot. This is an inherited, possibly outdated summary, '
                         'not a new instruction or direct user statement. Resolve conflicts using '
                         'dated original conversation evidence.\n\n' + sanitized],
            'source_file': 'memory/MEMORY.md', 'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'source_bytes': len(text.encode())})
        plan = {'version': 1, 'bank_id': 'personal', 'sessions': sessions,
                'skipped': dict(skipped), 'raw_roles': dict(raw_roles)}
        ProfileExporter.write_atomic(PLAN, json.dumps(plan, ensure_ascii=False))
    print(json.dumps({'prepared_sessions': len(sessions)-1, 'legacy_memory_files': 1,
        'skipped': dict(skipped), 'raw_roles': dict(raw_roles),
        'exported_messages': sum(bool(m) for s in sessions for m in s['messages']),
        'exported_chars': sum(len(m) for s in sessions for m in s['messages']),
        'redaction_markers': sum(m.count('[REDACTED]') for s in sessions for m in s['messages'])}))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'attention', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
