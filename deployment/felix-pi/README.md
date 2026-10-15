# Felix Pi deployment baseline

This fork preserves the Nanobot source running on the Raspberry Pi before the
Hindsight integration. Baseline captured on 2026-10-15.

## Source

- Upstream: https://github.com/HKUDS/nanobot
- Base: tag v0.3.5, commit 1bb712d3488915ca4ed9ccc1a93067ff722f5ab9
- Deployed custom source: a42dfffa1bdc025118201fe3621b2c8120829fac
- Branch: felix/codex-subagents-reactions
- Changes: native subagent model profiles and Discord completion reactions

All 559 tracked package files present in the installed package match the local
source after normalizing CRLF to LF. There were no differing files. The 70
source-only package files are channel tests omitted from the installed wheel.
The installed package additionally contains 438 generated WebUI files and 10
TUI binary files. These generated distribution assets are not copied into Git.

## Runtime snapshot

- Linux ARM64, Python 3.13.5, nanobot-ai 0.3.5 with the custom source above.
- requirements.snapshot.txt records 105 installed dependency versions, excluding
  nanobot-ai itself so a stock PyPI wheel does not replace the fork.
- config.example.json contains only selected model and agent settings from the
  running configuration. It is an overlay, not a complete runnable config.
- The snapshot has a 45 minute idle compaction timeout and Dream enabled every
  two hours. Dream removal and Hindsight are future changes, absent here.
- The currently configured global subagent limit is 22: two sol-high agents and
  twenty luna agents. This differs from the original fresh-install script.

## Recreate the Python environment

Use a fresh environment on Linux ARM64 with Python 3.13. Keep the existing live
environment until a replacement has passed its own checks.

```bash
git clone --branch felix/codex-subagents-reactions https://github.com/Seeratul/nanobot.git
cd nanobot
python3.13 -m venv .venv
.venv/bin/python -m pip install -c deployment/felix-pi/requirements.snapshot.txt .
.venv/bin/python -m pip check
.venv/bin/python -m nanobot --version
```

The version list is a runtime snapshot, not a hash-locked artifact archive. A
fresh installation has not been tested as part of this capture. WebUI/TUI build
artifacts require the upstream build instructions or a separately preserved
distribution artifact; the source comparison does not verify those binaries.

Configure channels, service paths and provider authentication separately, then
merge the example agent settings into the local config. Existing provider
credentials, Discord tokens and OAuth sessions are not supplied by this fork.
Do not replace a working config with the example wholesale.

## Data boundary

The fork contains source, upstream history, the selected config example and a
dependency version snapshot. It contains no Pi environment files, authentication
state, actual user or persona profiles, conversation sessions, memory archive,
private application documents, or machine-specific service units.

Those files require a separate private backup. This repository does not claim
to be a complete backup of the Pi or a restorable image of the running process.

## Development and remotes

On the existing Windows checkout, `origin` continues to refer to HKUDS/nanobot.
The added `fork` remote refers to Seeratul/nanobot and the custom branch tracks
that remote. The fork's default branch is the deployed custom branch; its main
branch remains the upstream snapshot created by GitHub when forking.

Continue custom development from this deployed branch. Upstream upgrades need
an explicit merge/rebase and validation; do not replace the deployed source
with the newest upstream main as part of memory integration.
