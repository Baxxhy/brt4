#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
cd "$PROJECT_ROOT"

DATASET=${DATASET:-swtlite}
timestamp=$(date +%Y%m%d_%H%M%S)
MODEL=${MODEL:-deepseek-v3}
WORKERS=${WORKERS:-4}
SEED_WORKERS=${SEED_WORKERS:-1}
ISSUE_REWRITE_WORKERS=${ISSUE_REWRITE_WORKERS:-4}
FORMAL_WORKERS=${FORMAL_WORKERS:-8}
REPO_ROOT_BASE=${REPO_ROOT_BASE:-"$PACKAGE_ROOT/swe_repos"}
CONDA_SH=${CONDA_SH:-"/root/conda/ENTER/etc/profile.d/conda.sh"}
CONDA_EXE=${CONDA_EXE:-"/root/conda/ENTER/bin/conda"}
TMPDIR=${TMPDIR:-"/root/Baxxhy/tmp"}
LIMIT=${LIMIT:-}

case "$DATASET" in
  tdd)
    DATASET_TOTAL=449
    RUN_NAME=${RUN_NAME:-"run_tdd449_ats_brt_${timestamp}"}
    RUN_DIR=${RUN_DIR:-"$PROJECT_ROOT/results/runs/tdd/$RUN_NAME"}
    INSTANCES_PATH=${INSTANCES_PATH:-"$PROJECT_ROOT/data/issues/tdd/tdd_issues.json"}
    CODE_RETRIEVAL_PATH=${CODE_RETRIEVAL_PATH:-"$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_deepseek.json"}
    TEST_RETRIEVAL_PATH=${TEST_RETRIEVAL_PATH:-"$PROJECT_ROOT/retrieval_results/test/icore/deepseek/related_tests.json"}
    ;;
  swt|swtlite)
    DATASET_TOTAL=276
    RUN_NAME=${RUN_NAME:-"run_swtlite276_ats_brt_${timestamp}"}
    RUN_DIR=${RUN_DIR:-"$PROJECT_ROOT/results/runs/swtlite/$RUN_NAME"}
    INSTANCES_PATH=${INSTANCES_PATH:-"$PROJECT_ROOT/data/issues/swt276_issues.json"}
    CODE_RETRIEVAL_PATH=${CODE_RETRIEVAL_PATH:-"$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json"}
    TEST_RETRIEVAL_PATH=${TEST_RETRIEVAL_PATH:-"$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json"}
    ;;
  *)
    echo "DATASET must be tdd, swt, or swtlite" >&2
    exit 2
    ;;
esac

if [[ -e "$RUN_DIR" ]] && find "$RUN_DIR" -mindepth 1 -print -quit | grep -q .; then
  echo "refusing to overwrite non-empty run directory: $RUN_DIR" >&2
  exit 2
fi

mkdir -p "$RUN_DIR"/{logs,evaluation,exports,tmp} "$TMPDIR"
export PYTHONPATH="$PACKAGE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export CONDA_EXE
export BRT3_CONDA_EXE="$CONDA_EXE"
export BRT3_CONDA_SH="$CONDA_SH"
export TMPDIR
export BRT4_MODEL_CALL_LOG="$RUN_DIR/logs/model_calls.jsonl"
if [[ -f "$CONDA_SH" ]]; then
  # shellcheck disable=SC1090
  source "$CONDA_SH"
fi
export PATH="$(dirname "$CONDA_EXE"):$PATH"

pipeline_log="$RUN_DIR/logs/pipeline.log"
{
  echo "method=ATS-BRT: Behavior-Grounded Adaptive Typed Search for Bug Reproduction Tests"
  echo "dataset=$DATASET"
  echo "dataset_total=$DATASET_TOTAL"
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
  echo "golden_allowed_during_generation=false"
} | tee "$pipeline_log"

ISSUE_REWRITE_DIR="$RUN_DIR/issue_rewrite"
OUTPUT_DIR="$ISSUE_REWRITE_DIR" \
WORKERS="$ISSUE_REWRITE_WORKERS" \
MODEL="$MODEL" \
INSTANCES_PATH="$INSTANCES_PATH" \
CODE_RETRIEVAL_PATH="$CODE_RETRIEVAL_PATH" \
TEST_RETRIEVAL_PATH="$TEST_RETRIEVAL_PATH" \
LIMIT="$LIMIT" \
RESUME=false \
bash "$PROJECT_ROOT/scripts/run_issue_rewrite.sh" 2>&1 | tee "$RUN_DIR/logs/issue_rewrite_driver.log"
touch "$RUN_DIR/issue_rewrite.done"

export BRT4_BEHAVIOR_CACHE_DIR="$ISSUE_REWRITE_DIR"

run_generation_pass() {
  local pass_name=$1
  local resume=$2
  echo "generation_pass=$pass_name started_at=$(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$pipeline_log"
  RUN_NAME="$RUN_NAME" \
  RUN_DIR="$RUN_DIR" \
  WORKERS="$WORKERS" \
  SEED_WORKERS="$SEED_WORKERS" \
  MODEL="$MODEL" \
  LIMIT="$LIMIT" \
  RESUME="$resume" \
  INSTANCES_PATH="$INSTANCES_PATH" \
  CODE_RETRIEVAL_PATH="$CODE_RETRIEVAL_PATH" \
  TEST_RETRIEVAL_PATH="$TEST_RETRIEVAL_PATH" \
  REPO_ROOT_BASE="$REPO_ROOT_BASE" \
  ISSUE_REWRITE_PATH="$ISSUE_REWRITE_DIR" \
  COUNTERFACTUAL_SHADOW_MODE=true \
  ENABLE_BIDIRECTIONAL_COUNTERFACTUAL_VALIDATION=false \
  ENABLE_NEGATIVE_CONTROL=false \
  ENABLE_CONTRASTIVE_OBSERVATION_ORACLE=false \
  ENABLE_COUNTERFACTUAL_REPAIR_BRANCH=false \
  ENABLE_RUNTIME_TARGET_REACHABILITY=true \
  ENABLE_ADAPTIVE_TYPED_SEARCH=true \
  ENABLE_STRUCTURED_OBSERVATION_EXTRACTOR=true \
  ENABLE_MINIMAL_ORACLE_SEARCH=true \
  ENABLE_TRIGGER_SEARCH=true \
  ENABLE_DUPLICATE_AWARE_ARCHIVE=true \
  ENABLE_OPTIONAL_RECOMPOSITION=true \
  ENABLE_SELECTOR_V2=true \
  MAX_EXTRA_UNIQUE_CANDIDATES=3 \
  MAX_TRIGGER_SEARCH_CANDIDATES=2 \
  MAX_MINIMAL_ORACLE_CANDIDATES=2 \
  MAX_PROTOCOL_REPAIR_CANDIDATES=1 \
  MAX_RECOMPOSITION_CANDIDATES=1 \
  bash "$PROJECT_ROOT/scripts/run_generate.sh" 2>&1 | tee -a "$pipeline_log"
  echo "generation_pass=$pass_name finished_at=$(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$pipeline_log"
}

run_generation_pass initial false

completeness_args=(
  --instances_path "$INSTANCES_PATH"
  --generation_dir "$RUN_DIR/generation"
  --summary_path "$RUN_DIR/generation_completeness_initial.json"
  --fail_incomplete true
)
if [[ -n "$LIMIT" ]]; then
  completeness_args+=(--limit "$LIMIT")
fi
if ! python "$PROJECT_ROOT/scripts/check_generation_completeness.py" "${completeness_args[@]}" 2>&1 | tee "$RUN_DIR/logs/generation_completeness_initial.log"; then
  echo "generation incomplete; retrying missing/error instances once" | tee -a "$pipeline_log"
  run_generation_pass retry_1 true
fi

final_completeness_args=(
  --instances_path "$INSTANCES_PATH"
  --generation_dir "$RUN_DIR/generation"
  --summary_path "$RUN_DIR/generation_completeness_final.json"
  --fail_incomplete false
)
if [[ -n "$LIMIT" ]]; then
  final_completeness_args+=(--limit "$LIMIT")
fi
python "$PROJECT_ROOT/scripts/check_generation_completeness.py" \
  "${final_completeness_args[@]}" \
  2>&1 | tee "$RUN_DIR/logs/generation_completeness_final.log"

python "$PROJECT_ROOT/scripts/summarize_model_calls.py" \
  --run_dir "$RUN_DIR" \
  --instances_path "$INSTANCES_PATH" \
  2>&1 | tee "$RUN_DIR/logs/model_call_summary.log"

python "$PROJECT_ROOT/scripts/export_ats_selection.py" \
  --run_dir "$RUN_DIR" \
  --instances_path "$INSTANCES_PATH" \
  2>&1 | tee "$RUN_DIR/logs/export_selector_v2.log"
touch "$RUN_DIR/selection_frozen.done"

python "$PROJECT_ROOT/scripts/run_formal_eval_after_generation.py" \
  --outputs_dir "$RUN_DIR/exports/selector_v2_selection" \
  --dataset_file "$INSTANCES_PATH" \
  --repo_root_base "$REPO_ROOT_BASE" \
  --max_workers "$FORMAL_WORKERS" \
  --eval_completed_only false \
  --timeout 1800 \
  --evaluation_dir "$RUN_DIR/evaluation/formal_selector_v2" \
  --log_path "$RUN_DIR/logs/formal_selector_v2.log" \
  --summary_path "$RUN_DIR/evaluation/formal_selector_v2_summary.json" \
  --compute_patch_coverage true \
  --missing_generated_policy count_as_fail \
  2>&1 | tee "$RUN_DIR/logs/formal_selector_v2_driver.log"
touch "$RUN_DIR/formal_eval.done"
echo "completed_at=$(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$pipeline_log"
