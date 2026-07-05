#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
timestamp=$(date +%Y%m%d_%H%M%S)
export RUN_NAME=${RUN_NAME:-"smoke_${timestamp}"}
export RUN_DIR=${RUN_DIR:-"$PROJECT_ROOT/results/smoke/$RUN_NAME"}
export LIMIT=${LIMIT:-5}
export WORKERS=${WORKERS:-5}

bash "$SCRIPT_DIR/run_generate.sh"
touch "$RUN_DIR/done"
