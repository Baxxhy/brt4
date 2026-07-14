#!/usr/bin/env python3
"""Bounded engineering check for trace collection and source path matching."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from brt4.evaluation.direct_eval import parse_patch_coverage, run_shell, trace_test_command


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="brt4_patch_coverage_check_") as raw_root:
        root = Path(raw_root)
        project = root / "project"
        package = project / "pkg"
        coverage_dir = root / "coverage"
        package.mkdir(parents=True)
        coverage_dir.mkdir()
        (package / "mod.py").write_text(
            "def add(left, right):\n    return left + right\n", encoding="utf-8"
        )
        (project / "check.py").write_text(
            "from pkg.mod import add\nassert add(2, 3) == 5\n", encoding="utf-8"
        )
        command = trace_test_command("python check.py", str(coverage_dir))
        run = run_shell(
            f"export PYTHONPATH={project}:$PYTHONPATH && {command}",
            str(project),
            60,
        )
        parsed = parse_patch_coverage(
            coverage_dir,
            {"pkg/mod.py": [2]},
            str(project),
        )
        mismatch = parse_patch_coverage(
            coverage_dir,
            {"pkg/not_executed.py": [1]},
            str(project),
        )
        missing_dir = root / "missing_artifacts"
        missing_dir.mkdir()
        missing_artifacts = parse_patch_coverage(
            missing_dir,
            {"pkg/mod.py": [2]},
            str(project),
        )
        report = {
            "command_started": run.get("returncode") == 0,
            "trace_return_code": run.get("returncode"),
            "trace_stderr": str(run.get("stderr") or "")[-500:],
            "counts_file_created": (coverage_dir / "trace_counts.dat").is_file(),
            "cover_files_created": bool(list((coverage_dir / "cover").rglob("*.cover"))),
            "coverage_status": parsed.get("coverage_status"),
            "path_matched": bool((parsed.get("matched_sources_by_file") or {}).get("pkg/mod.py")),
            "target_lines": parsed.get("target_line_count"),
            "covered_target_lines": parsed.get("covered_line_count"),
            "nonzero_coverage": bool(parsed.get("covered_line_count")),
            "path_mismatch_status": mismatch.get("coverage_status"),
            "missing_artifact_status": missing_artifacts.get("coverage_status"),
            "failure_reason": parsed.get("failure_reason"),
        }
        print(json.dumps(report, indent=2))
        expected = {
            "command_started": True,
            "counts_file_created": True,
            "cover_files_created": True,
            "coverage_status": "SUCCESS",
            "path_matched": True,
            "target_lines": 1,
            "covered_target_lines": 1,
            "nonzero_coverage": True,
            "path_mismatch_status": "PATH_MATCH_FAILED",
            "missing_artifact_status": "PARSE_FAILED",
        }
        return 0 if all(report.get(key) == value for key, value in expected.items()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
