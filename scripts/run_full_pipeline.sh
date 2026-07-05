#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_ROOT"

timestamp=$(date +%Y%m%d_%H%M%S)
export RUN_NAME=${RUN_NAME:-"run_${timestamp}"}
export RUN_DIR=${RUN_DIR:-"$PROJECT_ROOT/results/runs/$RUN_NAME"}

bash "$SCRIPT_DIR/run_generate.sh"
bash "$SCRIPT_DIR/run_evaluate.sh" "$RUN_DIR"
bash "$SCRIPT_DIR/run_formal_eval.sh" "$RUN_DIR"

mkdir -p "$RUN_DIR/exports" "$RUN_DIR/logs"
cmd=(python "$PROJECT_ROOT/scripts/export_run_outputs.py" --run-dir "$RUN_DIR")
printf '%q ' "${cmd[@]}" | tee "$RUN_DIR/logs/export.command.txt"
printf '\n' | tee -a "$RUN_DIR/logs/export.command.txt"
"${cmd[@]}" 2>&1 | tee "$RUN_DIR/logs/export.log"
touch "$RUN_DIR/export.done" "$RUN_DIR/done"
