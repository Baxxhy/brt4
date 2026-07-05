#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_ROOT"

python - <<'PY'
import json
from pathlib import Path

roots = [Path("results/runs"), Path("results/smoke")]
runs = []
for root in roots:
    if not root.is_dir():
        continue
    for path in root.iterdir():
        if path.is_dir():
            runs.append(path)
runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
for path in runs[:10]:
    summary = path / "evaluation" / "formal_eval_summary.json"
    direct = path / "evaluation" / "direct_eval" / "metrics.json"
    counts = {"F2P_SUCCESS": "-", "FIXED_FAIL": "-", "BUGGY_PASS": "-"}
    if summary.is_file():
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
            by_status = data.get("by_status") or data.get("status_counts") or {}
            for key in counts:
                counts[key] = by_status.get(key, counts[key])
        except Exception:
            pass
    elif direct.is_file():
        try:
            data = json.loads(direct.read_text(encoding="utf-8"))
            by_status = data.get("by_status") or {}
            for key in counts:
                counts[key] = by_status.get(key, counts[key])
        except Exception:
            pass
    markers = ",".join(p.name for p in sorted(path.glob("*.done"))) or "-"
    print(f"{path.name}\tF2P_SUCCESS={counts['F2P_SUCCESS']}\tFIXED_FAIL={counts['FIXED_FAIL']}\tBUGGY_PASS={counts['BUGGY_PASS']}\t{path}\tdone={markers}")
PY
