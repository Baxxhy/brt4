#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
cd "$PROJECT_ROOT"

RUN_DIR=${RUN_DIR:-"${1:-}"}
if [[ -z "$RUN_DIR" ]]; then
  echo "Usage: RUN_DIR=results/runs/<run_name> bash scripts/run_formal_eval.sh" >&2
  exit 2
fi
RUN_DIR=$(realpath -m "$RUN_DIR")
RUN_NAME=$(basename "$RUN_DIR")
BRT4_CONDA_ENV_PREFIX=${BRT4_CONDA_ENV_PREFIX:-"${RUN_NAME}_"}
export BRT4_CONDA_ENV_PREFIX
REPO_ROOT_BASE=${REPO_ROOT_BASE:-"$PACKAGE_ROOT/swe_repos"}
INSTANCES_PATH=${INSTANCES_PATH:-"$PROJECT_ROOT/data/issues/swt276_issues.json"}
WORKERS=${WORKERS:-6}
TIMEOUT=${TIMEOUT:-1800}
EVALUATION_DIR=${EVALUATION_DIR:-"$RUN_DIR/evaluation/formal"}
SUMMARY_PATH=${SUMMARY_PATH:-"$RUN_DIR/evaluation/formal_eval_summary.json"}
FORMAL_LOG_PATH=${FORMAL_LOG_PATH:-"$RUN_DIR/logs/formal_eval.log"}
RESUME=${RESUME:-true}
EVAL_COMPLETED_ONLY=${EVAL_COMPLETED_ONLY:-false}
TMPDIR=${TMPDIR:-"$RUN_DIR/tmp/formal_eval"}
export TMPDIR
USE_GENERATED_WORKTREES=${USE_GENERATED_WORKTREES:-false}
COMPUTE_PATCH_COVERAGE=${COMPUTE_PATCH_COVERAGE:-true}
MISSING_GENERATED_POLICY=${MISSING_GENERATED_POLICY:-count_as_fail}

mkdir -p "$EVALUATION_DIR" "$RUN_DIR/logs" "$TMPDIR"
cmd=(
  python "$PROJECT_ROOT/scripts/run_formal_eval_after_generation.py"
  --outputs_dir "$RUN_DIR/generation"
  --evaluation_dir "$EVALUATION_DIR"
  --summary_path "$SUMMARY_PATH"
  --log_path "$FORMAL_LOG_PATH"
  --dataset_file "$INSTANCES_PATH"
  --repo_root_base "$REPO_ROOT_BASE"
  --max_workers "$WORKERS"
  --timeout "$TIMEOUT"
  --eval_completed_only "$EVAL_COMPLETED_ONLY"
  --compute_patch_coverage "$COMPUTE_PATCH_COVERAGE"
  --missing_generated_policy "$MISSING_GENERATED_POLICY"
)
if [[ "$RESUME" == "true" || "$RESUME" == "1" || "$RESUME" == "yes" ]]; then
  cmd+=(--resume)
fi
if [[ "$USE_GENERATED_WORKTREES" == "true" ]]; then
  cmd+=(--use_generated_worktrees)
fi

printf '%q ' "${cmd[@]}" | tee "$RUN_DIR/logs/formal_eval.command.txt"
printf '\n' | tee -a "$RUN_DIR/logs/formal_eval.command.txt"
"${cmd[@]}" 2>&1 | tee "$RUN_DIR/logs/formal_eval_driver.log"
touch "$RUN_DIR/formal_eval.done"
