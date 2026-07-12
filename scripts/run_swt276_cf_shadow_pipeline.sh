#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
cd "$PROJECT_ROOT"

RUN_NAME=${RUN_NAME:-"run_swt276_counterfactual_shadow_$(date +%Y%m%d_%H%M%S)"}
RUN_DIR=${RUN_DIR:-"$PROJECT_ROOT/results/runs/$RUN_NAME"}
MODEL=${MODEL:-deepseek-v3}
WORKERS=${WORKERS:-3}
SEED_WORKERS=${SEED_WORKERS:-1}
ISSUE_REWRITE_WORKERS=${ISSUE_REWRITE_WORKERS:-3}
FORMAL_WORKERS=${FORMAL_WORKERS:-6}
INSTANCES_PATH=${INSTANCES_PATH:-"$PROJECT_ROOT/data/issues/swt276_issues.json"}
CODE_RETRIEVAL_PATH=${CODE_RETRIEVAL_PATH:-"$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json"}
TEST_RETRIEVAL_PATH=${TEST_RETRIEVAL_PATH:-"$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json"}
REPO_ROOT_BASE=${REPO_ROOT_BASE:-"$PACKAGE_ROOT/swe_repos"}
CONDA_SH=${CONDA_SH:-"/root/conda/ENTER/etc/profile.d/conda.sh"}
CONDA_EXE=${CONDA_EXE:-"/root/conda/ENTER/bin/conda"}
TMPDIR=${TMPDIR:-"/root/Baxxhy/tmp"}
ISSUE_REWRITE_DIR=${ISSUE_REWRITE_DIR:-"$RUN_DIR/issue_rewrite"}
MODEL_CALL_LOG=${MODEL_CALL_LOG:-"$RUN_DIR/logs/model_calls.jsonl"}

mkdir -p "$RUN_DIR"/{logs,evaluation,exports,tmp} "$TMPDIR"
export PYTHONPATH="$PACKAGE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export CONDA_EXE
export BRT3_CONDA_EXE="$CONDA_EXE"
export BRT3_CONDA_SH="$CONDA_SH"
export BRT4_MODEL_CALL_LOG="$MODEL_CALL_LOG"
export TMPDIR
if [[ -f "$CONDA_SH" ]]; then
  # shellcheck disable=SC1090
  source "$CONDA_SH"
fi
export PATH="$(dirname "$CONDA_EXE"):$PATH"

pipeline_log="$RUN_DIR/logs/pipeline.log"
{
  echo "run_name=$RUN_NAME"
  echo "run_dir=$RUN_DIR"
  echo "started_at=$(date '+%Y-%m-%d %H:%M:%S')"
  echo "model=$MODEL"
  echo "workers=$WORKERS"
  echo "seed_workers=$SEED_WORKERS"
  echo "issue_rewrite_workers=$ISSUE_REWRITE_WORKERS"
  echo "formal_workers=$FORMAL_WORKERS"
  echo "instances_path=$INSTANCES_PATH"
  echo "code_retrieval_path=$CODE_RETRIEVAL_PATH"
  echo "test_retrieval_path=$TEST_RETRIEVAL_PATH"
  echo "repo_root_base=$REPO_ROOT_BASE"
  echo "issue_rewrite_dir=$ISSUE_REWRITE_DIR"
  echo "model_call_log=$MODEL_CALL_LOG"
} | tee "$pipeline_log"

OUTPUT_DIR="$ISSUE_REWRITE_DIR" \
WORKERS="$ISSUE_REWRITE_WORKERS" \
MODEL="$MODEL" \
INSTANCES_PATH="$INSTANCES_PATH" \
CODE_RETRIEVAL_PATH="$CODE_RETRIEVAL_PATH" \
TEST_RETRIEVAL_PATH="$TEST_RETRIEVAL_PATH" \
RESUME=true \
bash "$PROJECT_ROOT/scripts/run_issue_rewrite.sh" 2>&1 | tee "$RUN_DIR/logs/issue_rewrite_driver.log"
touch "$RUN_DIR/issue_rewrite.done"

export BRT4_BEHAVIOR_CACHE_DIR="$ISSUE_REWRITE_DIR${BRT4_BEHAVIOR_CACHE_DIR:+:$BRT4_BEHAVIOR_CACHE_DIR}"

RUN_NAME="$RUN_NAME" \
RUN_DIR="$RUN_DIR" \
WORKERS="$WORKERS" \
SEED_WORKERS="$SEED_WORKERS" \
MODEL="$MODEL" \
RESUME=true \
INSTANCES_PATH="$INSTANCES_PATH" \
CODE_RETRIEVAL_PATH="$CODE_RETRIEVAL_PATH" \
TEST_RETRIEVAL_PATH="$TEST_RETRIEVAL_PATH" \
REPO_ROOT_BASE="$REPO_ROOT_BASE" \
COUNTERFACTUAL_SHADOW_MODE=true \
ENABLE_BIDIRECTIONAL_COUNTERFACTUAL_VALIDATION=true \
ENABLE_NEGATIVE_CONTROL=true \
MAX_NEGATIVE_CONTROL_ATTEMPTS=1 \
MAX_NEGATIVE_CONTROL_AST_EDITS=1 \
ENABLE_RUNTIME_TARGET_REACHABILITY=true \
ENABLE_CONTRASTIVE_OBSERVATION_ORACLE=true \
MIN_VALID_SURROGATE_PATCHES_FOR_CONSENSUS=2 \
SURROGATE_CONSENSUS_THRESHOLD=0.67 \
COUNTERFACTUAL_EVIDENCE_MODE=soft \
bash "$PROJECT_ROOT/scripts/run_generate.sh" 2>&1 | tee -a "$pipeline_log"

python "$PROJECT_ROOT/scripts/summarize_model_calls.py" \
  --run_dir "$RUN_DIR" \
  --instances_path "$INSTANCES_PATH" \
  2>&1 | tee "$RUN_DIR/logs/model_call_summary.log"

python "$PROJECT_ROOT/scripts/export_counterfactual_selection.py" \
  --run_dir "$RUN_DIR" \
  --instances_path "$INSTANCES_PATH" \
  --require_complete true \
  --touch_done true \
  2>&1 | tee "$RUN_DIR/logs/export.log"

python "$PROJECT_ROOT/scripts/run_formal_eval_after_generation.py" \
  --outputs_dir "$RUN_DIR/exports/legacy_selection" \
  --dataset_file "$INSTANCES_PATH" \
  --repo_root_base "$REPO_ROOT_BASE" \
  --max_workers "$FORMAL_WORKERS" \
  --eval_completed_only false \
  --timeout 1800 \
  --evaluation_dir "$RUN_DIR/evaluation/formal_legacy_276" \
  --log_path "$RUN_DIR/logs/formal_legacy_276.log" \
  --summary_path "$RUN_DIR/evaluation/formal_legacy_276_summary.json" \
  --compute_patch_coverage true \
  2>&1 | tee "$RUN_DIR/logs/formal_legacy_driver.log"
touch "$RUN_DIR/evaluation_legacy.done"

python "$PROJECT_ROOT/scripts/run_formal_eval_after_generation.py" \
  --outputs_dir "$RUN_DIR/exports/counterfactual_selection" \
  --dataset_file "$INSTANCES_PATH" \
  --repo_root_base "$REPO_ROOT_BASE" \
  --max_workers "$FORMAL_WORKERS" \
  --eval_completed_only false \
  --timeout 1800 \
  --evaluation_dir "$RUN_DIR/evaluation/formal_counterfactual_276" \
  --log_path "$RUN_DIR/logs/formal_counterfactual_276.log" \
  --summary_path "$RUN_DIR/evaluation/formal_counterfactual_276_summary.json" \
  --compute_patch_coverage true \
  2>&1 | tee "$RUN_DIR/logs/formal_counterfactual_driver.log"
touch "$RUN_DIR/evaluation_counterfactual.done"

python "$PROJECT_ROOT/scripts/compare_formal_runs.py" \
  --run_dir "$RUN_DIR" \
  --instances_path "$INSTANCES_PATH" \
  --touch_done \
  2>&1 | tee "$RUN_DIR/logs/comparison.log"

echo "completed_at=$(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$pipeline_log"
