"""Wait for the isolated build, move tmpfs artifacts to SSD, then benchmark."""
import pathlib
import shutil
import subprocess
import sys
import time

root = pathlib.Path('/tmp/nanobot-qwen3-eval')
destination = pathlib.Path('/var/tmp/nanobot-qwen3-eval')
deadline = time.monotonic() + 1800
while 'Built target llama-server' not in (root / 'build.log').read_text():
    if time.monotonic() > deadline:
        raise TimeoutError('llama-server build did not finish')
    time.sleep(15)
if not root.is_symlink():
    if root.resolve() != pathlib.Path('/tmp/nanobot-qwen3-eval') or destination.exists():
        raise RuntimeError('Unexpected artifact paths; refusing move')
    shutil.move(str(root), str(destination))
    root.symlink_to(destination, target_is_directory=True)
print('Build complete; artifacts now on SSD', flush=True)
for threads in (2, 4):
    command = [sys.executable, '/tmp/nanobot-qwen3-benchmark.py', '--threads', str(threads)]
    if threads == 4:
        command.append('--reuse-documents')
    subprocess.run(command, check=True)
