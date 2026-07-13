#!/usr/bin/env python3
"""Compare legacy and counterfactual formal-evaluation outputs."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any


ENV_ERROR_TOKENS = ("SETUP", "COLLECT", "SYNTAX", "TIMEOUT", "ERROR")


def load_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [
            {**value, "instance_id": value.get("instance_id", key)}
            for key, value in data.items()
            if isinstance(value, dict)
        ]
    raise ValueError(f"unsupported dataset shape in {path}")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def status_of(merged: dict[str, Any], instance_id: str) -> str:
    item = merged.get(instance_id)
    if isinstance(item, dict):
        return str(item.get("status") or "UNKNOWN")
    return "MISSING_EVAL_RESULT"


def metrics_from_statuses(statuses: list[str], denominator: int) -> dict[str, Any]:
    counts = Counter(statuses)
    success = counts.get("F2P_SUCCESS", 0)
    env_errors = sum(count for status, count in counts.items() if any(token in status for token in ENV_ERROR_TOKENS))
    return {
        "denominator": denominator,
        "f2p_success": success,
        "f2p_fail": denominator - success,
        "f2p_at_1": success / denominator if denominator else 0,
        "f2p_at_1_percent": round(success / denominator * 100, 4) if denominator else 0,
        "by_status": dict(sorted(counts.items())),
        "env_error_count": env_errors,
        "env_error_percent": round(env_errors / denominator * 100, 4) if denominator else 0,
    }


def status_value(value: Any, default: str = "UNKNOWN") -> str:
    if isinstance(value, dict):
        return str(value.get("status") or default)
    if value in (None, ""):
        return default
    return str(value)


def iter_instance_files(instance_dir: Path, name: str) -> list[Path]:
    paths: list[Path] = []
    if not instance_dir.is_dir():
        return paths
    for path in instance_dir.rglob(name):
        try:
            rel_parts = path.relative_to(instance_dir).parts
        except ValueError:
            continue
        if "worktree" in rel_parts:
            continue
        paths.append(path)
    return sorted(paths)


def counterfactual_coverage(generation_dir: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary_count = 0
    plan_count = 0
    negative_status = Counter()
    trigger_status = Counter()
    repair_status = Counter()
    bidirectional_status = Counter()
    oracle_stability = Counter()
    target_hit = Counter()
    full_matrix_count = 0
    surrogate_paired_runs = 0
    evidence_files = 0
    for row in rows:
        instance_id = str(row.get("instance_id") or "")
        instance_dir = generation_dir / instance_id
        summary_paths = iter_instance_files(instance_dir, "counterfactual_summary.json")
        if summary_paths:
            summary_count += 1
        for path in iter_instance_files(instance_dir, "counterfactual_plan.json"):
            if load_json(path):
                plan_count += 1
                break
        metadata_paths = iter_instance_files(instance_dir, "negative_control_metadata.json")
        if not metadata_paths:
            negative_status["MISSING"] += 1
        else:
            for path in metadata_paths:
                metadata = load_json(path)
                negative_status[status_value(metadata.get("status") if isinstance(metadata, dict) else None)] += 1
        summary = load_json(summary_paths[0]) if summary_paths else {}
        if isinstance(summary, dict):
            trigger_status[status_value(summary.get("trigger_necessity"))] += 1
            repair_status[status_value(summary.get("repair_sufficiency"))] += 1
            bidirectional_status[status_value(summary.get("bidirectional_support"))] += 1
            oracle_stability[status_value(summary.get("oracle_stability"))] += 1
        else:
            trigger_status["UNKNOWN"] += 1
            repair_status["UNKNOWN"] += 1
            bidirectional_status["UNKNOWN"] += 1
            oracle_stability["UNKNOWN"] += 1
        evidence_paths = iter_instance_files(instance_dir, "counterfactual_evidence.json")
        if evidence_paths:
            evidence_files += len(evidence_paths)
        for path in evidence_paths:
            evidence = load_json(path)
            if not isinstance(evidence, dict):
                continue
            positive = evidence.get("positive_buggy")
            negative = evidence.get("negative_buggy")
            surrogate_runs = evidence.get("surrogate_runs")
            if not isinstance(surrogate_runs, list):
                surrogate_runs = []
            paired = [
                run for run in surrogate_runs
                if isinstance(run, dict)
                and isinstance(run.get("positive_result"), dict)
                and isinstance(run.get("negative_result"), dict)
            ]
            surrogate_paired_runs += len(paired)
            if isinstance(positive, dict) and isinstance(negative, dict) and paired:
                full_matrix_count += 1
            for result in [positive, negative]:
                if isinstance(result, dict):
                    target_hit[status_value(result.get("runtime_target_hit"), "unknown")] += 1
            for run in paired:
                for key in ("positive_result", "negative_result"):
                    result = run.get(key)
                    if isinstance(result, dict):
                        target_hit[status_value(result.get("runtime_target_hit"), "unknown")] += 1
    return {
        "instances_with_counterfactual_summary": summary_count,
        "counterfactual_plan_generated": plan_count,
        "negative_control_status": dict(sorted(negative_status.items())),
        "trigger_necessity": dict(sorted(trigger_status.items())),
        "repair_sufficiency": dict(sorted(repair_status.items())),
        "bidirectional_support": dict(sorted(bidirectional_status.items())),
        "oracle_stability": dict(sorted(oracle_stability.items())),
        "target_reachability": dict(sorted(target_hit.items())),
        "counterfactual_evidence_files": evidence_files,
        "complete_2x2_execution_matrix_count": full_matrix_count,
        "surrogate_paired_run_count": surrogate_paired_runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run_dir", required=True)
    parser.add_argument("--instances_path", default="data/issues/swt276_issues.json")
    parser.add_argument("--generation_dir", default="")
    parser.add_argument("--legacy_eval_dir", default="")
    parser.add_argument("--counterfactual_eval_dir", default="")
    parser.add_argument("--output_path", default="")
    parser.add_argument("--touch_done", action="store_true")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    generation_dir = Path(args.generation_dir).resolve() if args.generation_dir else run_dir / "generation"
    legacy_eval_dir = Path(args.legacy_eval_dir).resolve() if args.legacy_eval_dir else run_dir / "evaluation" / "formal_legacy_276"
    cf_eval_dir = (
        Path(args.counterfactual_eval_dir).resolve()
        if args.counterfactual_eval_dir
        else run_dir / "evaluation" / "formal_counterfactual_276"
    )
    output_path = Path(args.output_path).resolve() if args.output_path else run_dir / "comparison_summary.json"
    rows = load_rows(Path(args.instances_path))
    ids = [str(row.get("instance_id") or "") for row in rows]
    denominator = len(ids)
    legacy_merged = load_json(legacy_eval_dir / "merged_results.json")
    cf_merged = load_json(cf_eval_dir / "merged_results.json")
    if not isinstance(legacy_merged, dict):
        legacy_merged = {}
    if not isinstance(cf_merged, dict):
        cf_merged = {}

    legacy_statuses = [status_of(legacy_merged, iid) for iid in ids]
    cf_statuses = [status_of(cf_merged, iid) for iid in ids]
    transitions = Counter(
        f"{legacy_status} -> {cf_status}"
        for legacy_status, cf_status in zip(legacy_statuses, cf_statuses, strict=False)
    )
    per_instance: list[dict[str, Any]] = []
    rescued: list[str] = []
    lost: list[str] = []
    for iid, legacy_status, cf_status in zip(ids, legacy_statuses, cf_statuses, strict=False):
        item = {
            "instance_id": iid,
            "legacy_status": legacy_status,
            "counterfactual_status": cf_status,
            "rescued": legacy_status != "F2P_SUCCESS" and cf_status == "F2P_SUCCESS",
            "lost": legacy_status == "F2P_SUCCESS" and cf_status != "F2P_SUCCESS",
        }
        if item["rescued"]:
            rescued.append(iid)
        if item["lost"]:
            lost.append(iid)
        per_instance.append(item)

    cf_manifest = load_jsonl(run_dir / "exports" / "counterfactual_selection" / "selection_manifest.jsonl")
    by_manifest = {str(row.get("instance_id") or ""): row for row in cf_manifest}
    ranking_changed_same_status = [
        iid for iid, legacy_status, cf_status in zip(ids, legacy_statuses, cf_statuses, strict=False)
        if by_manifest.get(iid, {}).get("ranking_changed_in_shadow") and legacy_status == cf_status
    ]
    cf_fallback_count = sum(1 for row in cf_manifest if row.get("fallback_used"))

    coverage = counterfactual_coverage(generation_dir, rows)
    model_call_summary = load_json(run_dir / "model_call_summary.json")
    summary = {
        "run_dir": str(run_dir),
        "dataset_total": denominator,
        "legacy": metrics_from_statuses(legacy_statuses, denominator),
        "counterfactual": metrics_from_statuses(cf_statuses, denominator),
        "status_transition_matrix": dict(sorted(transitions.items())),
        "rescued": rescued,
        "lost": lost,
        "net_gain": len(rescued) - len(lost),
        "ranking_changed_same_status": ranking_changed_same_status,
        "counterfactual_fallback_count": cf_fallback_count,
        "counterfactual_coverage": coverage,
        "model_call_summary": model_call_summary if isinstance(model_call_summary, dict) else {},
        "env_diagnosis_required": (
            metrics_from_statuses(legacy_statuses, denominator)["env_error_percent"] > 20
            or metrics_from_statuses(cf_statuses, denominator)["env_error_percent"] > 20
        ),
        "paths": {
            "generation_dir": str(generation_dir),
            "legacy_eval_dir": str(legacy_eval_dir),
            "counterfactual_eval_dir": str(cf_eval_dir),
            "legacy_metrics": str(legacy_eval_dir / "metrics.json"),
            "counterfactual_metrics": str(cf_eval_dir / "metrics.json"),
            "legacy_metrics_dataset_total": str(legacy_eval_dir / "metrics_dataset_total.json"),
            "counterfactual_metrics_dataset_total": str(cf_eval_dir / "metrics_dataset_total.json"),
            "legacy_merged": str(legacy_eval_dir / "merged_results.json"),
            "counterfactual_merged": str(cf_eval_dir / "merged_results.json"),
            "legacy_merged_with_missing": str(legacy_eval_dir / "merged_results_with_missing.json"),
            "counterfactual_merged_with_missing": str(cf_eval_dir / "merged_results_with_missing.json"),
        },
    }
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path = run_dir / "comparison_manifest.jsonl"
    manifest_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in per_instance),
        encoding="utf-8",
    )
    if args.touch_done:
        (run_dir / "comparison.done").write_text("", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
