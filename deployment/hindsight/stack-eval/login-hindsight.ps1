# Run in your own PowerShell terminal. Device-code login stays in that terminal.
$ErrorActionPreference = 'Stop'
$taskKey = Join-Path $env:USERPROFILE '.ssh/id_ed25519_nanobot_pi'
if (-not (Test-Path -LiteralPath $taskKey)) {
    throw "Pi SSH key not found: $taskKey"
}
& ssh -t -o BatchMode=yes -i $taskKey bot@192.168.178.31 'sudo -u hindsight env CODEX_HOME=/var/lib/hindsight/codex /usr/bin/codex login --device-auth'
if ($LASTEXITCODE -ne 0) {
    throw 'Hindsight Codex login did not finish successfully.'
}
& ssh -o BatchMode=yes -i $taskKey bot@192.168.178.31 'sudo -u hindsight env CODEX_HOME=/var/lib/hindsight/codex /usr/bin/codex login status'
if ($LASTEXITCODE -ne 0) {
    throw 'Could not verify the Hindsight Codex login.'
}
