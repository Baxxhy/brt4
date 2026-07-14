#!/usr/bin/env python3
"""Evaluate selected tests against golden patch target lines only."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from brt4.evaluation.direct_eval import (
    apply_patch_text,
    conda_activate_cmd,
    direct_test_relpath,
    env_name_for,
    first_test_selector,
    git_reset_to,
    group_ids_by_repo,
    parse_patch_coverage,
    patch_requires_rebuild,
    patch_target_lines,
    repo_path,
    resolve_conda_env,
    run_shell,
    setup_command,
    test_command,
    trace_test_command,
    write_generated_test,
)
from brt4.io.io_utils import load_issue_data


FAILURE_STATUSES = {"RUN_FAILED", "PARSE_FAILED", "PATH_MATCH_FAILED"}


def load_local_golden_patches(issues: dict[str, dict[str, Any]]) -> str:
    """Fill golden patches from the local SWE-bench Lite Arrow cache."""

    try:
        from datasets import Dataset
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("datasets is required to read the local golden patch cache") from exc
    candidates = sorted(
        Path.home().glob(
            ".cache/huggingface/datasets/"
            "SWE-bench___swe-bench_lite/default/*/*/swe-bench_lite-test.arrow"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("local SWE-bench Lite Arrow cache is unavailable")
    dataset_path = candidates[0]
    rows = Dataset.from_file(str(dataset_path))
    by_id = {str(row["instance_id"]): row for row in rows}
    missing: list[str] = []
    for instance_id, issue in issues.items():
        source = by_id.get(instance_id)
        if source is None or not source.get("patch"):
            missing.append(instance_id)
            continue
        issue["patch"] = str(source["patch"])
    if missing:
        raise RuntimeError(f"golden patch missing for {len(missing)} instances: {missing[:5]}")
    return str(dataset_path)


def _flatten_lines(values: dict[str, list[int]]) -> list[str]:
    return [f"{path}:{line}" for path, lines in values.items() for line in lines]


def _base_result(instance_id: str, target_lines: dict[str, list[int]]) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "target_files": sorted(target_lines),
        "target_lines": _flatten_lines(target_lines),
        "covered_target_lines": [],
        "target_lines_by_file": target_lines,
        "covered_lines_by_file": {path: [] for path in target_lines},
        "patch_coverage": 0.0,
        "coverage_status": "NO_TARGET_LINES" if not target_lines else "RUN_FAILED",
        "failure_category": "NO_TARGET_LINES" if not target_lines else "NOT_COMPLETED",
        "failure_reason": "golden patch has no added or replaced Python target lines"
        if not target_lines
        else "coverage evaluation did not complete",
        "trace_return_code": None,
        "trace_timeout": False,
    }


def evaluate_one(
    issue: dict[str, Any],
    selected_dir: Path,
    repo_root_base: str,
    artifact_root: Path,
    timeout: int,
) -> dict[str, Any]:
    instance_id = str(issue["instance_id"])
    targets = patch_target_lines(str(issue.get("patch") or ""))
    result = _base_result(instance_id, targets)
    if not targets:
        return result
    final_path = selected_dir / instance_id / "final_test.py"
    if not final_path.is_file():
        result["failure_category"] = "MISSING_TEST"
        result["failure_reason"] = "selected final_test.py is missing"
        return result
    generated_worktree = selected_dir / instance_id / "worktree"
    repo_dir = str(generated_worktree) if generated_worktree.is_dir() else repo_path(repo_root_base, issue)
    result["repo_dir"] = repo_dir
    result["worktree_mode"] = (
        "selected_export_worktree" if generated_worktree.is_dir() else "shared_repo_fallback"
    )
    env_name = resolve_conda_env(env_name_for(issue))
    result["env_name"] = env_name
    code = final_path.read_text(encoding="utf-8")
    rel_file = direct_test_relpath(instance_id, str(selected_dir))
    selector = first_test_selector(code)
    command = test_command(str(issue["repo"]), str(issue["version"]), rel_file, selector)
    coverage_dir = artifact_root / instance_id
    if coverage_dir.exists():
        shutil.rmtree(coverage_dir)
    coverage_dir.mkdir(parents=True)
    try:
        git_reset_to(repo_dir, str(issue["base_commit"]), clean=False)
        applied = apply_patch_text(repo_dir, str(issue.get("patch") or ""))
        if applied.get("returncode") != 0:
            result["failure_category"] = "PATCH_APPLY_FAILED"
            result["failure_reason"] = "golden patch apply failed: " + str(
                applied.get("stderr") or applied.get("stdout") or "unknown error"
            )[-1000:]
            return result
        write_generated_test(repo_dir, rel_file, code)
        pythonpath = f"{repo_dir}:{repo_dir}/src:{repo_dir}/lib"
        if patch_requires_rebuild(str(issue.get("patch") or "")):
            setup_full = f"{conda_activate_cmd(env_name)} && {setup_command(str(issue['repo']), str(issue['version']))}"
            setup_result = run_shell(setup_full, repo_dir, timeout)
            result["setup_return_code"] = setup_result.get("returncode")
            if setup_result.get("returncode") != 0:
                result["failure_category"] = "SETUP_FAILED"
                result["failure_reason"] = "patched-side setup failed: " + str(
                    setup_result.get("stderr") or setup_result.get("stdout") or "unknown error"
                )[-1000:]
                return result
        traced = trace_test_command(command, str(coverage_dir))
        full_command = (
            f"{conda_activate_cmd(env_name)} && "
            f"export PYTHONPATH={pythonpath}:$PYTHONPATH && {traced}"
        )
        trace_run = run_shell(full_command, repo_dir, timeout)
        parsed = parse_patch_coverage(coverage_dir, targets, repo_dir)
        result.update(parsed)
        result["failure_category"] = (
            "" if parsed.get("coverage_status") == "SUCCESS" else parsed.get("coverage_status")
        )
        result["trace_return_code"] = trace_run.get("returncode")
        result["trace_timeout"] = bool(trace_run.get("timeout"))
        result["covered_target_lines"] = _flatten_lines(
            result.get("covered_lines_by_file") or {}
        )
        result["trace_command"] = traced
        if trace_run.get("timeout"):
            result["coverage_status"] = "RUN_FAILED"
            result["failure_category"] = "TRACE_TIMEOUT"
            result["failure_reason"] = "coverage trace timed out"
        elif trace_run.get("returncode") != 0:
            excerpt = str(trace_run.get("stderr") or trace_run.get("stdout") or "")[-1500:]
            result["coverage_status"] = "RUN_FAILED"
            result["failure_category"] = "TRACE_COMMAND_FAILED"
            result["failure_reason"] = f"coverage test command failed: {excerpt}"
        return result
    except Exception as exc:  # noqa: BLE001
        result["coverage_status"] = "RUN_FAILED"
        result["failure_category"] = "EXCEPTION"
        result["failure_reason"] = repr(exc)
        return result
    finally:
        if result.get("coverage_status") == "SUCCESS":
            shutil.rmtree(coverage_dir, ignore_errors=True)
        try:
            git_reset_to(repo_dir, str(issue["base_commit"]), clean=False)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs_dir", type=Path, required=True)
    parser.add_argument("--dataset_file", required=True)
    parser.add_argument("--repo_root_base", required=True)
    parser.add_argument("--output_dir", type=Path, required=True)
    parser.add_argument("--max_workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    selected_dir = args.outputs_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    issues = load_issue_data(args.dataset_file)
    golden_source = load_local_golden_patches(issues)
    instance_ids = list(issues)
    results_path = output_dir / "per_instance_results.json"
    results: dict[str, dict[str, Any]] = {}
    if args.resume and results_path.is_file():
        value = json.loads(results_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            results = value
    pending = [instance_id for instance_id in instance_ids if instance_id not in results]
    buckets = group_ids_by_repo(issues, pending, max(1, args.max_workers))
    results_lock = threading.Lock()

    def run_bucket(instance_bucket: list[str]) -> dict[str, dict[str, Any]]:
        bucket_results: dict[str, dict[str, Any]] = {}
        for instance_id in instance_bucket:
            item = evaluate_one(
                issues[instance_id],
                selected_dir,
                args.repo_root_base,
                output_dir / "trace_artifacts",
                args.timeout,
            )
            bucket_results[instance_id] = item
            with results_lock:
                results[instance_id] = item
                results_path.write_text(
                    json.dumps(results, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            print(
                f"{instance_id} {item.get('coverage_status')} "
                f"covered={len(item.get('covered_target_lines') or [])}/"
                f"{len(item.get('target_lines') or [])}",
                flush=True,
            )
        return bucket_results

    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as pool:
        futures = [pool.submit(run_bucket, bucket) for bucket in buckets if bucket]
        for future in as_completed(futures):
            future.result()

    statuses = Counter(str(item.get("coverage_status") or "UNKNOWN") for item in results.values())
    applicable = sum(bool(item.get("target_lines")) for item in results.values())
    completed = statuses.get("SUCCESS", 0)
    failed = sum(statuses.get(status, 0) for status in FAILURE_STATUSES)
    target_line_count = sum(len(item.get("target_lines") or []) for item in results.values())
    completed_target_line_count = sum(
        len(item.get("target_lines") or [])
        for item in results.values()
        if item.get("coverage_status") == "SUCCESS"
    )
    covered_line_count = sum(
        len(item.get("covered_target_lines") or [])
        for item in results.values()
        if item.get("coverage_status") == "SUCCESS"
    )
    covered_instances = sum(
        item.get("coverage_status") == "SUCCESS" and bool(item.get("covered_target_lines"))
        for item in results.values()
    )
    metrics = {
        "total_instances": len(instance_ids),
        "instances_with_patch_lines": applicable,
        "applicable_instances": applicable,
        "coverage_completed_instances": completed,
        "coverage_failed_instances": failed,
        "no_target_lines_instances": statuses.get("NO_TARGET_LINES", 0),
        "patch_covered_instances": covered_instances,
        "target_lines": target_line_count,
        "completed_target_lines": completed_target_line_count,
        "covered_target_lines": covered_line_count,
        "patch_coverage_at_1": covered_instances / applicable if applicable else 0.0,
        "patch_coverage_at_1_applicable": covered_instances / applicable if applicable else 0.0,
        "patch_coverage_at_1_dataset_total": covered_instances / len(instance_ids)
        if instance_ids
        else 0.0,
        "patch_line_coverage": covered_line_count / target_line_count
        if target_line_count
        else 0.0,
        "patch_line_coverage_completed_only": covered_line_count / completed_target_line_count
        if completed_target_line_count
        else 0.0,
        "coverage_status_counts": dict(sorted(statuses.items())),
        "failure_reason_counts": dict(
            sorted(
                Counter(
                    str(item.get("failure_category") or "UNKNOWN")
                    for item in results.values()
                    if item.get("coverage_status") in FAILURE_STATUSES
                ).items()
            )
        ),
        "golden_patch_source": golden_source,
        "selected_outputs_dir": str(selected_dir),
        "per_instance_results": results,
    }
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in metrics.items() if key != "per_instance_results"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
