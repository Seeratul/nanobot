"""A fixed marker controls only the dedicated worker; clients retain NNP."""
from pathlib import Path
import subprocess

root = Path('/opt/hindsight')
(root / 'worker_gate.py').write_text('''from pathlib import Path
import subprocess
paused = Path('/var/lib/hindsight/control/forget-pending').exists()
subprocess.run(['/usr/bin/systemctl', 'stop' if paused else 'start',
                'hindsight-worker.service'], check=True)
''')
(root / 'worker_gate.py').chmod(0o644)
units = Path('/etc/systemd/system')
(units / 'hindsight-worker-gate.service').write_text('''[Unit]
Description=Apply the fixed Hindsight worker pause signal
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/hindsight/worker_gate.py
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
StandardOutput=null
StandardError=null
[Install]
WantedBy=multi-user.target
''')
(units / 'hindsight-worker-gate.path').write_text('''[Unit]
Description=Watch the Hindsight worker pause signal
[Path]
PathChanged=/var/lib/hindsight/control
Unit=hindsight-worker-gate.service
[Install]
WantedBy=multi-user.target
''')
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', '--now', 'hindsight-worker-gate.path',
                'hindsight-worker-gate.service'], check=True)
print('fixed-worker gate installed; NoNewPrivileges remains enabled')
