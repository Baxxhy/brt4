#!/usr/bin/env python3
"""Export independent Legacy and Selector V2 views after P0 generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from brt4.posthoc.selector_v2 import collect_candidates, select_candidate


def parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value}")


def load_instances(path: Path) -> list[str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value if isinstance(value, list) else list(value.values())
    return [
        str(row["instance_id"])
        for row in rows
        if isinstance(row, dict) and row.get("instance_id")
    ]


def write_export(export_dir: Path, records: list[dict[str, Any]]) -> str:
    if export_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing export: {export_dir}")
    export_dir.mkdir(parents=True)
    for record in records:
        source = Path(record["code_path"])
        if not source.is_file():
            continue
        destination = export_dir / record["instance_id"]
        destination.mkdir()
        shutil.copy2(source, destination / "final_test.py")
        generation_instance = Path(str(record.get("generation_instance_dir") or ""))
        for metadata_name in ("host_context.json", "summary.json"):
            metadata_path = generation_instance / metadata_name
            if metadata_path.is_file():
                shutil.copy2(metadata_path, destination / metadata_name)
        worktree = Path(str(record.get("worktree_path") or ""))
        if worktree.is_dir():
            (destination / "worktree").symlink_to(worktree, target_is_directory=True)
        (destination / "selection.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    manifest_path = export_dir / "selection_manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (export_dir / "manifest.sha256").write_text(
        f"{digest}  selection_manifest.jsonl\n", encoding="utf-8"
    )
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run_dir", type=Path, required=True)
    parser.add_argument("--instances_path", type=Path, required=True)
    parser.add_argument("--enable_selector_v2_posthoc", type=parse_bool, default=True)
    parser.add_argument("--selector_v2_fallback_to_legacy", type=parse_bool, default=True)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    generation_dir = run_dir / "generation"
    if not (run_dir / "generation.done").is_file():
        raise RuntimeError("P0 generation is not frozen: generation.done is missing")
    instance_ids = load_instances(args.instances_path.resolve())
    missing_summaries = [
        instance_id
        for instance_id in instance_ids
        if not (generation_dir / instance_id / "summary.json").is_file()
    ]
    if missing_summaries:
        preview = ", ".join(missing_summaries[:5])
        raise RuntimeError(
            f"P0 generation is incomplete for {len(missing_summaries)} instances: {preview}"
        )

    legacy_records: list[dict[str, Any]] = []
    selector_records: list[dict[str, Any]] = []
    for instance_id in instance_ids:
        candidates, legacy = collect_candidates(generation_dir / instance_id)
        if legacy is None:
            continue
        legacy_records.append(
            {
                "instance_id": instance_id,
                "selector_version": "legacy",
                "candidate_id": legacy.candidate_id,
                "code_path": str(legacy.code_path),
                "selection_reason": "P0 Legacy final selection",
                "rank_key": list(legacy.rank_key),
                "fallback_reason": "",
                "code_hash": legacy.code_hash,
                "worktree_path": str(generation_dir / instance_id / "worktree"),
                "generation_instance_dir": str(generation_dir / instance_id),
            }
        )
        if args.enable_selector_v2_posthoc:
            selected, reason, fallback, ranked = select_candidate(
                candidates, legacy, args.selector_v2_fallback_to_legacy
            )
        else:
            selected = legacy
            reason = "Selector V2 disabled; retained Legacy"
            fallback = True
            ranked = candidates
        selector_records.append(
            {
                "instance_id": instance_id,
                "selector_version": "selector_v2_posthoc",
                "candidate_id": selected.candidate_id,
                "code_path": str(selected.code_path),
                "selection_reason": reason,
                "rank_key": list(selected.rank_key),
                "fallback_reason": reason if fallback else "",
                "fallback_to_legacy": fallback,
                "legacy_candidate_id": legacy.candidate_id,
                "changed_from_legacy": selected.code_hash != legacy.code_hash,
                "code_hash": selected.code_hash,
                "candidate_count_unique": len(candidates),
                "ranked_candidates": [item.manifest_record() for item in ranked],
                "worktree_path": str(generation_dir / instance_id / "worktree"),
                "generation_instance_dir": str(generation_dir / instance_id),
            }
        )

    exports = run_dir / "exports"
    legacy_dir = exports / "legacy_selection"
    selector_dir = exports / "selector_v2_selection"
    existing = [path for path in (legacy_dir, selector_dir) if path.exists()]
    if existing:
        raise FileExistsError(
            "refusing to overwrite existing exports: " + ", ".join(str(path) for path in existing)
        )
    legacy_hash = write_export(legacy_dir, legacy_records)
    selector_hash = write_export(selector_dir, selector_records)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generation_dir": str(generation_dir),
        "instances_total": len(instance_ids),
        "legacy_exported": len(legacy_records),
        "selector_v2_exported": len(selector_records),
        "selector_v2_changed_from_legacy": sum(
            bool(row["changed_from_legacy"]) for row in selector_records
        ),
        "enable_selector_v2_posthoc": args.enable_selector_v2_posthoc,
        "selector_v2_fallback_to_legacy": args.selector_v2_fallback_to_legacy,
        "legacy_manifest_sha256": legacy_hash,
        "selector_v2_manifest_sha256": selector_hash,
    }
    (run_dir / "posthoc_selection_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
