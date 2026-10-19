"""Root-owned fixed action: a timeout hold stops import and both shared workers."""
import subprocess
from pathlib import Path

HOLD = Path('/var/lib/hindsight/import-timeout.hold')


def main():
    if HOLD.exists():
        for unit in ['hindsight-history-import.service', 'hindsight-worker.service']:
            subprocess.run(['/usr/bin/systemctl', 'stop', unit], check=True)


if __name__ == '__main__':
    main()
