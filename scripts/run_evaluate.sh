#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
cd "$PROJECT_ROOT"

RUN_DIR=${RUN_DIR:-"${1:-}"}
if [[ -z "$RUN_DIR" ]]; then
  echo "Usage: RUN_DIR=results/runs/<run_name> bash scripts/run_evaluate.sh" >&2
  exit 2
fi
RUN_DIR=$(realpath -m "$RUN_DIR")
REPO_ROOT_BASE=${REPO_ROOT_BASE:-"$PACKAGE_ROOT/swe_repos"}
INSTANCES_PATH=${INSTANCES_PATH:-"$PROJECT_ROOT/data/issues/swt276_issues.json"}
WORKERS=${WORKERS:-6}
TIMEOUT=${TIMEOUT:-1800}

mkdir -p "$RUN_DIR/evaluation/direct_eval" "$RUN_DIR/logs"
cmd=(
  python -m brt4.direct_eval
  --instances_path "$INSTANCES_PATH"
  --generated_dir "$RUN_DIR/generation"
  --repo_root_base "$REPO_ROOT_BASE"
  --output_dir "$RUN_DIR/evaluation/direct_eval"
  --max_workers "$WORKERS"
  --timeout "$TIMEOUT"
  --resume
)

printf '%q ' "${cmd[@]}" | tee "$RUN_DIR/logs/evaluation.command.txt"
printf '\n' | tee -a "$RUN_DIR/logs/evaluation.command.txt"
cd "$PACKAGE_ROOT"
"${cmd[@]}" 2>&1 | tee "$RUN_DIR/logs/evaluation.log"
touch "$RUN_DIR/evaluation.done"
