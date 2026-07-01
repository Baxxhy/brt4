#!/usr/bin/env bash
set -euo pipefail

ROOT=/root/Baxxhy/BugReproduce
cd "$ROOT"
mapfile -t files < <(find "$ROOT/brt3" \
  \( -path '*/results/*' -o -path '*/outputs*' -o -path '*/formal_f2p*' -o -path '*/worktree/*' -o -path '*/__pycache__/*' \) -prune \
  -o -name '*.py' -type f -print)
python -m py_compile "${files[@]}"
echo "compiled ${#files[@]} BRT3 Python files"
