#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
cd "$PROJECT_ROOT"

timestamp=$(date +%Y%m%d_%H%M%S)
RUN_NAME=${RUN_NAME:-"run_${timestamp}"}
RUN_DIR=${RUN_DIR:-"$PROJECT_ROOT/results/runs/$RUN_NAME"}
BRT4_CONDA_ENV_PREFIX=${BRT4_CONDA_ENV_PREFIX:-"${RUN_NAME}_"}
export BRT4_CONDA_ENV_PREFIX
TMPDIR=${TMPDIR:-"$RUN_DIR/tmp/generation"}
export TMPDIR
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
RESUME=${RESUME:-false}
COUNTERFACTUAL_SHADOW_MODE=${COUNTERFACTUAL_SHADOW_MODE:-true}
ENABLE_BIDIRECTIONAL_COUNTERFACTUAL_VALIDATION=${ENABLE_BIDIRECTIONAL_COUNTERFACTUAL_VALIDATION:-true}
ENABLE_NEGATIVE_CONTROL=${ENABLE_NEGATIVE_CONTROL:-true}
MAX_NEGATIVE_CONTROL_ATTEMPTS=${MAX_NEGATIVE_CONTROL_ATTEMPTS:-1}
MAX_NEGATIVE_CONTROL_AST_EDITS=${MAX_NEGATIVE_CONTROL_AST_EDITS:-1}
ENABLE_RUNTIME_TARGET_REACHABILITY=${ENABLE_RUNTIME_TARGET_REACHABILITY:-true}
ENABLE_CONTRASTIVE_OBSERVATION_ORACLE=${ENABLE_CONTRASTIVE_OBSERVATION_ORACLE:-true}
MIN_VALID_SURROGATE_PATCHES_FOR_CONSENSUS=${MIN_VALID_SURROGATE_PATCHES_FOR_CONSENSUS:-2}
SURROGATE_CONSENSUS_THRESHOLD=${SURROGATE_CONSENSUS_THRESHOLD:-0.67}
COUNTERFACTUAL_EVIDENCE_MODE=${COUNTERFACTUAL_EVIDENCE_MODE:-soft}
MODEL_CALL_LOG=${MODEL_CALL_LOG:-"$RUN_DIR/logs/model_calls.jsonl"}
export BRT4_MODEL_CALL_LOG="$MODEL_CALL_LOG"

mkdir -p "$RUN_DIR"/{generation,evaluation,exports,logs,tmp} "$TMPDIR"
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
  "issue_rewrite_path": "$ISSUE_REWRITE_PATH",
  "conda_env_prefix": "$BRT4_CONDA_ENV_PREFIX",
  "tmpdir": "$TMPDIR",
  "counterfactual_shadow_mode": "$COUNTERFACTUAL_SHADOW_MODE",
  "enable_bidirectional_counterfactual_validation": "$ENABLE_BIDIRECTIONAL_COUNTERFACTUAL_VALIDATION",
  "enable_negative_control": "$ENABLE_NEGATIVE_CONTROL",
  "max_negative_control_attempts": $MAX_NEGATIVE_CONTROL_ATTEMPTS,
  "max_negative_control_ast_edits": $MAX_NEGATIVE_CONTROL_AST_EDITS,
  "enable_runtime_target_reachability": "$ENABLE_RUNTIME_TARGET_REACHABILITY",
  "enable_contrastive_observation_oracle": "$ENABLE_CONTRASTIVE_OBSERVATION_ORACLE",
  "min_valid_surrogate_patches_for_consensus": $MIN_VALID_SURROGATE_PATCHES_FOR_CONSENSUS,
  "surrogate_consensus_threshold": $SURROGATE_CONSENSUS_THRESHOLD,
  "counterfactual_evidence_mode": "$COUNTERFACTUAL_EVIDENCE_MODE",
  "model_call_log": "$MODEL_CALL_LOG"
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
  --counterfactual_shadow_mode "$COUNTERFACTUAL_SHADOW_MODE"
  --enable_bidirectional_counterfactual_validation "$ENABLE_BIDIRECTIONAL_COUNTERFACTUAL_VALIDATION"
  --enable_negative_control "$ENABLE_NEGATIVE_CONTROL"
  --max_negative_control_attempts "$MAX_NEGATIVE_CONTROL_ATTEMPTS"
  --max_negative_control_ast_edits "$MAX_NEGATIVE_CONTROL_AST_EDITS"
  --enable_runtime_target_reachability "$ENABLE_RUNTIME_TARGET_REACHABILITY"
  --enable_contrastive_observation_oracle "$ENABLE_CONTRASTIVE_OBSERVATION_ORACLE"
  --min_valid_surrogate_patches_for_consensus "$MIN_VALID_SURROGATE_PATCHES_FOR_CONSENSUS"
  --surrogate_consensus_threshold "$SURROGATE_CONSENSUS_THRESHOLD"
  --counterfactual_evidence_mode "$COUNTERFACTUAL_EVIDENCE_MODE"
)
if [[ -n "$LIMIT" ]]; then
  cmd+=(--limit "$LIMIT")
fi
if [[ "$RESUME" == "true" ]]; then
  cmd+=(--resume)
fi

printf '%q ' "${cmd[@]}" | tee "$RUN_DIR/command.txt"
printf '\n' | tee -a "$RUN_DIR/command.txt"
printf '%q ' "${cmd[@]}" > "$RUN_DIR/logs/generation.command.txt"
printf '\n' >> "$RUN_DIR/logs/generation.command.txt"
cd "$PACKAGE_ROOT"
"${cmd[@]}" 2>&1 | tee "$RUN_DIR/logs/generation.log"
touch "$RUN_DIR/generation.done"
