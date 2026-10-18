"""Run two native workers inside the existing systemd stop/forget boundary."""
import signal
import subprocess
import time


def run(commands):
    children = []
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        # KillMode=control-group delivers this signal to the children as well.
        # Forwarding it would trigger the native worker's second-signal abort.
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        for command in commands:
            if stop_requested:
                break
            child = subprocess.Popen(command)
            children.append(child)
            if stop_requested and child.poll() is None:
                # Covers a stop arriving while Popen was creating this child.
                child.terminate()
        while not stop_requested:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('native worker exited; restart the shared unit')
            time.sleep(1)
    finally:
        if not stop_requested:
            for child in children:
                if child.poll() is None:
                    child.terminate()
        deadline = time.monotonic() + 38
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main():
    run([
        ['/opt/hindsight/venv/bin/python', '/opt/hindsight/usage_worker.py',
         '--worker-id', identity,
         '--http-host', '127.0.0.1', '--http-port', str(port)]
        for identity, port in [('felix-pi', 8892), ('felix-pi-2', 8893)]
    ])


if __name__ == '__main__':
    main()
