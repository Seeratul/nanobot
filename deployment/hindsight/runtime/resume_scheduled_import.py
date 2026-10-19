"""One-shot Pi-local resumption; safe to inspect before the scheduled deadline."""
import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DUE = datetime(2026, 10, 17, 21, 0, 0, tzinfo=ZoneInfo('Europe/Berlin'))
HOLD = Path('/var/lib/hindsight/scheduled-import.hold')
FORGET = Path('/var/lib/hindsight/control/forget-pending')
SERVICES = ('hindsight-api', 'hindsight-worker', 'hindsight-history-import')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    now = datetime.now(DUE.tzinfo)
    due = now >= DUE
    status = {'due': DUE.isoformat(), 'now': now.isoformat(), 'deadline_reached': due,
              'hold_present': HOLD.exists(), 'forget_pending': FORGET.exists()}
    if args.check:
        print(json.dumps(status))
        return
    if not due:
        raise RuntimeError('scheduled time has not arrived')
    if FORGET.exists():
        raise RuntimeError('pending fact retirement blocks resumption')
    if Path('/var/lib/hindsight/import-timeout.hold').exists():
        raise RuntimeError('timeout hold requires explicit review before resumption')
    HOLD.unlink(missing_ok=True)
    directory = os.open(HOLD.parent, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(directory)
    os.close(directory)
    # These units retain their existing dependencies, hardening and forget gate.
    config = json.loads(Path('/var/lib/hindsight/runtime.json').read_text())
    local_embedding = config.get('HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL', '').startswith(
        'http://127.0.0.1:8891/')
    services = (('hindsight-embedding',) if local_embedding else ()) + SERVICES
    for name in services:
        subprocess.run(['systemctl', 'start', name], check=True)
    for name in services:
        pid = subprocess.check_output(['systemctl', 'show', name, '-p', 'MainPID', '--value'], text=True).strip()
        if pid == '0':
            raise RuntimeError('scheduled service did not start')
    print(json.dumps({'status': 'resumed', 'at': datetime.now(DUE.tzinfo).isoformat(),
                      'embedding_location': 'local' if local_embedding else 'remote',
                      'services_started': len(services)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'needs_attention', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
