"""Set the authorized large-import wall limit while workers/import are stopped."""
import json
import os
import subprocess
from pathlib import Path

from install_parallel_workers import atomic

RUNTIME = Path('/var/lib/hindsight/runtime.json')
FIELD = 'HINDSIGHT_API_RETAIN_WALL_TIMEOUT'


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit('root required')
    for name in ['hindsight-history-import', 'hindsight-worker']:
        state = subprocess.check_output(
            ['systemctl', 'show', name, '--property=ActiveState', '--value'], text=True,
        ).strip()
        if state not in {'inactive', 'failed'}:
            raise SystemExit('stop importer and workers before configuring their limit')
    if Path('/var/lib/hindsight/control/forget-pending').exists():
        raise SystemExit('pending forget blocks resumption')
    original = json.loads(RUNTIME.read_text())
    if original.get(FIELD) not in {None, '3600', '86400'}:
        raise SystemExit('unexpected custom retain limit')
    configured = {**original, FIELD: '86400'}
    metadata = RUNTIME.stat()
    atomic(RUNTIME, json.dumps(configured), metadata.st_mode & 0o777,
           metadata.st_uid, metadata.st_gid)
    actual = json.loads(RUNTIME.read_text())
    if actual != configured or {k: v for k, v in actual.items() if k != FIELD} != {
        k: v for k, v in original.items() if k != FIELD
    }:
        raise RuntimeError('runtime verification failed')
    print(json.dumps({'retain_wall_timeout_seconds': 86400, 'started': False}))


if __name__ == '__main__':
    main()
