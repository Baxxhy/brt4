#!/usr/bin/env python3
"""Start true-patch direct evaluation only after generation is complete."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path("/root/Baxxhy/BugReproduce")


def parse_bool(value: str) -> bool:
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected true/false")


def load_rows(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return [dict(value, instance_id=value.get("instance_id", key)) for key, value in data.items() if isinstance(value, dict)]


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_dataset_total_metrics(
    metrics: dict,
    status_counts: dict[str, int],
    *,
    dataset_total: int,
    completed_count: int,
    missing: list[str],
    missing_counted_as_failures: bool,
) -> dict:
    counts = dict(status_counts)
    if missing_counted_as_failures and missing:
        counts["MISSING_GENERATED_TEST"] = counts.get("MISSING_GENERATED_TEST", 0) + len(missing)
    denominator = dataset_total if missing_counted_as_failures else completed_count
    success = safe_int(metrics.get("f2p_success"), counts.get("F2P_SUCCESS", 0))
    patch_success = safe_int(metrics.get("patch_cov_success"), 0)
    patch_cov_enabled = bool(metrics.get("patch_cov_enabled"))
    return {
        "denominator_policy": (
            "dataset_total_missing_generated_as_fail"
            if missing_counted_as_failures
            else "completed_only"
        ),
        "dataset_total": dataset_total,
        "evaluated_instances": completed_count,
        "missing_generated_count": len(missing),
        "missing_generated": missing,
        "missing_counted_as_failures": missing_counted_as_failures,
        "total_instances": denominator,
        "f2p_success": success,
        "f2p_fail": max(denominator - success, 0),
        "f2p_at_1": ratio(success, denominator),
        "f2p_at_1_percent": round(ratio(success, denominator) * 100, 4),
        "by_status": dict(sorted(counts.items())),
        "completed_only_total_instances": metrics.get("total_instances", completed_count),
        "completed_only_f2p_at_1": metrics.get("f2p_at_1", 0),
        "completed_only_f2p_at_1_percent": metrics.get("f2p_at_1_percent", 0),
        "patch_cov_enabled": patch_cov_enabled,
        "patch_cov_success": patch_success,
        "patch_cov_fail_dataset_total": max(denominator - patch_success, 0) if patch_cov_enabled else 0,
        "patch_cov_at_1_dataset_total": ratio(patch_success, denominator) if patch_cov_enabled else 0,
        "patch_cov_at_1_dataset_total_percent": (
            round(ratio(patch_success, denominator) * 100, 4) if patch_cov_enabled else 0
        ),
        "patch_cov_applicable_completed_only": metrics.get("patch_cov_applicable", 0),
        "patch_cov_at_1_applicable_completed_only": metrics.get("patch_cov_at_1", 0),
        "patch_cov_at_1_applicable_completed_only_percent": metrics.get("patch_cov_at_1_percent", 0),
        "patch_cov_target_lines": metrics.get("patch_cov_target_lines", 0),
        "patch_cov_covered_lines": metrics.get("patch_cov_covered_lines", 0),
        "patch_line_coverage": metrics.get("patch_line_coverage", 0),
        "patch_line_coverage_percent": metrics.get("patch_line_coverage_percent", 0),
        "patch_cov_by_status": metrics.get("patch_cov_by_status", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outputs_dir", required=True)
    parser.add_argument("--dataset_file", default=str(ROOT / "brt2/data/issues/swt276_issues.json"))
    parser.add_argument("--patch_file", default="")
    parser.add_argument("--repo_root_base", default=str(ROOT / "swe_repos"))
    parser.add_argument("--max_workers", type=int, default=6)
    parser.add_argument("--eval_completed_only", type=parse_bool, default=False)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--evaluation_dir", default="")
    parser.add_argument("--eval_clone_root", default="")
    parser.add_argument("--eval_worktree_root", default="")
    parser.add_argument("--log_path", default="")
    parser.add_argument("--summary_path", default="")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--compute_patch_coverage", type=parse_bool, default=True)
    parser.add_argument(
        "--missing_generated_policy",
        choices=["abort", "count_as_fail"],
        default="count_as_fail",
        help=(
            "When --eval_completed_only=false and some instances lack final_test.py, "
            "either abort or evaluate completed instances and count missing tests as failures "
            "in dataset-total metrics."
        ),
    )
    parser.add_argument(
        "--use_generated_worktrees",
        action="store_true",
        help=(
            "Opt in to evaluating inside generation/<instance>/worktree. "
            "The default creates isolated formal-eval local clones under --eval_clone_root."
        ),
    )
    args = parser.parse_args()
    outputs = Path(args.outputs_dir)
    rows = load_rows(Path(args.dataset_file))
    completed = [row for row in rows if (outputs / str(row.get("instance_id")) / "final_test.py").is_file()]
    missing = [str(row.get("instance_id")) for row in rows if row not in completed]
    formal_dir = Path(args.evaluation_dir).resolve() if args.evaluation_dir else outputs / "formal_eval"
    formal_dir.mkdir(parents=True, exist_ok=True)
    print(f"已生成 {len(completed)}/{len(rows)}，未完成 {len(missing)}")
    if missing:
        print("未完成实例:", ", ".join(missing[:50]))
    if not completed:
        if missing and not args.eval_completed_only and args.missing_generated_policy == "count_as_fail":
            merged_with_missing = {
                instance_id: {
                    "instance_id": instance_id,
                    "status": "MISSING_GENERATED_TEST",
                    "success": False,
                    "missing_generated_test": True,
                }
                for instance_id in missing
            }
            metrics_dataset_total = build_dataset_total_metrics(
                {},
                {"MISSING_GENERATED_TEST": len(missing)},
                dataset_total=len(rows),
                completed_count=0,
                missing=missing,
                missing_counted_as_failures=True,
            )
            write_json(formal_dir / "merged_results_with_missing.json", merged_with_missing)
            write_json(formal_dir / "metrics_dataset_total.json", metrics_dataset_total)
            summary_path = Path(args.summary_path).resolve() if args.summary_path else outputs / "formal_eval_summary.json"
            write_json(
                summary_path,
                {
                    "returncode": 0,
                    "completed": 0,
                    "dataset_total": len(rows),
                    "missing": missing,
                    "missing_counted_as_failures": True,
                    "metrics": {},
                    "metrics_completed_only": {},
                    "metrics_dataset_total": metrics_dataset_total,
                    "formal_categories": {"MISSING_GENERATED_TEST": len(missing)},
                    "formal_categories_completed_only": {},
                    "log": "",
                    "worktree_mode": "not_started_no_generated_tests",
                },
            )
            print("没有可执行的 final_test.py；已按数据集总数写出全失败 summary。")
            return 0
        return 1
    missing_counted_as_failures = (
        bool(missing)
        and not args.eval_completed_only
        and args.missing_generated_policy == "count_as_fail"
    )
    if missing and not args.eval_completed_only:
        if args.missing_generated_policy == "abort":
            print("未全部生成，正式评测未启动。")
            return 1
        print("未全部生成；继续评测已生成实例，缺失实例将在数据集总分母指标中计为失败。")
    if args.patch_file:
        if len(completed) != 1:
            print("--patch_file 只支持单 instance；批量请使用包含 patch 的 dataset 或 SWE-bench 数据加载。", file=sys.stderr)
            return 2
        completed[0]["patch"] = Path(args.patch_file).read_text(encoding="utf-8")
    eval_clone_root = (
        Path(args.eval_clone_root).resolve()
        if args.eval_clone_root
        else formal_dir / "eval_clones"
    )
    eval_worktree_root = (
        Path(args.eval_worktree_root).resolve()
        if args.eval_worktree_root
        else Path("")
    )
    log_path = Path(args.log_path).resolve() if args.log_path else ROOT / "brt4/logs/formal_eval.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
        json.dump(completed, handle, ensure_ascii=False)
        filtered_path = handle.name
    command = [
        sys.executable, "-m", "brt4.direct_eval",
        "--instances_path", filtered_path,
        "--generated_dir", str(outputs),
        "--repo_root_base", args.repo_root_base,
        "--output_dir", str(formal_dir),
        "--max_workers", str(args.max_workers),
        "--timeout", str(args.timeout),
        "--eval_clone_root", str(eval_clone_root),
    ]
    if args.eval_worktree_root:
        command.extend(["--eval_worktree_root", str(eval_worktree_root)])
    if args.use_generated_worktrees:
        command.append("--use_generated_worktrees")
    if args.compute_patch_coverage:
        command.extend(["--compute_patch_coverage", "true"])
    if not args.patch_file and not all(row.get("patch") for row in completed):
        command.append("--use_swebench_lite")
    if args.resume:
        command.append("--resume")
    try:
        log_mode = "a" if args.resume else "w"
        with log_path.open(log_mode, encoding="utf-8") as log:
            proc = subprocess.Popen(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
            returncode = proc.wait()
    finally:
        os.unlink(filtered_path)
    metrics_path = formal_dir / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else {}
    merged_path = formal_dir / "merged_results.json"
    merged = json.loads(merged_path.read_text(encoding="utf-8")) if merged_path.is_file() else {}
    normalized: dict[str, int] = {}
    for result in merged.values():
        raw = str(result.get("status") or "UNKNOWN")
        if raw == "F2P_SUCCESS":
            category = "F2P_SUCCESS"
        elif raw == "BUGGY_PASS":
            category = "BUGGY_PASS"
        elif raw == "FIXED_FAIL":
            category = "FIXED_FAIL"
        elif any(marker in raw for marker in ("SETUP", "COLLECT", "SYNTAX", "TIMEOUT")):
            category = "ENV_OR_COLLECT"
        else:
            category = raw
        normalized[category] = normalized.get(category, 0) + 1
    merged_with_missing = dict(merged)
    normalized_with_missing = dict(normalized)
    if missing_counted_as_failures:
        normalized_with_missing["MISSING_GENERATED_TEST"] = (
            normalized_with_missing.get("MISSING_GENERATED_TEST", 0) + len(missing)
        )
        for instance_id in missing:
            merged_with_missing.setdefault(
                instance_id,
                {
                    "instance_id": instance_id,
                    "status": "MISSING_GENERATED_TEST",
                    "success": False,
                    "missing_generated_test": True,
                },
            )
    metrics_dataset_total = build_dataset_total_metrics(
        metrics,
        normalized,
        dataset_total=len(rows),
        completed_count=len(completed),
        missing=missing,
        missing_counted_as_failures=missing_counted_as_failures,
    )
    write_json(formal_dir / "merged_results_with_missing.json", merged_with_missing)
    write_json(formal_dir / "metrics_dataset_total.json", metrics_dataset_total)
    summary = {
        "returncode": returncode,
        "completed": len(completed),
        "dataset_total": len(rows),
        "missing": missing,
        "missing_generated_policy": args.missing_generated_policy,
        "missing_counted_as_failures": missing_counted_as_failures,
        "metrics": metrics,
        "metrics_completed_only": metrics,
        "metrics_dataset_total": metrics_dataset_total,
        "patch_coverage": {
            "enabled": bool(metrics.get("patch_cov_enabled")),
            "applicable": metrics.get("patch_cov_applicable", 0),
            "success": metrics.get("patch_cov_success", 0),
            "patch_cov_at_1": metrics.get("patch_cov_at_1", 0),
            "patch_cov_at_1_percent": metrics.get("patch_cov_at_1_percent", 0),
            "target_lines": metrics.get("patch_cov_target_lines", 0),
            "covered_lines": metrics.get("patch_cov_covered_lines", 0),
            "line_coverage": metrics.get("patch_line_coverage", 0),
            "line_coverage_percent": metrics.get("patch_line_coverage_percent", 0),
            "by_status": metrics.get("patch_cov_by_status", {}),
        },
        "formal_categories": normalized,
        "formal_categories_with_missing": normalized_with_missing,
        "log": str(log_path),
        "eval_clone_root": str(eval_clone_root),
        "eval_worktree_root": str(eval_worktree_root) if args.eval_worktree_root else "",
        "worktree_mode": (
            "generated_instance_worktree"
            if args.use_generated_worktrees
            else "isolated_eval_worktree"
            if args.eval_worktree_root
            else "isolated_eval_clone"
        ),
    }
    summary_path = Path(args.summary_path).resolve() if args.summary_path else outputs / "formal_eval_summary.json"
    write_json(summary_path, summary)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
