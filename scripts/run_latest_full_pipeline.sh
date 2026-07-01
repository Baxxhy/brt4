#!/usr/bin/env bash
set -euo pipefail

ROOT=/root/Baxxhy/BugReproduce
BRT3="/brt4"
DEFAULT_INSTANCES="$ROOT/brt2/data/issues/swt276_issues.json"
DEFAULT_CODE="$ROOT/iCoRe/retrieval_results/code/code_retrieval_results_gpt.json"
DEFAULT_TESTS="$ROOT/iCoRe/retrieval_results/test/icore/gpt/related_tests.json"
DEFAULT_REPOS="$ROOT/swe_repos"

instances_file="$DEFAULT_INSTANCES"
instances_explicit=false
model="deepseek-v3"
max_workers=6
max_workers_explicit=false
run_id=""
run_dir=""
mode="full"
resume=false
smoke=false
smoke_n=5
temperature=0.1
timeout=1800
original_args=("$@")

usage() {
  cat <<'EOF'
Usage: bash scripts/run_latest_full_pipeline.sh [options]

Options:
  --instances-file PATH  Issue dataset JSON/JSONL, or TXT with one instance ID per line.
  --model NAME           LLM model (default: deepseek-v3).
  --max-workers N        Generation and formal-evaluation workers (default: 6).
  --run-id ID            Run directory name (default: run_YYYYMMDD_HHMMSS).
  --run-dir PATH         Reuse or create an explicit run directory; overrides --run-id.
  --generation-only      Run generation and partial export, but no true-patch evaluation.
  --evaluation-only      Evaluate an existing run's generated tests, then export.
  --export-only          Rebuild JSON exports without generation or evaluation.
  --resume               Resume generation summaries and formal worker results.
  --smoke                Limit the resolved input to the first smoke instances.
  --smoke-n N            Number of smoke instances (default: 5).
  --temperature FLOAT    Generation temperature (default: 0.1).
  --timeout SECONDS      Per setup/test timeout (default: 1800).
  --help                 Show this help.

The three *-only modes are mutually exclusive. The default runs generation,
formal true-patch evaluation, and export in separate phases.
EOF
}

while (($#)); do
  case "$1" in
    --instances-file) instances_file=$2; instances_explicit=true; shift 2 ;;
    --model) model=$2; shift 2 ;;
    --max-workers) max_workers=$2; max_workers_explicit=true; shift 2 ;;
    --run-id) run_id=$2; shift 2 ;;
    --run-dir) run_dir=$2; shift 2 ;;
    --generation-only)
      [[ "$mode" == "full" ]] || { echo "Only one *-only mode may be selected." >&2; exit 2; }
      mode="generation_only"; shift ;;
    --evaluation-only)
      [[ "$mode" == "full" ]] || { echo "Only one *-only mode may be selected." >&2; exit 2; }
      mode="evaluation_only"; shift ;;
    --export-only)
      [[ "$mode" == "full" ]] || { echo "Only one *-only mode may be selected." >&2; exit 2; }
      mode="export_only"; shift ;;
    --resume) resume=true; shift ;;
    --smoke) smoke=true; shift ;;
    --smoke-n) smoke_n=$2; shift 2 ;;
    --temperature) temperature=$2; shift 2 ;;
    --timeout) timeout=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$smoke" == true && "$max_workers_explicit" != true ]]; then
  max_workers=5
fi

[[ "$max_workers" =~ ^[1-9][0-9]*$ ]] || { echo "--max-workers must be positive." >&2; exit 2; }
[[ "$smoke_n" =~ ^[1-9][0-9]*$ ]] || { echo "--smoke-n must be positive." >&2; exit 2; }
[[ "$timeout" =~ ^[1-9][0-9]*$ ]] || { echo "--timeout must be positive." >&2; exit 2; }
instances_file=$(realpath -m "$instances_file")

if [[ -z "$run_dir" ]]; then
  if [[ -z "$run_id" ]]; then
    run_id="run_$(date +%Y%m%d_%H%M%S)"
    $smoke && run_id="${run_id}_smoke"
  fi
  run_dir="$BRT3/results/runs/$run_id"
else
  run_dir=$(realpath -m "$run_dir")
  run_id=$(basename "$run_dir")
fi

if [[ -e "$run_dir" && "$resume" != true && ( "$mode" == "full" || "$mode" == "generation_only" ) ]]; then
  echo "Run directory already exists; use --resume or an *-only mode: $run_dir" >&2
  exit 2
fi
mkdir -p "$run_dir"/{generation,evaluation,logs,checkpoints,exports}

if [[ -f "$run_dir/run_config.json" && "$instances_explicit" != true ]]; then
  existing_instances=$(python - "$run_dir/run_config.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get("resolved_instances_file") or data.get("instances_file") or "")
PY
)
  [[ -z "$existing_instances" ]] || instances_file="$existing_instances"
fi

resolved_instances="$run_dir/resolved_instances.json"
if [[ "$mode" != "export_only" || ! -f "$resolved_instances" ]]; then
  cd "$ROOT"
  python - "$instances_file" "$DEFAULT_INSTANCES" "$resolved_instances" "$smoke" "$smoke_n" <<'PY'
import json, sys
from pathlib import Path
from brt4.io_utils import load_issue_data

source, canonical, output, smoke, smoke_n = sys.argv[1:]
source_path = Path(source)
if source_path.suffix.lower() == ".txt":
    wanted = [line.strip() for line in source_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = load_issue_data(canonical)
    selected = [rows[iid] for iid in wanted if iid in rows]
else:
    rows = load_issue_data(source)
    selected = list(rows.values())
if smoke == "true":
    selected = selected[: int(smoke_n)]
Path(output).write_text(json.dumps(selected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"resolved_instances={len(selected)}")
PY
fi

instance_count=$(python -c 'import json,sys; print(len(json.load(open(sys.argv[1], encoding="utf-8"))))' "$resolved_instances")
[[ "$instance_count" -gt 0 ]] || { echo "No instances resolved." >&2; exit 2; }

python - "$run_dir/run_config.json" <<PY
import json
from datetime import datetime
from pathlib import Path
path = Path(${run_dir@Q}) / "run_config.json"
old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
data = {
    "run_id": ${run_id@Q},
    "created_at": old.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "project_root": ${BRT3@Q},
    "instances_file": ${instances_file@Q},
    "resolved_instances_file": ${resolved_instances@Q},
    "code_retrieval_path": ${DEFAULT_CODE@Q},
    "test_retrieval_path": ${DEFAULT_TESTS@Q},
    "repo_root_base": ${DEFAULT_REPOS@Q},
    "model": ${model@Q},
    "temperature": float(${temperature@Q}),
    "max_workers": int(${max_workers@Q}),
    "timeout": int(${timeout@Q}),
    "max_env_rounds": 3,
    "max_brt_rounds": 3,
    "max_patch_rounds": 3,
    "validation_mode": "surrogate_patch",
    "enable_protocol_recovery": True,
    "enable_seed_mutation": True,
    "enable_observation_oracle": True,
    "enable_strict_semantic_verifier": True,
    "instance_count": int(${instance_count@Q}),
    "smoke": ${smoke@Q} == "true",
}
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

invocation=$(printf '%q ' bash "$BRT3/scripts/run_latest_full_pipeline.sh" "${original_args[@]}")
manifest_path="$run_dir/manifest.txt"
if [[ -f "$manifest_path" ]]; then
  cat >> "$manifest_path" <<EOF

resume_started_at: $(date '+%Y-%m-%d %H:%M:%S')
resume_invocation: $invocation
resume_mode: $mode
resume_instance_count: $instance_count
resume_model: $model
resume_temperature: $temperature
resume_max_workers: $max_workers
resume_timeout_seconds: $timeout
EOF
else
  cat > "$manifest_path" <<EOF
BRT3 organized run
run_id: $run_id
started_at: $(date '+%Y-%m-%d %H:%M:%S')
invocation: $invocation
mode: $mode
instances_file: $instances_file
resolved_instances_file: $resolved_instances
instance_count: $instance_count
model: $model
temperature: $temperature
max_workers: $max_workers
timeout_seconds: $timeout
max_env_rounds: 3
max_brt_rounds: 3
max_patch_rounds: 3
validation_mode: surrogate_patch
protocol_recovery: true
seed_mutation: true
observation_oracle: true
strict_semantic_verifier: true
generation_dir: $run_dir/generation
evaluation_dir: $run_dir/evaluation
note: API credentials are intentionally excluded.
EOF
fi

cd "$ROOT"
bash "$BRT3/scripts/check_compile.sh"

if [[ "$mode" == "full" || "$mode" == "generation_only" ]]; then
  python - <<'PY'
from brt4.api_pool import configured_apis
count = len(configured_apis())
if count < 1:
    raise SystemExit("No configured API accounts")
print(f"configured_api_accounts={count}")
PY
  generation_cmd=(
    python -m brt4.run
    --instances_path "$resolved_instances"
    --code_retrieval_path "$DEFAULT_CODE"
    --test_retrieval_path "$DEFAULT_TESTS"
    --repo_root_base "$DEFAULT_REPOS"
    --output_dir "$run_dir/generation"
    --model "$model"
    --temperature "$temperature"
    --max_workers "$max_workers"
    --num_candidates 1
    --timeout "$timeout"
    --max_env_rounds 3
    --max_brt_rounds 3
    --max_patch_rounds 3
    --validation_mode surrogate_patch
    --enable_protocol_recovery true
    --enable_seed_mutation true
    --enable_observation_oracle true
    --enable_strict_semantic_verifier true
  )
  $resume && generation_cmd+=(--resume)
  printf '%q ' "${generation_cmd[@]}" > "$run_dir/logs/generation.command.txt"
  printf '\n' >> "$run_dir/logs/generation.command.txt"
  "${generation_cmd[@]}" 2>&1 | tee -a "$run_dir/logs/generation.log"
  python - "$resolved_instances" "$run_dir/generation" <<'PY'
import json, sys
from pathlib import Path
rows = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
out = Path(sys.argv[2])
ids = [str(row["instance_id"]) for row in rows]
missing = [iid for iid in ids if not (out / iid / "summary.json").is_file()]
print(f"generation_summaries={len(ids) - len(missing)}/{len(ids)}")
if missing:
    print("missing generation summaries:", ", ".join(missing[:50]))
    raise SystemExit(1)
PY
  touch "$run_dir/generation.done"
fi

if [[ "$mode" == "full" || "$mode" == "evaluation_only" ]]; then
  evaluation_cmd=(
    python "$BRT3/scripts/run_formal_eval_after_generation.py"
    --outputs_dir "$run_dir/generation"
    --evaluation_dir "$run_dir/evaluation"
    --summary_path "$run_dir/evaluation/formal_eval_summary.json"
    --log_path "$run_dir/logs/evaluation.log"
    --dataset_file "$resolved_instances"
    --repo_root_base "$DEFAULT_REPOS"
    --max_workers "$max_workers"
    --timeout "$timeout"
    --eval_completed_only false
    --resume
  )
  printf '%q ' "${evaluation_cmd[@]}" > "$run_dir/logs/evaluation.command.txt"
  printf '\n' >> "$run_dir/logs/evaluation.command.txt"
  "${evaluation_cmd[@]}" 2>&1 | tee -a "$run_dir/logs/evaluation_driver.log"
  touch "$run_dir/evaluation.done"
fi

export_cmd=(python "$BRT3/scripts/export_run_outputs.py" --run-dir "$run_dir")
printf '%q ' "${export_cmd[@]}" > "$run_dir/logs/export.command.txt"
printf '\n' >> "$run_dir/logs/export.command.txt"
"${export_cmd[@]}" 2>&1 | tee -a "$run_dir/logs/export.log"
touch "$run_dir/export.done"

python - "$run_dir" "$BRT3" <<'PY'
import json, sys
from datetime import datetime
from pathlib import Path
run_dir, project = map(Path, sys.argv[1:])
summary_path = run_dir / "exports/final_summary.json"
summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
cleanup = project / "results/cleanup/latest_cleanup_timestamp.txt"
cleanup_ts = cleanup.read_text(encoding="utf-8").strip() if cleanup.is_file() else "unknown"
text = f"""# BRT3 Run Self Check

- Run directory: `{run_dir}`
- Updated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- Cleanup manifest timestamp: {cleanup_ts}
- Preserved 42% result: `results/preserved/f2p_42_20260623_091037`
- Preserved 40% result: `results/preserved/f2p_40_20260621_224723`
- Total instances: {summary.get('total_instances')}
- Generated: {summary.get('generated_count')}
- Evaluated: {summary.get('evaluated_count')}
- F2P: {summary.get('f2p_success_count')}/{summary.get('total_instances')} = {summary.get('f2p_success_rate')}
- Status counts: `{json.dumps(summary.get('status_counts', {}), ensure_ascii=False)}`
- Failure categories: `{json.dumps(summary.get('failure_category_counts', {}), ensure_ascii=False)}`
- `all_outputs.json`: {(run_dir / 'exports/all_outputs.json').is_file()}
- `all_outputs.jsonl`: {(run_dir / 'exports/all_outputs.jsonl').is_file()}
- `all_tests_only.json`: {(run_dir / 'exports/all_tests_only.json').is_file()}
- `final_summary.json`: {summary_path.is_file()}
- README.md: {(project / 'README.md').is_file()}
- README_RUN.md: {(project / 'README_RUN.md').is_file()}
- README_STRUCTURE.md: {(project / 'README_STRUCTURE.md').is_file()}

Generation, formal evaluation, and export errors are recorded in `logs/`.
"""
(run_dir / "SELF_CHECK_RUN.md").write_text(text, encoding="utf-8")
PY

summary="$run_dir/exports/final_summary.json"
echo
echo "整理完成。"
echo "新运行结果：$run_dir"
python - "$summary" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"F2P: {d['f2p_success_count']}/{d['total_instances']} = {d['f2p_success_rate']:.4%}")
PY
echo "导出文件：$run_dir/exports/all_outputs.json"
echo "tmux session: ${TMUX_PANE:-not-running-inside-tmux}"
cat >> "$manifest_path" <<EOF
completed_at: $(date '+%Y-%m-%d %H:%M:%S')
completed_mode: $mode
EOF
