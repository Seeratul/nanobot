"""Install the validated ANN planner fix while retaining the real timeout hold."""
import importlib.metadata
import json
import os
import subprocess
from pathlib import Path

from switch_openrouter import atomic

DATA = Path('/var/lib/hindsight')
OPT = Path('/opt/hindsight')
REPORTS = Path('/var/tmp/hindsight-ann-replay-20261019')


def main():
    if os.geteuid() != 0:
        raise RuntimeError('root required')
    for unit in ['hindsight-worker', 'hindsight-history-import']:
        status = subprocess.check_output(
            ['systemctl', 'show', unit, '-p', 'ActiveState', '--value'], text=True).strip()
        if status not in ['inactive', 'failed']:
            raise RuntimeError('stop importer and workers before installation')
    if (DATA / 'control/forget-pending').exists():
        raise RuntimeError('fact retirement blocks installation')
    hold = json.loads((DATA / 'import-timeout.hold').read_text())
    if hold.get('phase') != 'ann_timeout' or hold.get('seeds') != 910:
        raise RuntimeError('timeout evidence differs from validated workload')
    if importlib.metadata.version('hindsight-api-slim') != '1.0.0':
        raise RuntimeError('unsupported native runtime')
    report = json.loads((REPORTS / 'patched-report.json').read_text())
    if (report.get('result') != 'success' or report.get('seeds') != 910
            or report.get('search_settings_changed') is not False
            or report.get('planner_setting_restored') is not True
            or report.get('seconds', 120) >= 120):
        raise RuntimeError('full patched replay has not passed')
    source = Path(__file__).parent
    content = (source / 'ann_timeout_stop.py').read_text()
    if 'SET LOCAL enable_sort=off' not in content or 'asyncio.timeout(limit)' not in content:
        raise RuntimeError('unexpected adapter source')
    for destination in [OPT / 'ann_timeout_stop.py', OPT / 'maintenance/ann_timeout_stop.py']:
        atomic(destination, content, mode=0o644)
    atomic(OPT / 'maintenance/test_ann_timeout_stop.py',
           (source / 'test_ann_timeout_stop.py').read_text(), mode=0o644)
    print(json.dumps({'installed': True, 'timeout_hold_preserved': True,
                      'full_native_arrays': True, 'ann_limit_seconds': 120,
                      'planner_preference': 'transaction_local', 'services_started': False}))


if __name__ == '__main__':
    main()
