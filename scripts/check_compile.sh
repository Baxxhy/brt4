#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
cd "$PROJECT_ROOT"

mapfile -t files < <(find . \
  \( -path './results/*' -o -path './data/*' -o -path './retrieval_results/*' -o -path './__pycache__/*' \) -prune \
  -o -name '*.py' -type f -print | sort)

cd "$PACKAGE_ROOT"
python -m py_compile "${files[@]/#/$PROJECT_ROOT/}"
echo "compiled ${#files[@]} BRT4 Python files"
