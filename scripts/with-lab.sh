#!/usr/bin/env bash
# Map the Infisical lab key onto the dev target the care-kit tools expect, then run the command.
# Usage: infisical run --projectId <id> --env dev --silent -- ./scripts/with-lab.sh ./scripts/deploy.sh <file> dev
set -euo pipefail
: "${N8N_LOCAL_WORKFLOW_LAB_API_KEY:?N8N_LOCAL_WORKFLOW_LAB_API_KEY not set; run under infisical run --env dev}"
export N8N_API_URL_DEV="${N8N_LAB_URL:-http://localhost:5678}"
export N8N_API_KEY_DEV="$N8N_LOCAL_WORKFLOW_LAB_API_KEY"
export N8N_LAYOUT_CHECK="${N8N_LAYOUT_CHECK:-$HOME/.claude/skills/n8n-canvas-docs/scripts/layout_check.py}"
exec "$@"
