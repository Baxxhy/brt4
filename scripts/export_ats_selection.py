#!/usr/bin/env python3
"""Freeze ATS-BRT final-test selection before formal evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [
            {**item, "instance_id": item.get("instance_id", key)}
            for key, item in data.items()
            if isinstance(item, dict)
        ]
    raise ValueError(f"unsupported dataset shape: {path}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_revision(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run_dir", required=True)
    parser.add_argument("--instances_path", required=True)
    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()
    project_root = Path(__file__).resolve().parents[1]
    generation_dir = run_dir / "generation"
    export_dir = run_dir / "exports" / "selector_v2_selection"
    if export_dir.exists():
        raise FileExistsError(f"refusing to overwrite frozen export: {export_dir}")
    export_dir.mkdir(parents=True)
    rows: list[dict[str, Any]] = []
    for issue in load_rows(Path(args.instances_path).resolve()):
        instance_id = str(issue.get("instance_id") or "")
        source_dir = generation_dir / instance_id
        source_test = source_dir / "final_test.py"
        ranking = load_json(source_dir / "candidate_ranking.json")
        summary = load_json(source_dir / "summary.json")
        target_dir = export_dir / instance_id
        target_dir.mkdir(parents=True)
        manifest = {
            "instance_id": instance_id,
            "repo": str(issue.get("repo") or ""),
            "selector_version": "selector_v2",
            "selected_candidate_id": str(
                (ranking.get("selector_v2") or {}).get("selected_candidate_id")
                if isinstance(ranking.get("selector_v2"), dict)
                else ""
            ),
            "selected_attempt": ranking.get("selected_attempt"),
            "selection_reason": str(ranking.get("selection_policy") or ""),
            "status": "MISSING_GENERATED_TEST",
            "source_final_test": str(source_test),
            "exported_final_test": "",
            "code_hash": "",
            "uses_counterfactual_fields": False,
        }
        if source_test.is_file():
            target_test = target_dir / "final_test.py"
            shutil.copy2(source_test, target_test)
            for name in (
                "summary.json",
                "host_context.json",
                "behavior_target.json",
                "protocol_recovery.json",
                "candidate_ranking.json",
                "candidate_archive.json",
                "adaptive_search_trace.json",
                "selector_v2_ranking.json",
            ):
                source = source_dir / name
                if source.is_file():
                    shutil.copy2(source, target_dir / name)
            manifest.update(
                {
                    "status": "EXPORTED",
                    "exported_final_test": str(target_test),
                    "code_hash": sha256(target_test),
                    "generation_status": str(summary.get("status") or "UNKNOWN"),
                }
            )
        (target_dir / "selection.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        rows.append(manifest)
    manifest_path = export_dir / "selection_manifest.jsonl"
    manifest_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows),
        encoding="utf-8",
    )
    metadata = {
        "selector_version": "selector_v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_path": str(Path(args.instances_path).resolve()),
        "dataset_total": len(rows),
        "exported": sum(item["status"] == "EXPORTED" for item in rows),
        "missing": sum(item["status"] != "EXPORTED" for item in rows),
        "manifest_sha256": sha256(manifest_path),
        "selector_code_sha256": sha256(project_root / "generation" / "adaptive_search.py"),
        "git_revision": git_revision(project_root),
        "golden_inputs_read": False,
        "counterfactual_fields_used": False,
    }
    (export_dir / "manifest_meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
