#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_ROOT"

confirm=false
if [[ "${1:-}" == "--confirm-delete" ]]; then
  confirm=true
fi

python - "$confirm" <<'PY'
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

confirm = sys.argv[1] == "true"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
manifest_dir = Path("results/cleanup_manifests")
manifest_dir.mkdir(parents=True, exist_ok=True)
archive = Path("results/archive/legacy_best")
archive.mkdir(parents=True, exist_ok=True)

patterns = ("outputs", "formal_f2p", "direct_eval", "full_pipeline", "prepare_check")
keep_names = {"data", "retrieval_results", "results", "scripts", "prompts"}
special_tokens = ("bestsofar", "best", "complete")

dirs = [
    p for p in Path(".").iterdir()
    if p.is_dir() and p.name not in keep_names and p.name.startswith(patterns)
]
files = [
    p for p in Path(".").iterdir()
    if p.is_file() and (
        p.name.startswith(patterns)
        or p.suffix in {".pid"}
        or p.name.endswith(".run.sh")
        or (p.suffix == ".log" and p.name.startswith(patterns))
        or (p.name.startswith(("run_", "regenerate_", "wait_and_run_")) and p.suffix == ".sh")
        or (p.name.startswith(("run_", "regenerate_", "wait_and_run_")) and p.name.endswith(".sh.log"))
    )
]
run_dirs = list(Path("results/runs").glob("*")) if Path("results/runs").is_dir() else []
run_dirs = [p for p in run_dirs if p.is_dir()]

ordinary_dirs = [p for p in dirs if not any(tok in p.name.lower() for tok in special_tokens)]
ordinary_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
kept_dirs = ordinary_dirs[:3]
delete_dirs = ordinary_dirs[3:]
archive_dirs = [p for p in dirs if any(tok in p.name.lower() for tok in special_tokens)]

run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
kept_runs = run_dirs[:3]
delete_runs = run_dirs[3:]

delete_files = [p for p in files if not any(tok in p.name.lower() for tok in special_tokens)]
archive_files = [p for p in files if any(tok in p.name.lower() for tok in special_tokens)]

candidates_path = manifest_dir / f"delete_candidates_{ts}.txt"
deleted_path = manifest_dir / f"deleted_{ts}.txt"
uncertain_path = manifest_dir / f"uncertain_files_{ts}.txt"

with candidates_path.open("w", encoding="utf-8") as f:
    f.write("KEEP_DIRS\\n")
    for p in kept_dirs:
        f.write(f"{p}\\n")
    f.write("\\nKEEP_RESULTS_RUNS\\n")
    for p in kept_runs:
        f.write(f"{p}\\n")
    f.write("\\nDELETE_DIRS\\n")
    for p in delete_dirs + delete_runs:
        f.write(f"{p}\\n")
    f.write("\\nDELETE_FILES\\n")
    for p in delete_files:
        f.write(f"{p}\\n")
    f.write("\\nARCHIVE_LEGACY_BEST\\n")
    for p in archive_dirs + archive_files:
        f.write(f"{p} -> {archive / p.name}\\n")

uncertain_path.write_text("", encoding="utf-8")

actions = []
if confirm:
    for p in archive_dirs + archive_files:
        target = archive / p.name
        if target.exists():
            target = archive / f"{p.name}.{ts}"
        shutil.move(str(p), str(target))
        actions.append(f"ARCHIVED {p} -> {target}")
    for p in delete_dirs + delete_runs:
        shutil.rmtree(p)
        actions.append(f"DELETED_DIR {p}")
    for p in delete_files:
        try:
            p.unlink()
            actions.append(f"DELETED_FILE {p}")
        except FileNotFoundError:
            pass
else:
    actions.append("DRY_RUN_ONLY")

deleted_path.write_text("\\n".join(actions) + "\\n", encoding="utf-8")
print(json.dumps({
    "confirm": confirm,
    "kept_dirs": [str(p) for p in kept_dirs],
    "kept_results_runs": [str(p) for p in kept_runs],
    "delete_candidates": str(candidates_path),
    "deleted_manifest": str(deleted_path),
    "uncertain_files": str(uncertain_path),
}, ensure_ascii=False, indent=2))
PY
