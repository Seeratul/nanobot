"""Finish and harden the separate installation after initial readiness delay."""
import json
from pathlib import Path
import subprocess

path = Path('/var/lib/hindsight/runtime.json')
env = json.loads(path.read_text())
env['HINDSIGHT_API_LLM_TRACE_ENABLED'] = 'false'
env['HINDSIGHT_API_ENABLE_BANK_CONFIG_API'] = 'false'
path.write_text(json.dumps(env))
path.chmod(0o600)
subprocess.run(['systemctl', 'restart', 'hindsight-api', 'hindsight-worker'], check=True)
Path('/opt/hindsight/installed.json').write_text(json.dumps({
    'hindsight': '1.0.0', 'api_worker_enabled': False,
    'model_sha256': '06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439',
}))
print(json.dumps({'installed': True, 'api_worker': False, 'llm_traces': False}))
