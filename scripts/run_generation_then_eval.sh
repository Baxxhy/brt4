#!/usr/bin/env bash
set -euo pipefail

# Compatibility entry point for the complete two-stage pipeline.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec bash "$SCRIPT_DIR/run_latest_full_pipeline.sh" "$@"
