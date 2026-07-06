#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
cd "$PROJECT_ROOT"

timestamp=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR=${OUTPUT_DIR:-"$PROJECT_ROOT/results/issue_rewrite/issue_rewrite_${timestamp}"}
WORKERS=${WORKERS:-10}
MODEL=${MODEL:-deepseek-v3}
TEMPERATURE=${TEMPERATURE:-0.1}
INSTANCES_PATH=${INSTANCES_PATH:-"$PROJECT_ROOT/data/issues/swt276_issues.json"}
CODE_RETRIEVAL_PATH=${CODE_RETRIEVAL_PATH:-"$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json"}
TEST_RETRIEVAL_PATH=${TEST_RETRIEVAL_PATH:-"$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json"}

mkdir -p "$OUTPUT_DIR/logs"
cmd=(
  python -m brt4.run_issue_rewrite
  --instances_path "$INSTANCES_PATH"
  --code_retrieval_path "$CODE_RETRIEVAL_PATH"
  --test_retrieval_path "$TEST_RETRIEVAL_PATH"
  --output_dir "$OUTPUT_DIR"
  --model "$MODEL"
  --temperature "$TEMPERATURE"
  --max_workers "$WORKERS"
)

printf '%q ' "${cmd[@]}" | tee "$OUTPUT_DIR/command.txt"
printf '\n' | tee -a "$OUTPUT_DIR/command.txt"
cd "$PACKAGE_ROOT"
"${cmd[@]}" 2>&1 | tee "$OUTPUT_DIR/logs/issue_rewrite.log"
touch "$OUTPUT_DIR/done"
