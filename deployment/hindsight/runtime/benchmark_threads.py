"""Bounded ABBA benchmark of the same real fact texts; source text stays in RAM."""
import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path

import httpx

BASE = 'http://127.0.0.1:8891'
UNIT = Path('/etc/systemd/system/hindsight-embedding.service')
OUTPUT = Path('/var/lib/hindsight/benchmarks/thread-comparison.json')
MODEL = 'qwen3-embedding-0.6b-q8_0'


def event(**data):
    print(json.dumps(data), flush=True)


def save(report):
    OUTPUT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, OUTPUT)
    directory = os.open(OUTPUT.parent, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(directory)
    os.close(directory)


def reading(pid):
    mem = {row.split(':', 1)[0]: int(row.split()[1])
           for row in Path('/proc/meminfo').read_text().splitlines()}
    process = {row.split(':', 1)[0]: int(row.split()[1])
               for row in Path(f'/proc/{pid}/status').read_text().splitlines()
               if row.startswith(('VmRSS:', 'VmHWM:'))}
    cpu = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    vm = dict(row.split() for row in Path('/proc/vmstat').read_text().splitlines())
    return {'temperature_c': int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000,
            'available_kib': mem['MemAvailable'], 'rss_kib': process['VmRSS'],
            'peak_rss_kib': process['VmHWM'], 'cpu': cpu,
            'process_ticks': int(fields[11]) + int(fields[12]),
            'swap_in_pages': int(vm['pswpin']), 'swap_out_pages': int(vm['pswpout'])}


def throttle():
    result = subprocess.check_output(['vcgencmd', 'get_throttled'], text=True).strip()
    return int(result.split('=')[1], 16)


async def ready(client):
    for _ in range(120):
        try:
            result = await client.get(BASE + '/health', timeout=2)
            if result.status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(1)
    raise TimeoutError('embedding readiness')


async def embed(client, texts):
    response = await client.post(BASE + '/v1/embeddings', json={'model': MODEL, 'input': texts})
    response.raise_for_status()
    data = response.json()
    rows = sorted(data['data'], key=lambda row: row['index'])
    if [row['index'] for row in rows] != list(range(len(texts))):
        raise ValueError('embedding index mismatch')
    vectors = [row['embedding'] for row in rows]
    if any(len(vector) != 1024 or not all(math.isfinite(x) for x in vector) for vector in vectors):
        raise ValueError('invalid vector')
    return vectors, data['usage']['prompt_tokens']


async def main():
    for service in ('hindsight-history-import', 'hindsight-worker'):
        pid = subprocess.check_output(['systemctl', 'show', service, '-p', 'MainPID', '--value'], text=True).strip()
        if pid != '0':
            raise ValueError('benchmark requires import and worker stopped')
    report = {'status': 'preparing', 'order': [2, 6, 6, 2], 'batch_size': 8,
              'physical_cpu_cores': os.cpu_count(), 'runs': [], 'import_resumed': False,
              'scope': 'local embeddings only; same stored fact texts; no extraction or consolidation'}
    async with httpx.AsyncClient(timeout=600, trust_env=False) as client:
        facts = []
        offset = 0
        while True:
            response = await client.get('http://127.0.0.1:8888/v1/default/banks/personal/memories/list',
                                        params={'limit': 1000, 'offset': offset})
            response.raise_for_status()
            data = response.json()
            facts.extend(row['text'] for row in data['items']
                         if row.get('fact_type') in ('world', 'experience')
                         and row.get('state') != 'invalidated' and 80 <= len(row['text']) <= 600)
            offset += len(data['items'])
            if offset >= data['total']:
                break
            if not data['items']:
                raise ValueError('fact pagination stalled')
        candidates = sorted(set(facts), key=lambda text: (len(text), text))
        if len(candidates) < 32:
            raise ValueError('insufficient eligible real facts')
        texts = [candidates[round(index*(len(candidates)-1)/31)] for index in range(32)]
        expected_tokens = 0
        for text in texts:
            response = await client.post(BASE + '/tokenize', json={'content': text})
            response.raise_for_status()
            expected_tokens += len(response.json()['tokens']) + 1
        report.update(dataset_facts=len(texts), dataset_tokens_per_run=expected_tokens,
                      eligible_facts=len(candidates),
                      private_dataset_sha256=hashlib.sha256(json.dumps(texts).encode()).hexdigest(),
                      minimum_text_chars=min(map(len, texts)), maximum_text_chars=max(map(len, texts)))
        save(report)
        event(stage='prepared', facts=len(texts), tokens_per_run=expected_tokens,
              runs=4, order=report['order'])
        reference = None
        try:
            for number, threads in enumerate(report['order'], 1):
                original = UNIT.read_text()
                updated = re.sub(r'--threads(?:-batch)? \d+', lambda match:
                                 match.group(0).rsplit(' ', 1)[0] + f' {threads}', original)
                UNIT.write_text(updated)
                subprocess.run(['systemctl', 'daemon-reload'], check=True)
                # A fresh server in every run keeps process state and cache treatment symmetric.
                subprocess.run(['systemctl', 'restart', 'hindsight-embedding'], check=True)
                await ready(client)
                slots = await client.get(BASE + '/slots')
                slots.raise_for_status()
                if len(slots.json()) != 8 or any(slot['n_ctx'] != 4096 for slot in slots.json()):
                    raise ValueError('slot configuration changed')
                await embed(client, [f'Synthetic warmup record number {index}: keep answers concise.' for index in range(8)])
                pid = int(subprocess.check_output(['systemctl', 'show', 'hindsight-embedding', '-p', 'MainPID', '--value'], text=True))
                first = reading(pid)
                samples = [first]
                mask_start = throttle()
                async def monitor():
                    while True:
                        await asyncio.sleep(1)
                        samples.append(reading(pid))
                monitor_task = asyncio.create_task(monitor())
                event(stage='run_started', run=number, threads=threads)
                started = time.monotonic()
                vectors = []
                token_usage = 0
                try:
                    for start in range(0, len(texts), 8):
                        result, tokens = await embed(client, texts[start:start+8])
                        vectors.extend(result)
                        token_usage += tokens
                        event(stage='batch_completed', run=number, threads=threads,
                              completed_texts=len(vectors), elapsed_seconds=round(time.monotonic()-started, 2))
                    elapsed = time.monotonic()-started
                finally:
                    monitor_task.cancel()
                    try:
                        await monitor_task
                    except asyncio.CancelledError:
                        pass
                last = reading(pid)
                samples.append(last)
                if token_usage != expected_tokens:
                    raise ValueError('input token usage mismatch')
                if reference is None:
                    reference = vectors
                minimum_cosine = min(sum(a*b for a,b in zip(left,right,strict=True)) /
                                     math.sqrt(sum(x*x for x in left)*sum(x*x for x in right))
                                     for left,right in zip(reference,vectors,strict=True))
                if minimum_cosine < 0.999:
                    raise ValueError('embedding numerical consistency failed')
                delta = [b-a for a,b in zip(first['cpu'],last['cpu'],strict=True)]
                total = sum(delta)
                run = {'run': number, 'threads': threads, 'seconds': round(elapsed,3),
                       'facts_per_second': round(len(texts)/elapsed,4), 'tokens': token_usage,
                       'tokens_per_second': round(token_usage/elapsed,3), 'minimum_cosine': minimum_cosine,
                       'cpu_busy_percent': round(100*(total-delta[3]-delta[4])/total,2),
                       'embedding_cpu_percent': round(100*(last['process_ticks']-first['process_ticks']) /
                                                      os.sysconf('SC_CLK_TCK')/elapsed,2),
                       'maximum_temperature_c': max(sample['temperature_c'] for sample in samples),
                       'minimum_available_mib': round(min(sample['available_kib'] for sample in samples)/1024,1),
                       'embedding_peak_rss_mib': round(max(sample['peak_rss_kib'] for sample in samples)/1024,1),
                       'swap_in_pages': last['swap_in_pages']-first['swap_in_pages'],
                       'swap_out_pages': last['swap_out_pages']-first['swap_out_pages'],
                       'throttle_start': mask_start, 'throttle_end': throttle()}
                report['runs'].append(run)
                report['status'] = 'running'
                save(report)
                event(stage='run_completed', **run)
            totals = {str(threads): sum(run['seconds'] for run in report['runs'] if run['threads']==threads)
                      for threads in (2,6)}
            report.update(status='complete', aggregate_seconds=totals,
                          observed_six_thread_speedup=totals['2']/totals['6'],
                          observed_time_reduction_percent=100*(1-totals['6']/totals['2']))
            save(report)
            event(stage='benchmark_complete', aggregate_seconds=totals,
                  observed_six_thread_speedup=round(totals['2']/totals['6'],3))
        finally:
            subprocess.run(['/opt/hindsight/venv/bin/python', '/opt/hindsight/maintenance/restore_threads6.py'], check=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        event(stage='needs_attention', error_type=type(exc).__name__)
        raise SystemExit(1) from None
