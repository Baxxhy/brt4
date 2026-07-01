#!/usr/bin/env python3
"""Export one organized BRT3 run into analysis-friendly JSON files."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INSTANCES = PROJECT_ROOT.parent / "brt2/data/issues/swt276_issues.json"


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    if path.suffix.lower() in {".jsonl", ".txt"}:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                value = {"instance_id": line}
            if isinstance(value, dict):
                rows.append(value)
        return rows
    data = _read_json(path, [])
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        rows = []
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            row = dict(value)
            row.setdefault("instance_id", key)
            rows.append(row)
        return rows
    return []


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _rel(path: Path | None, run_dir: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(run_dir.resolve()))
    except (OSError, ValueError):
        return str(path)


def _excerpt(execution: dict[str, Any], limit: int = 4000) -> str | None:
    if not execution:
        return None
    text = "\n".join(
        str(execution.get(key) or "") for key in ("stdout", "stderr", "error_reason")
    ).strip()
    return text[-limit:] if text else None


def _evaluation_results(evaluation_dir: Path) -> tuple[dict[str, Any], Path | None]:
    merged_path = evaluation_dir / "merged_results.json"
    merged = _read_json(merged_path, None)
    if isinstance(merged, dict):
        return merged, merged_path
    results: dict[str, Any] = {}
    for path in sorted(evaluation_dir.glob("worker_*/results.json")):
        data = _read_json(path, {})
        if isinstance(data, dict):
            results.update(data)
    return results, None


def _failure_category(result: dict[str, Any]) -> str | None:
    status = str(result.get("status") or "")
    if not status or status == "F2P_SUCCESS":
        return None
    if status == "BUGGY_PASS":
        return "BUGGY_PASS"
    if status in {"FIXED_FAIL", "PATCHED_FAIL"}:
        return "FIXED_FAIL"
    if "PATCH_APPLY" in status:
        return "PATCH_APPLY_ERROR"
    for category in ("SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT"):
        if category.replace("_ERROR", "") in status or category in status:
            return category
    if status in {"ERROR", "UNKNOWN", "MISSING_GENERATED_TEST"}:
        return status
    return "UNRELATED_FAIL"


def _selected_checkpoint(
    instance_id: str,
    instance_dir: Path,
    checkpoint_root: Path,
    ranking: dict[str, Any],
) -> tuple[int | None, int, dict[str, str | None]]:
    checkpoints = ranking.get("checkpoints") if isinstance(ranking, dict) else []
    if not isinstance(checkpoints, list):
        checkpoints = []
    selected = ranking.get("selected_attempt") if isinstance(ranking, dict) else None
    if not isinstance(selected, int):
        selected = None
        for item in checkpoints:
            if isinstance(item, dict) and item.get("selected"):
                value = item.get("round_id")
                if isinstance(value, int):
                    selected = value
                    break
    source_dir = instance_dir / "checkpoints"
    target_dir = checkpoint_root / instance_id
    copied: dict[str, str | None] = {"code": None, "metadata": None}
    if selected is not None:
        target_dir.mkdir(parents=True, exist_ok=True)
        for suffix, key in ((".py", "code"), (".json", "metadata")):
            source = source_dir / f"candidate_attempt_{selected}{suffix}"
            if source.is_file():
                target = target_dir / f"selected{suffix}"
                shutil.copy2(source, target)
                copied[key] = str(target)
    return selected, len(checkpoints), copied


def export_run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).resolve()
    generation_dir = run_dir / "generation"
    evaluation_dir = run_dir / "evaluation"
    exports_dir = run_dir / "exports"
    checkpoint_root = run_dir / "checkpoints"
    config = _read_json(run_dir / "run_config.json", {})
    instances_path = Path(
        str(config.get("resolved_instances_file") or config.get("instances_file") or DEFAULT_INSTANCES)
    )
    if not instances_path.is_absolute():
        instances_path = run_dir / instances_path
    rows = _load_rows(instances_path)
    instance_ids = [str(row.get("instance_id")) for row in rows if row.get("instance_id")]
    eval_results, merged_path = _evaluation_results(evaluation_dir)
    if not instance_ids:
        discovered = {
            path.name
            for path in generation_dir.iterdir()
            if path.is_dir() and path.name != "formal_eval"
        } if generation_dir.is_dir() else set()
        instance_ids = sorted(discovered | set(eval_results))

    records: list[dict[str, Any]] = []
    checkpoint_index: list[dict[str, Any]] = []
    for instance_id in instance_ids:
        instance_dir = generation_dir / instance_id
        summary_path = instance_dir / "summary.json"
        final_test_path = instance_dir / "final_test.py"
        ranking_path = instance_dir / "candidate_ranking.json"
        summary = _read_json(summary_path, {})
        ranking = _read_json(ranking_path, {})
        evaluation = eval_results.get(instance_id, {})
        selected_attempt, candidate_count, copied = _selected_checkpoint(
            instance_id, instance_dir, checkpoint_root, ranking
        )
        if copied["code"] or copied["metadata"]:
            checkpoint_index.append(
                {
                    "instance_id": instance_id,
                    "selected_attempt": selected_attempt,
                    "code": _rel(Path(copied["code"]) if copied["code"] else None, run_dir),
                    "metadata": _rel(Path(copied["metadata"]) if copied["metadata"] else None, run_dir),
                }
            )
        generated = final_test_path.is_file()
        test_content = final_test_path.read_text(encoding="utf-8", errors="replace") if generated else None
        buggy_execution = summary.get("buggy_execution") if isinstance(summary, dict) else {}
        if not isinstance(buggy_execution, dict):
            buggy_execution = {}
        dual = summary.get("dual_version_result") if isinstance(summary, dict) else {}
        if not isinstance(dual, dict):
            dual = {}
        buggy = evaluation.get("buggy") if isinstance(evaluation, dict) else {}
        fixed = evaluation.get("fixed") if isinstance(evaluation, dict) else {}
        buggy_run = evaluation.get("buggy_run") if isinstance(evaluation, dict) else {}
        fixed_run = evaluation.get("fixed_run") if isinstance(evaluation, dict) else {}
        buggy = buggy if isinstance(buggy, dict) else {}
        fixed = fixed if isinstance(fixed, dict) else {}
        buggy_run = buggy_run if isinstance(buggy_run, dict) else {}
        fixed_run = fixed_run if isinstance(fixed_run, dict) else {}
        status = evaluation.get("status") if isinstance(evaluation, dict) else None
        direct_path = evaluation.get("direct_test_repo_path") if isinstance(evaluation, dict) else None
        selector = evaluation.get("selector") if isinstance(evaluation, dict) else None
        execution_log = None
        if selected_attempt is not None:
            candidate_log = instance_dir / "logs" / f"execution_round_{selected_attempt}.log"
            if candidate_log.is_file():
                execution_log = candidate_log
        error = summary.get("error") if isinstance(summary, dict) else None
        if not error and isinstance(evaluation, dict):
            error = evaluation.get("error")
        strict_failure = summary.get("strict_failure_class") if isinstance(summary, dict) else None
        record = {
            "instance_id": instance_id,
            "generated": generated,
            "evaluated": instance_id in eval_results,
            "selected_test": {
                "test_file_path": direct_path or _rel(final_test_path, run_dir),
                "test_nodeid": selector,
                "test_content": test_content,
                "patch_content": None,
            },
            "generation": {
                "status": summary.get("status") if isinstance(summary, dict) else None,
                "final_round": selected_attempt if selected_attempt is not None else summary.get("rounds_used") if isinstance(summary, dict) else None,
                "candidate_count": candidate_count or (1 if generated else 0),
                "selected_candidate_index": selected_attempt,
                "verifier_decision": summary.get("strict_verifier_decision") if isinstance(summary, dict) else None,
                "buggy_execution_status": buggy_execution.get("status"),
                "buggy_returncode": buggy_execution.get("returncode"),
                "buggy_log_excerpt": _excerpt(buggy_execution),
                "issue_aligned": strict_failure == "issue_aligned" if strict_failure else None,
                "surrogate_status": dual.get("status"),
            },
            "formal_evaluation": {
                "status": status,
                "buggy_status": buggy.get("status"),
                "patched_status": fixed.get("status"),
                "buggy_returncode": buggy_run.get("returncode"),
                "patched_returncode": fixed_run.get("returncode"),
                "is_f2p_success": status == "F2P_SUCCESS",
                "failure_category": _failure_category(evaluation if isinstance(evaluation, dict) else {}),
            },
            "paths": {
                "generation_record": _rel(summary_path, run_dir) if summary_path.is_file() else None,
                "evaluation_record": _rel(merged_path, run_dir) if instance_id in eval_results and merged_path else None,
                "test_file": _rel(final_test_path, run_dir) if generated else None,
                "buggy_log": _rel(execution_log, run_dir) if execution_log else None,
                "patched_log": None,
            },
            "error": error,
        }
        records.append(record)

    generated_count = sum(1 for item in records if item["generated"])
    evaluated_count = sum(1 for item in records if item["evaluated"])
    success_count = sum(1 for item in records if item["formal_evaluation"]["is_f2p_success"])
    status_counts = Counter(
        item["formal_evaluation"]["status"] or "NOT_EVALUATED" for item in records
    )
    failure_counts = Counter(
        item["formal_evaluation"]["failure_category"]
        for item in records
        if item["formal_evaluation"]["failure_category"]
    )
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_outputs = {
        "run_id": run_dir.name,
        "created_at": created_at,
        "project_root": str(PROJECT_ROOT),
        "generation_dir": _rel(generation_dir, run_dir),
        "evaluation_dir": _rel(evaluation_dir, run_dir),
        "total_instances": len(instance_ids),
        "generated_count": generated_count,
        "evaluated_count": evaluated_count,
        "f2p_success_count": success_count,
        "f2p_success_rate": success_count / len(instance_ids) if instance_ids else 0.0,
        "records": records,
    }
    tests_only = [
        {
            "instance_id": item["instance_id"],
            "test_file_path": item["selected_test"]["test_file_path"],
            "test_content": item["selected_test"]["test_content"],
            "formal_status": item["formal_evaluation"]["status"],
            "is_f2p_success": item["formal_evaluation"]["is_f2p_success"],
        }
        for item in records
    ]
    final_summary = {
        "run_id": run_dir.name,
        "total_instances": len(instance_ids),
        "generated_count": generated_count,
        "evaluated_count": evaluated_count,
        "f2p_success_count": success_count,
        "f2p_success_rate": success_count / len(instance_ids) if instance_ids else 0.0,
        "status_counts": dict(status_counts),
        "failure_category_counts": dict(failure_counts),
    }

    output_json = Path(args.output_json) if args.output_json else exports_dir / "all_outputs.json"
    output_jsonl = Path(args.output_jsonl) if args.output_jsonl else exports_dir / "all_outputs.jsonl"
    tests_json = Path(args.tests_only_json) if args.tests_only_json else exports_dir / "all_tests_only.json"
    summary_json = Path(args.summary_json) if args.summary_json else exports_dir / "final_summary.json"
    _atomic_json(output_json, all_outputs)
    _atomic_text(output_jsonl, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records))
    _atomic_json(tests_json, tests_only)
    _atomic_json(summary_json, final_summary)
    _atomic_json(checkpoint_root / "index.json", checkpoint_index)
    print(json.dumps(final_summary, ensure_ascii=False, indent=2), flush=True)
    return final_summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-json", default="")
    parser.add_argument("--output-jsonl", default="")
    parser.add_argument("--tests-only-json", default="")
    parser.add_argument("--summary-json", default="")
    return parser


def main() -> int:
    export_run(build_parser().parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
