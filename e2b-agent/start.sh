#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Starting e2b-agent..."
cd "$SCRIPT_DIR"
source /opt/conda/bin/activate
pip install -q -r requirements.txt 2>/dev/null
python agent.py "$@"
