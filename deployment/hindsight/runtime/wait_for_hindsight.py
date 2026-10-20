"""Fail startup cleanly until the local memory API can read its database."""
import time
import urllib.request


def wait_ready(timeout=180, opener=None, clock=time.monotonic, sleep=time.sleep):
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            with opener('http://127.0.0.1:8888/health', timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        sleep(2)
    return False


if __name__ == '__main__':
    raise SystemExit(0 if wait_ready() else 1)
