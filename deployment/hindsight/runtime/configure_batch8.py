"""Configure batch eight while retaining a bounded shared context pool."""
import json
import os
import re
import subprocess
from pathlib import Path

for service in ('hindsight-history-import', 'hindsight-worker'):
    pid = subprocess.check_output(['systemctl', 'show', service, '-p', 'MainPID', '--value'], text=True).strip()
    if pid != '0':
        raise RuntimeError('stop import and worker before changing batch configuration')

runtime = Path('/var/lib/hindsight/runtime.json')
original = runtime.stat()
config = json.loads(runtime.read_text())
config['HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE'] = '8'
config['HINDSIGHT_API_EMBEDDINGS_MAX_CONCURRENT_REQUESTS'] = '1'
temp = runtime.with_suffix('.batch.tmp')
fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as stream:
    json.dump(config, stream)
    stream.flush()
    os.fsync(stream.fileno())
os.chown(temp, original.st_uid, original.st_gid)
os.replace(temp, runtime)
directory = os.open(runtime.parent, os.O_RDONLY | os.O_DIRECTORY)
os.fsync(directory)
os.close(directory)

unit = Path('/etc/systemd/system/hindsight-embedding.service')
text = unit.read_text()
assert len(re.findall(r'^ExecStart=', text, flags=re.MULTILINE)) == 1
command = ('ExecStart=/opt/hindsight/llama/llama-server '
           '--model /opt/hindsight/models/Qwen3-Embedding-0.6B-Q8_0.gguf '
           '--alias qwen3-embedding-0.6b-q8_0 --host 127.0.0.1 --port 8891 '
           '--embedding --pooling last --threads 2 --threads-batch 2 '
           '--ctx-size 8192 --kv-unified --kv-unified-per-slot 4096 '
           '--batch-size 1024 --ubatch-size 1024 --parallel 8')
text = re.sub(r'^ExecStart=.*$', command, text, flags=re.MULTILINE)
text = re.sub(r'^MemoryMax=.*$', 'MemoryMax=2300M', text, flags=re.MULTILINE)
unit.write_text(text)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'restart', 'hindsight-embedding'], check=True)
print(json.dumps({'hindsight_texts_per_request': 8, 'embedding_http_concurrency': 1,
                  'qwen_parallel_slots': 8, 'shared_context_pool_tokens': 8192,
                  'maximum_context_per_slot': 4096, 'cpu_threads': 2,
                  'qwen_token_batch_size': 1024, 'import_resumed': False}))
