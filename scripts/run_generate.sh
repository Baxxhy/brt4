#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
cd "$PROJECT_ROOT"

timestamp=$(date +%Y%m%d_%H%M%S)
RUN_NAME=${RUN_NAME:-"run_${timestamp}"}
RUN_DIR=${RUN_DIR:-"$PROJECT_ROOT/results/runs/$RUN_NAME"}
WORKERS=${WORKERS:-6}
SEED_WORKERS=${SEED_WORKERS:-$WORKERS}
MODEL=${MODEL:-deepseek-v3}
TEMPERATURE=${TEMPERATURE:-0.1}
TIMEOUT=${TIMEOUT:-1800}
INSTANCES_PATH=${INSTANCES_PATH:-"$PROJECT_ROOT/data/issues/swt276_issues.json"}
CODE_RETRIEVAL_PATH=${CODE_RETRIEVAL_PATH:-"$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json"}
TEST_RETRIEVAL_PATH=${TEST_RETRIEVAL_PATH:-"$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json"}
REPO_ROOT_BASE=${REPO_ROOT_BASE:-"$PACKAGE_ROOT/swe_repos"}
ISSUE_REWRITE_PATH=${ISSUE_REWRITE_PATH:-""}
LIMIT=${LIMIT:-""}

mkdir -p "$RUN_DIR"/{generation,evaluation,exports,logs,tmp}
cat > "$RUN_DIR/run_config.json" <<EOF
{
  "run_name": "$RUN_NAME",
  "created_at": "$(date '+%Y-%m-%d %H:%M:%S')",
  "model": "$MODEL",
  "temperature": $TEMPERATURE,
  "workers": $WORKERS,
  "seed_workers": $SEED_WORKERS,
  "instances_path": "$INSTANCES_PATH",
  "code_retrieval_path": "$CODE_RETRIEVAL_PATH",
  "test_retrieval_path": "$TEST_RETRIEVAL_PATH",
  "repo_root_base": "$REPO_ROOT_BASE",
  "issue_rewrite_path": "$ISSUE_REWRITE_PATH"
}
EOF

cmd=(
  python -m brt4.run
  --instances_path "$INSTANCES_PATH"
  --code_retrieval_path "$CODE_RETRIEVAL_PATH"
  --test_retrieval_path "$TEST_RETRIEVAL_PATH"
  --repo_root_base "$REPO_ROOT_BASE"
  --output_dir "$RUN_DIR/generation"
  --model "$MODEL"
  --temperature "$TEMPERATURE"
  --max_workers "$WORKERS"
  --timeout "$TIMEOUT"
  --num_candidates 1
)
if [[ -n "$LIMIT" ]]; then
  cmd+=(--limit "$LIMIT")
fi

printf '%q ' "${cmd[@]}" | tee "$RUN_DIR/command.txt"
printf '\n' | tee -a "$RUN_DIR/command.txt"
printf '%q ' "${cmd[@]}" > "$RUN_DIR/logs/generation.command.txt"
printf '\n' >> "$RUN_DIR/logs/generation.command.txt"
cd "$PACKAGE_ROOT"
"${cmd[@]}" 2>&1 | tee "$RUN_DIR/logs/generation.log"
touch "$RUN_DIR/generation.done"
