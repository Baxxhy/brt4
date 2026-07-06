#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
cd "$PROJECT_ROOT"

timestamp=$(date +%Y%m%d_%H%M%S)
CANARY_RUN_NAME=${CANARY_RUN_NAME:-"run_p0_adaptive_seed_canary5_${timestamp}"}
CANARY_RUN_DIR=${CANARY_RUN_DIR:-"$PROJECT_ROOT/results/runs/$CANARY_RUN_NAME"}
FULL_RUN_NAME=${FULL_RUN_NAME:-"run_p0_adaptive_seed_full276_${timestamp}"}
FULL_RUN_DIR=${FULL_RUN_DIR:-"$PROJECT_ROOT/results/runs/$FULL_RUN_NAME"}
TMUX_SESSION=${TMUX_SESSION:-"brt4_p0_adaptive_seed_full276_${timestamp}"}
INSTANCES_PATH=${INSTANCES_PATH:-"$PROJECT_ROOT/data/issues/swt276_issues.json"}

CANARY_IDS=(
  django__django-12184
  django__django-17087
  pytest-dev__pytest-5221
  sphinx-doc__sphinx-10325
  astropy__astropy-14182
)

mkdir -p "$CANARY_RUN_DIR/tmp" "$CANARY_RUN_DIR/logs"
CANARY_INSTANCES="$CANARY_RUN_DIR/tmp/canary_instances.json"

python - "$INSTANCES_PATH" "$CANARY_INSTANCES" "${CANARY_IDS[@]}" <<'PY'
import json
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
ids = sys.argv[3:]
data = json.loads(src.read_text(encoding="utf-8"))
if isinstance(data, dict):
    rows = []
    by_id = {}
    for key, value in data.items():
        if isinstance(value, dict):
            row = dict(value)
            row.setdefault("instance_id", key)
            by_id[row["instance_id"]] = row
else:
    rows = [row for row in data if isinstance(row, dict)]
    by_id = {row.get("instance_id"): row for row in rows}
missing = [iid for iid in ids if iid not in by_id]
if missing:
    raise SystemExit(f"missing canary ids: {missing}")
dst.write_text(json.dumps([by_id[iid] for iid in ids], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

echo "[canary] generation: $CANARY_RUN_DIR"
RUN_NAME="$CANARY_RUN_NAME" \
RUN_DIR="$CANARY_RUN_DIR" \
INSTANCES_PATH="$CANARY_INSTANCES" \
WORKERS="${CANARY_WORKERS:-5}" \
SEED_WORKERS="${CANARY_SEED_WORKERS:-2}" \
MODEL="${MODEL:-deepseek-v3}" \
TEMPERATURE="${TEMPERATURE:-0.1}" \
bash "$SCRIPT_DIR/run_generate.sh"

echo "[canary] formal eval"
RUN_DIR="$CANARY_RUN_DIR" \
INSTANCES_PATH="$CANARY_INSTANCES" \
WORKERS="${CANARY_EVAL_WORKERS:-2}" \
bash "$SCRIPT_DIR/run_formal_eval.sh" "$CANARY_RUN_DIR"

python - "$CANARY_RUN_DIR" "$FULL_RUN_NAME" "$FULL_RUN_DIR" "$TMUX_SESSION" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
full_run_name = sys.argv[2]
full_run_dir = Path(sys.argv[3])
tmux_session = sys.argv[4]
ids = [
    "django__django-12184",
    "django__django-17087",
    "pytest-dev__pytest-5221",
    "sphinx-doc__sphinx-10325",
    "astropy__astropy-14182",
]
merged_path = run_dir / "evaluation" / "formal" / "merged_results.json"
metrics_path = run_dir / "evaluation" / "formal" / "metrics.json"
summary_path = run_dir / "evaluation" / "formal_eval_summary.json"
merged = json.loads(merged_path.read_text(encoding="utf-8")) if merged_path.is_file() else {}
metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else {}
rows = []
failures = []
env_statuses = {"SETUP_ERROR", "COLLECT_ERROR", "SYNTAX_ERROR", "BUGGY_SETUP_ERROR", "FIXED_SETUP_ERROR"}
formal_statuses = []
for iid in ids:
    inst = run_dir / "generation" / iid
    summary_file = inst / "summary.json"
    final_file = inst / "final_test.py"
    ranking_file = inst / "candidate_ranking.json"
    if not summary_file.is_file():
        failures.append(f"{iid}: missing summary.json")
        summary = {}
    else:
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
    if not final_file.is_file():
        failures.append(f"{iid}: missing final_test.py")
    if not ranking_file.is_file():
        failures.append(f"{iid}: missing candidate_ranking.json")
    ev = merged.get(iid, {}) if isinstance(merged, dict) else {}
    formal_status = ev.get("status", "MISSING_EVAL")
    formal_statuses.append(formal_status)
    parity = ev.get("runner_parity") or {}
    oracle_risk = summary.get("final_oracle_risk") or {}
    required = [
        "seed_attempts_summary",
        "selected_seed_index",
        "final_oracle_risk",
    ]
    for key in required:
        if key not in summary:
            failures.append(f"{iid}: missing summary field {key}")
    if not parity:
        failures.append(f"{iid}: missing runner_parity")
    rows.append(
        {
            "instance_id": iid,
            "generation_status": summary.get("status", ""),
            "formal_status": formal_status,
            "selected_seed_index": summary.get("selected_seed_index", -1),
            "seed_attempts": summary.get("seed_attempts_count", len(summary.get("seed_attempts_summary", []))),
            "oracle_risk": oracle_risk.get("level", ""),
            "surrogate_status": (summary.get("dual_version_result") or {}).get("status", ""),
            "runner_parity_warning": "; ".join(parity.get("warnings") or []),
            "final_reason": str(summary.get("final_reason") or "")[:160],
            "summary_path": str(summary_file),
            "final_test_path": str(final_file),
        }
    )
if not merged_path.is_file() or not metrics_path.is_file() or not summary_path.is_file():
    failures.append("formal eval outputs are incomplete")
if formal_statuses and all(status in env_statuses or "SETUP" in status or "COLLECT" in status or "SYNTAX" in status for status in formal_statuses):
    failures.append("all canary formal statuses are env/collect/syntax failures")
if formal_statuses and all(status == "BUGGY_PASS" for status in formal_statuses):
    failures.append("all canary formal statuses are BUGGY_PASS")
print("| instance_id | generation_status | formal_status | selected_seed_index | seed_attempts | oracle_risk | surrogate_status | runner_parity_warning | final_reason |")
print("|---|---|---|---:|---:|---|---|---|---|")
for row in rows:
    print(
        "| {instance_id} | {generation_status} | {formal_status} | {selected_seed_index} | {seed_attempts} | {oracle_risk} | {surrogate_status} | {runner_parity_warning} | {final_reason} |".format(**row)
    )
print(json.dumps({"canary_run_dir": str(run_dir), "metrics": metrics, "failures": failures}, ensure_ascii=False, indent=2))
if failures:
    (run_dir / "canary_failed.json").write_text(json.dumps({"passed": False, "failures": failures, "rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(2)
(run_dir / "canary_passed.json").write_text(json.dumps({"passed": True, "rows": rows, "metrics": metrics}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
cmd = (
    f"cd {Path.cwd()} && "
    f"RUN_NAME={full_run_name} RUN_DIR={full_run_dir} WORKERS=6 SEED_WORKERS=2 MODEL=deepseek-v3 TEMPERATURE=0.1 "
    f"bash scripts/run_full_pipeline.sh"
)
subprocess.run(["tmux", "new-session", "-d", "-s", tmux_session, cmd], check=True)
info = {
    "started": True,
    "tmux_session": tmux_session,
    "full_run_name": full_run_name,
    "full_run_dir": str(full_run_dir),
    "generation_log": str(full_run_dir / "logs" / "generation.log"),
    "evaluation_log": str(full_run_dir / "logs" / "evaluation.log"),
    "formal_eval_log": str(full_run_dir / "logs" / "formal_eval.log"),
    "export_log": str(full_run_dir / "logs" / "export.log"),
    "progress_command": f"tmux attach -t {tmux_session}",
    "metrics_command": f"cat {full_run_dir}/evaluation/formal_eval_summary.json",
}
(run_dir / "full276_start_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(info, ensure_ascii=False, indent=2))
PY
