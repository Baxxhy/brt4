#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
timestamp=$(date +%Y%m%d_%H%M%S)

RUN_NAME=${RUN_NAME:-"run_p0_selector_v2_${timestamp}"}
RUN_DIR=${RUN_DIR:-"$PROJECT_ROOT/results/runs/$RUN_NAME"}
INSTANCES_PATH=${INSTANCES_PATH:-"$PROJECT_ROOT/data/issues/swt276_issues.json"}
MODEL=${MODEL:-deepseek-v3}
TEMPERATURE=${TEMPERATURE:-0.1}
WORKERS=${WORKERS:-6}
SEED_WORKERS=${SEED_WORKERS:-2}
EVAL_PYTHON=${EVAL_PYTHON:-python}
ENABLE_SELECTOR_V2_POSTHOC=${ENABLE_SELECTOR_V2_POSTHOC:-true}
SELECTOR_V2_FALLBACK_TO_LEGACY=${SELECTOR_V2_FALLBACK_TO_LEGACY:-true}

if [[ -e "$RUN_DIR" ]]; then
  printf 'Refusing to overwrite existing run: %s\n' "$RUN_DIR" >&2
  exit 2
fi
mkdir -p "$RUN_DIR"

# Issue rewriting is fresh; its cache is the only behavior input passed to P0.
OUTPUT_DIR="$RUN_DIR/issue_rewrite" \
INSTANCES_PATH="$INSTANCES_PATH" \
MODEL="$MODEL" \
TEMPERATURE="$TEMPERATURE" \
WORKERS="$WORKERS" \
bash "$SCRIPT_DIR/run_issue_rewrite.sh"

export BRT4_BEHAVIOR_CACHE_DIR="$RUN_DIR/issue_rewrite"
RUN_NAME="$RUN_NAME" \
RUN_DIR="$RUN_DIR" \
INSTANCES_PATH="$INSTANCES_PATH" \
MODEL="$MODEL" \
TEMPERATURE="$TEMPERATURE" \
WORKERS="$WORKERS" \
SEED_WORKERS="$SEED_WORKERS" \
bash "$SCRIPT_DIR/run_generate.sh"

python "$SCRIPT_DIR/export_p0_selections.py" \
  --run_dir "$RUN_DIR" \
  --instances_path "$INSTANCES_PATH" \
  --enable_selector_v2_posthoc "$ENABLE_SELECTOR_V2_POSTHOC" \
  --selector_v2_fallback_to_legacy "$SELECTOR_V2_FALLBACK_TO_LEGACY"

# Both immutable manifests exist before either formal evaluator is invoked.
# EVAL_PYTHON may point to an environment that provides datasets and socksio.
"$EVAL_PYTHON" "$SCRIPT_DIR/run_formal_eval_after_generation.py" \
  --outputs_dir "$RUN_DIR/exports/legacy_selection" \
  --dataset_file "$INSTANCES_PATH" \
  --evaluation_dir "$RUN_DIR/evaluation/formal_legacy" \
  --log_path "$RUN_DIR/logs/formal_legacy.log" \
  --summary_path "$RUN_DIR/evaluation/formal_legacy/summary.json" \
  --eval_completed_only false

"$EVAL_PYTHON" "$SCRIPT_DIR/run_formal_eval_after_generation.py" \
  --outputs_dir "$RUN_DIR/exports/selector_v2_selection" \
  --dataset_file "$INSTANCES_PATH" \
  --evaluation_dir "$RUN_DIR/evaluation/formal_selector_v2" \
  --log_path "$RUN_DIR/logs/formal_selector_v2.log" \
  --summary_path "$RUN_DIR/evaluation/formal_selector_v2/summary.json" \
  --eval_completed_only false
