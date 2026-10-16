#!/usr/bin/env bash
# Wrapper for deploy_workflow.py. Requires Python 3.10+.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null 2>&1 || { echo "python3 is required (3.10+)"; exit 2; }
exec python3 "$DIR/deploy_workflow.py" "$@"
