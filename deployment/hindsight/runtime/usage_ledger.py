"""Content-free, durable Hindsight call counters and approximate token totals."""
import argparse
import json
import os
import sqlite3
import urllib.request
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

DATABASE = Path('/var/lib/hindsight/usage/usage.sqlite3')
WORKERS = {'felix-pi': 8892, 'felix-pi-2': 8893}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def connect(path: Path = DATABASE) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Set permissions before SQLite opens the file, including its first creation.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=5)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS metric_instances (
                worker TEXT NOT NULL, instance TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                successful INTEGER NOT NULL, failed INTEGER NOT NULL,
                PRIMARY KEY(worker, instance));
            CREATE TABLE IF NOT EXISTS token_estimates (
                day_utc TEXT NOT NULL, worker TEXT NOT NULL,
                successful_calls INTEGER NOT NULL,
                estimated_input_tokens INTEGER NOT NULL,
                estimated_output_tokens INTEGER NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                PRIMARY KEY(day_utc, worker));
        ''')
        yield db
    finally:
        db.close()


def record_snapshot(worker: str, instance: str, successful: int, failed: int,
                    path: Path = DATABASE) -> None:
    if worker not in WORKERS or min(successful, failed) < 0:
        raise ValueError('invalid usage counters')
    now = utc_now()
    with connect(path) as db, db:
        db.execute('''INSERT INTO metric_instances VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker,instance) DO UPDATE SET last_seen=excluded.last_seen,
            successful=max(successful,excluded.successful), failed=max(failed,excluded.failed)
            ''', (worker, instance, now, now, successful, failed))


def record_estimate(worker: str, input_tokens: int, output_tokens: int,
                    path: Path = DATABASE) -> None:
    if worker not in WORKERS or min(input_tokens, output_tokens) < 0:
        raise ValueError('invalid token estimate')
    now = utc_now()
    with connect(path) as db, db:
        db.execute('''INSERT INTO token_estimates VALUES (?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(day_utc,worker) DO UPDATE SET
            successful_calls=successful_calls+1,
            estimated_input_tokens=estimated_input_tokens+excluded.estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens+excluded.estimated_output_tokens,
            last_seen=excluded.last_seen''',
            (now[:10], worker, input_tokens, output_tokens, now, now))


def process_instance(port: int) -> str:
    """Match the fixed native worker port to its Linux process start identity."""
    matches = []
    for directory in Path('/proc').iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            args = (directory / 'cmdline').read_bytes().split(b'\0')
            index = args.index(b'--http-port')
            if args[index + 1] != str(port).encode():
                continue
            # Fields after the final ')' start at stat field 3; starttime is 22.
            ticks = (directory / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            matches.append(f'{boot}:{directory.name}:{ticks}')
        except (OSError, ValueError, IndexError):
            continue
    if len(matches) != 1:
        raise RuntimeError('worker identity unavailable')
    return matches[0]


def parse_metrics(text: str) -> tuple[int, int]:
    totals: dict[str, int] = defaultdict(int)
    for line in text.splitlines():
        if not line.startswith('hindsight_llm_calls_total{'):
            continue
        labels, raw = line.rsplit(' ', 1)
        if 'model="gpt-6-luna"' not in labels:
            continue
        value = float(raw)
        if value < 0 or not value.is_integer():
            raise ValueError('invalid metric count')
        for status in ('true', 'false'):
            if f'success="{status}"' in labels:
                totals[status] += int(value)
    return totals['true'], totals['false']


def collect(path: Path = DATABASE) -> int:
    errors = 0
    # This maintenance collector contacts only fixed loopback worker endpoints.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for worker, port in WORKERS.items():
        try:
            before = process_instance(port)
            with opener.open(f'http://127.0.0.1:{port}/metrics', timeout=5) as response:
                successful, failed = parse_metrics(response.read(4_000_000).decode())
            if before != process_instance(port):
                raise RuntimeError('worker changed during scrape')
            record_snapshot(worker, before, successful, failed, path)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            # No exception strings, response bodies or arbitrary metric labels persist.
            errors += 1
    return errors


def report(path: Path = DATABASE) -> dict[str, object]:
    with connect(path) as db:
        counters = db.execute('''SELECT coalesce(sum(successful),0),
            coalesce(sum(failed),0),min(first_seen),max(last_seen)
            FROM metric_instances''').fetchone()
        estimates = db.execute('''SELECT coalesce(sum(successful_calls),0),
            coalesce(sum(estimated_input_tokens),0),
            coalesce(sum(estimated_output_tokens),0),min(first_seen),max(last_seen)
            FROM token_estimates''').fetchone()
    return {
        'model': 'gpt-6-luna',
        'observed_successful_calls': counters[0], 'observed_failed_calls': counters[1],
        'counters_first_collected_utc': counters[2], 'counters_last_collected_utc': counters[3],
        'calls_with_token_estimates': estimates[0],
        'estimated_input_tokens': estimates[1] if estimates[0] else None,
        'estimated_output_tokens': estimates[2] if estimates[0] else None,
        'estimates_first_recorded_utc': estimates[3], 'estimates_last_recorded_utc': estimates[4],
        'estimate_method': 'provider character count divided by four; not billed tokens',
        'coverage': 'counter snapshots since tracking began; estimates only after hook activation',
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['collect', 'report'])
    args = parser.parse_args()
    if args.action == 'collect':
        errors = collect()
        print(json.dumps({'worker_scrape_errors': errors}))
        if errors:
            raise SystemExit(1)
    else:
        print(json.dumps(report(), indent=2))


if __name__ == '__main__':
    main()
