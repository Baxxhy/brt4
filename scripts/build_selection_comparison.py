#!/usr/bin/env python3
"""Combine Legacy and Selector V2 F2P and patch-coverage metrics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def error_count(statuses: dict[str, Any]) -> int:
    return sum(
        int(count or 0)
        for status, count in statuses.items()
        if status not in {"F2P_SUCCESS", "FIXED_FAIL", "BUGGY_PASS"}
    )


def selector_summary(formal: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    statuses = formal.get("by_status") if isinstance(formal.get("by_status"), dict) else {}
    return {
        "F2P_SUCCESS": int(formal.get("f2p_success") or 0),
        "F2P_at_1": float(formal.get("f2p_at_1") or 0.0),
        "FIXED_FAIL": int(statuses.get("FIXED_FAIL") or 0),
        "BUGGY_PASS": int(statuses.get("BUGGY_PASS") or 0),
        "ERROR": error_count(statuses),
        "formal_status_counts": statuses,
        "Patch_Coverage_at_1": float(coverage.get("patch_coverage_at_1") or 0.0),
        "Patch_Coverage_at_1_dataset_total": float(
            coverage.get("patch_coverage_at_1_dataset_total") or 0.0
        ),
        "patch_line_coverage": float(coverage.get("patch_line_coverage") or 0.0),
        "target_lines": int(coverage.get("target_lines") or 0),
        "covered_target_lines": int(coverage.get("covered_target_lines") or 0),
        "coverage_failed_instances": int(coverage.get("coverage_failed_instances") or 0),
        "coverage_status_counts": coverage.get("coverage_status_counts") or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run_dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    legacy_formal = load(run_dir / "evaluation/formal_legacy_276/metrics.json")
    selector_formal = load(run_dir / "evaluation/formal_selector_v2_276/metrics.json")
    legacy_coverage = load(run_dir / "evaluation/patch_coverage_legacy_276/metrics.json")
    selector_coverage = load(run_dir / "evaluation/patch_coverage_selector_v2_276/metrics.json")
    legacy_results = load(run_dir / "evaluation/formal_legacy_276/merged_results.json")
    selector_results = load(run_dir / "evaluation/formal_selector_v2_276/merged_results.json")
    legacy_cov_results = legacy_coverage.get("per_instance_results") or {}
    selector_cov_results = selector_coverage.get("per_instance_results") or {}
    all_ids = sorted(set(legacy_results) | set(selector_results))
    rescued = [
        instance_id
        for instance_id in all_ids
        if legacy_results.get(instance_id, {}).get("status") != "F2P_SUCCESS"
        and selector_results.get(instance_id, {}).get("status") == "F2P_SUCCESS"
    ]
    lost = [
        instance_id
        for instance_id in all_ids
        if legacy_results.get(instance_id, {}).get("status") == "F2P_SUCCESS"
        and selector_results.get(instance_id, {}).get("status") != "F2P_SUCCESS"
    ]
    coverage_changes = [
        {
            "instance_id": instance_id,
            "legacy_status": legacy_cov_results.get(instance_id, {}).get("coverage_status", "MISSING"),
            "selector_v2_status": selector_cov_results.get(instance_id, {}).get(
                "coverage_status", "MISSING"
            ),
        }
        for instance_id in sorted(set(legacy_cov_results) | set(selector_cov_results))
        if legacy_cov_results.get(instance_id, {}).get("coverage_status")
        != selector_cov_results.get(instance_id, {}).get("coverage_status")
    ]
    legacy = selector_summary(legacy_formal, legacy_coverage)
    selector = selector_summary(selector_formal, selector_coverage)
    absolute = selector["Patch_Coverage_at_1"] - legacy["Patch_Coverage_at_1"]
    relative = absolute / legacy["Patch_Coverage_at_1"] if legacy["Patch_Coverage_at_1"] else None
    summary = {
        "total_instances": 276,
        "legacy": legacy,
        "selector_v2": selector,
        "F2P_rescued": len(rescued),
        "F2P_lost": len(lost),
        "F2P_net_gain": len(rescued) - len(lost),
        "rescued_instances": rescued,
        "lost_instances": lost,
        "Patch_Coverage_absolute_difference": absolute,
        "Patch_Coverage_relative_difference": relative,
        "coverage_status_change_count": len(coverage_changes),
        "coverage_status_changes": coverage_changes,
        "selection_manifests": load(run_dir / "selection_manifests_frozen.json"),
    }
    (run_dir / "comparison_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
