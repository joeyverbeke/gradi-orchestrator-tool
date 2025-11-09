#!/usr/bin/env bash
set -euo pipefail

cd /home/joey/gradi
source orchestrator/.venv/bin/activate

exec python3 orchestrator/orchestrator.py "$@"
