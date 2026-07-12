#!/usr/bin/env python3
"""Export legacy and counterfactual-would-select test sets.

This is an export-only utility. It reads generation artifacts and writes two
evaluation-ready directories without modifying the original generation output.
It does not read formal-evaluation results or any golden patch/test artifact.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


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


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def checkpoint_round(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def checkpoint_by_round(checkpoints: list[dict[str, Any]], round_id: int | None) -> dict[str, Any]:
    if round_id is None:
        return {}
    for item in checkpoints:
        if checkpoint_round(item.get("round_id")) == round_id:
            return item
    return {}


def find_checkpoint(checkpoints: list[dict[str, Any]], field: str) -> dict[str, Any]:
    for item in checkpoints:
        if item.get(field) is True:
            return item
    return {}


def resolve_code_path(instance_dir: Path, checkpoint: dict[str, Any]) -> Path | None:
    raw_path = str(checkpoint.get("code_path") or checkpoint.get("candidate_file_path") or "")
    if raw_path:
        path = Path(raw_path)
        if not path.is_absolute():
            path = instance_dir / path
        if path.is_file():
            return path
    round_id = checkpoint_round(checkpoint.get("round_id"))
    if round_id is not None:
        candidate = instance_dir / "checkpoints" / f"candidate_attempt_{round_id}.py"
        if candidate.is_file():
            return candidate
    return None


def copy_metadata(instance_dir: Path, target_dir: Path) -> None:
    for name in (
        "host_context.json",
        "summary.json",
        "behavior_target.json",
        "protocol_recovery.json",
        "candidate_ranking.json",
        "counterfactual_summary.json",
        "seed_attempts_summary.json",
        "selected_seed_summary.json",
    ):
        src = instance_dir / name
        if src.is_file():
            shutil.copy2(src, target_dir / name)


def prepare_instance_dir(target_dir: Path) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)


def export_one(
    row: dict[str, Any],
    generation_dir: Path,
    legacy_dir: Path,
    counterfactual_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    instance_id = str(row.get("instance_id") or "")
    instance_dir = generation_dir / instance_id
    final_path = instance_dir / "final_test.py"
    ranking = load_json(instance_dir / "candidate_ranking.json")
    checkpoints = ranking.get("checkpoints")
    if not isinstance(checkpoints, list):
        checkpoints = []
    checkpoints = [item for item in checkpoints if isinstance(item, dict)]

    legacy_round = checkpoint_round(ranking.get("legacy_selected_attempt"))
    cf_round = checkpoint_round(ranking.get("counterfactual_would_select_attempt"))
    selected_round = checkpoint_round(ranking.get("selected_attempt"))
    legacy_checkpoint = (
        checkpoint_by_round(checkpoints, legacy_round)
        or find_checkpoint(checkpoints, "legacy_selected")
        or checkpoint_by_round(checkpoints, selected_round)
        or find_checkpoint(checkpoints, "selected")
    )
    cf_checkpoint = (
        checkpoint_by_round(checkpoints, cf_round)
        or find_checkpoint(checkpoints, "counterfactual_would_select")
    )

    base_manifest = {
        "instance_id": instance_id,
        "repo": row.get("repo", ""),
        "ranking_path": str(instance_dir / "candidate_ranking.json"),
        "ranking_found": bool(ranking),
        "legacy_selected_attempt": legacy_checkpoint.get("round_id"),
        "counterfactual_would_select_attempt": cf_checkpoint.get("round_id"),
        "ranking_changed_in_shadow": bool(ranking.get("ranking_changed_in_shadow")),
        "selection_changed_by_counterfactual": bool(
            ranking.get("selection_changed_by_counterfactual")
        ),
    }

    legacy_target = legacy_dir / instance_id
    cf_target = counterfactual_dir / instance_id
    prepare_instance_dir(legacy_target)
    prepare_instance_dir(cf_target)
    copy_metadata(instance_dir, legacy_target)
    copy_metadata(instance_dir, cf_target)

    if not final_path.is_file():
        missing = {
            **base_manifest,
            "status": "MISSING_GENERATED_TEST",
            "source_final_test": str(final_path),
            "exported_final_test": "",
            "fallback_used": True,
        }
        return ({**missing, "selection": "legacy"}, {**missing, "selection": "counterfactual"})

    shutil.copy2(final_path, legacy_target / "final_test.py")
    legacy_manifest = {
        **base_manifest,
        "selection": "legacy",
        "status": "EXPORTED",
        "source_final_test": str(final_path),
        "exported_final_test": str(legacy_target / "final_test.py"),
        "fallback_used": False,
        "selection_reason": "legacy P0 final_test.py",
    }

    cf_source = resolve_code_path(instance_dir, cf_checkpoint)
    fallback_used = False
    reason = "counterfactual evidence rank would-select checkpoint"
    if cf_source is None:
        cf_source = final_path
        fallback_used = True
        reason = "missing or unknown counterfactual checkpoint; fell back to legacy final_test.py"
    shutil.copy2(cf_source, cf_target / "final_test.py")
    cf_manifest = {
        **base_manifest,
        "selection": "counterfactual",
        "status": "EXPORTED",
        "source_final_test": str(cf_source),
        "exported_final_test": str(cf_target / "final_test.py"),
        "fallback_used": fallback_used,
        "selection_reason": reason,
    }
    (legacy_target / "selection.json").write_text(
        json.dumps(legacy_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (cf_target / "selection.json").write_text(
        json.dumps(cf_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return legacy_manifest, cf_manifest


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run_dir", required=True)
    parser.add_argument("--instances_path", default="data/issues/swt276_issues.json")
    parser.add_argument("--generation_dir", default="")
    parser.add_argument("--legacy_dir", default="")
    parser.add_argument("--counterfactual_dir", default="")
    parser.add_argument("--require_complete", type=parse_bool, default=True)
    parser.add_argument("--touch_done", type=parse_bool, default=True)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    generation_dir = Path(args.generation_dir).resolve() if args.generation_dir else run_dir / "generation"
    legacy_dir = Path(args.legacy_dir).resolve() if args.legacy_dir else run_dir / "exports" / "legacy_selection"
    counterfactual_dir = (
        Path(args.counterfactual_dir).resolve()
        if args.counterfactual_dir
        else run_dir / "exports" / "counterfactual_selection"
    )
    rows = load_rows(Path(args.instances_path))
    legacy_dir.mkdir(parents=True, exist_ok=True)
    counterfactual_dir.mkdir(parents=True, exist_ok=True)

    legacy_manifest: list[dict[str, Any]] = []
    cf_manifest: list[dict[str, Any]] = []
    for row in rows:
        legacy_item, cf_item = export_one(row, generation_dir, legacy_dir, counterfactual_dir)
        legacy_manifest.append(legacy_item)
        cf_manifest.append(cf_item)

    write_jsonl(legacy_dir / "selection_manifest.jsonl", legacy_manifest)
    write_jsonl(counterfactual_dir / "selection_manifest.jsonl", cf_manifest)
    combined = [
        {"export": "legacy", **row}
        for row in legacy_manifest
    ] + [
        {"export": "counterfactual", **row}
        for row in cf_manifest
    ]
    write_jsonl(run_dir / "exports" / "selection_manifest.jsonl", combined)

    summary = {
        "total": len(rows),
        "legacy_exported": sum(1 for row in legacy_manifest if row["status"] == "EXPORTED"),
        "counterfactual_exported": sum(1 for row in cf_manifest if row["status"] == "EXPORTED"),
        "counterfactual_fallbacks": sum(1 for row in cf_manifest if row.get("fallback_used")),
        "ranking_changed_in_shadow": sum(
            1 for row in cf_manifest if row.get("ranking_changed_in_shadow")
        ),
        "missing_generated": [
            row["instance_id"] for row in legacy_manifest if row["status"] != "EXPORTED"
        ],
        "legacy_dir": str(legacy_dir),
        "counterfactual_dir": str(counterfactual_dir),
    }
    (run_dir / "exports" / "export_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if summary["missing_generated"]:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if args.require_complete else 0
    if args.touch_done:
        (run_dir / "legacy_export.done").write_text("", encoding="utf-8")
        (run_dir / "counterfactual_export.done").write_text("", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
