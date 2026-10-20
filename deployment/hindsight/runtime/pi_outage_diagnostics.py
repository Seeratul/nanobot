"""Bounded private host diagnostics; no application messages or model data."""
import datetime
import json
import os
import pathlib
import re
import subprocess
import urllib.request

DIRECTORY = pathlib.Path('/var/log/pi-diagnostics')
LIMIT = 2 * 1024 * 1024


def command(args, timeout=4):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return -1, ''


def kernel_category(message):
    text = message.lower()
    for category, terms in (
        ('oom', ('out of memory', 'oom-kill', 'killed process')),
        ('kernel_fault', ('kernel panic', 'oops:', 'soft lockup', 'hard lockup')),
        ('storage', ('i/o error', 'ext4-fs error', 'uas_eh_abort', 'reset superspeed')),
        ('usb', ('cannot enable', 'unable to enumerate', 'usb disconnect')),
        ('voltage', ('under-voltage', 'undervoltage')),
        ('thermal', ('critical temperature', 'thermal shutdown')),
        ('wifi', ('brcmf', 'deauthenticated', 'disassociated', 'mmc1:')),
    ):
        if any(term in text for term in terms):
            return category
    return 'kernel_warning'


def write_event(directory, event, limit=LIMIT):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / 'outages.jsonl'
    line = (json.dumps(event, sort_keys=True) + '\n').encode()
    if path.exists() and path.stat().st_size + len(line) > limit:
        (directory / 'outages.jsonl.3').unlink(missing_ok=True)
        for index in (2, 1):
            src = directory / ('outages.jsonl.' + str(index))
            if src.exists():
                os.replace(src, directory / ('outages.jsonl.' + str(index + 1)))
        os.replace(path, directory / 'outages.jsonl.1')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'ab') as out:
        out.write(line)
        out.flush()
        os.fsync(out.fileno())
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def snapshot():
    metrics = {}
    for line in pathlib.Path('/proc/meminfo').read_text().splitlines():
        name, value = line.split(':', 1)
        if name in ('MemAvailable', 'MemTotal', 'SwapFree', 'SwapTotal'):
            metrics[name + '_kib'] = int(value.strip().split()[0])
    metrics['load_average'] = [float(x) for x in pathlib.Path('/proc/loadavg').read_text().split()[:3]]
    metrics['temperature_mC'] = int(pathlib.Path('/sys/class/thermal/thermal_zone0/temp').read_text())
    _, throttled = command(['/usr/bin/vcgencmd', 'get_throttled'])
    match = re.fullmatch(r'throttled=(0x[0-9a-fA-F]+)\s*', throttled)
    metrics['throttled'] = int(match[1], 16) if match else None
    metrics['disk_available_bytes'] = os.statvfs('/').f_bavail * os.statvfs('/').f_frsize
    metrics['interfaces'] = {}
    for iface in pathlib.Path('/sys/class/net').iterdir():
        if iface.name not in ('wlan0', 'eth0'):
            continue
        metrics['interfaces'][iface.name] = {'state': (iface / 'operstate').read_text().strip()}
        for name in ('rx_errors', 'tx_errors', 'rx_dropped', 'tx_dropped'):
            metrics['interfaces'][iface.name][name] = int((iface / 'statistics' / name).read_text())
    metrics['network'] = {}
    for label, host in [('router', '192.168.178.1'), ('external_ip', '1.1.1.1')]:
        rc, _ = command(['/usr/bin/ping', '-n', '-c', '1', '-W', '2', host], timeout=3)
        metrics['network'][label + '_ping_ok'] = rc == 0
    for label, host in [('codex', 'chatgpt.com'), ('embeddings', 'openrouter.ai')]:
        rc, _ = command(['/usr/bin/getent', 'ahostsv4', host], timeout=3)
        metrics['network'][label + '_dns_ok'] = rc == 0
    _, output = command(['/usr/bin/systemctl', 'show', 'hindsight-api', 'hindsight-worker',
                          'nanobot-memory-sync', '-p', 'Id', '-p', 'ActiveState', '-p', 'NRestarts'])
    metrics['services'] = []
    for block in output.strip().split('\n\n'):
        fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if fields:
            metrics['services'].append(fields)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    metrics['health'] = {}
    for port in (8888, 8892, 8893):
        try:
            with opener.open('http://127.0.0.1:' + str(port) + '/health', timeout=1) as response:
                metrics['health'][str(port)] = response.status
        except Exception:
            metrics['health'][str(port)] = None
    try:
        with opener.open('http://127.0.0.1:8888/v1/default/banks/personal/stats', timeout=1) as response:
            stats = json.load(response)
        counts = stats.get('operations_by_status', {})
        metrics['memory_operations'] = {key: counts.get(key, 0)
                                         for key in ('pending', 'processing', 'failed', 'completed')}
    except Exception:
        metrics['memory_operations'] = None
    return metrics


def collect_once(directory=DIRECTORY):
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    common = {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'boot': boot, 'uptime_seconds': float(pathlib.Path('/proc/uptime').read_text().split()[0])}
    write_event(directory, {**common, 'kind': 'sample', **snapshot()})
    cursor_path = directory / 'kernel-cursor.json'
    try:
        saved = json.loads(cursor_path.read_text())
    except (OSError, ValueError):
        saved = {}
    args = ['/usr/bin/journalctl', '-k', '-b', '--no-pager', '-o', 'json']
    if saved.get('boot') == boot and isinstance(saved.get('cursor'), str):
        args += ['--after-cursor', saved['cursor']]
    rc, output = command(args, timeout=5)
    if rc:
        write_event(directory, {**common, 'kind': 'journal_read_failed'})
        return
    cursor = saved.get('cursor')
    for line in output.splitlines():
        row = json.loads(line)
        cursor = row.get('__CURSOR', cursor)
        if row.get('_TRANSPORT') != 'kernel':
            continue
        message = row.get('MESSAGE')
        if not isinstance(message, str):
            continue
        category = kernel_category(message)
        if int(row.get('PRIORITY', '7')) > 4 and category == 'kernel_warning':
            continue
        # No application journal is read. Kernel excerpts are bounded and common
        # credential-like values are redacted as an additional precaution.
        message = re.sub(r'(?i)(token|password|secret|api[_-]?key)\s*[:=]\s*\S+', r'\1=[REDACTED]', message)
        write_event(directory, {**common, 'kind': 'kernel', 'category': category,
                               'kernel_monotonic_us': row.get('__MONOTONIC_TIMESTAMP'),
                               'message': message[:800]})
    temp = cursor_path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump({'boot': boot, 'cursor': cursor}, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, cursor_path)


if __name__ == '__main__':
    try:
        collect_once()
    except Exception:
        # Never emit arbitrary exception bodies to another logging system.
        raise SystemExit(1) from None
