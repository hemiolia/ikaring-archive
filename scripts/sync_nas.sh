#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-/opt/homebrew/opt/python@3.14/bin/python3.14}"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(which python3)"
fi

exec "$PYTHON" "$SCRIPT_DIR/sync_nas.py" "$@"
