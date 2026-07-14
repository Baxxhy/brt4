#!/usr/bin/env python3
"""Freeze and fingerprint both selection manifests before evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run_dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    evaluation_dir = run_dir / "evaluation"
    if evaluation_dir.exists() and any(evaluation_dir.iterdir()):
        raise RuntimeError("refusing to freeze manifests after evaluation outputs exist")
    records = {}
    for name in ("legacy_selection", "selector_v2_selection"):
        manifest = run_dir / "exports" / name / "selection_manifest.jsonl"
        declared = run_dir / "exports" / name / "manifest.sha256"
        if not manifest.is_file() or not declared.is_file():
            raise FileNotFoundError(f"selection manifest is incomplete: {name}")
        actual = digest(manifest)
        expected = declared.read_text(encoding="utf-8").split()[0]
        if actual != expected:
            raise RuntimeError(f"manifest checksum mismatch: {name}")
        records[name] = {
            "manifest": str(manifest),
            "sha256": actual,
            "line_count": sum(1 for line in manifest.read_text(encoding="utf-8").splitlines() if line),
        }
    frozen = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_started": False,
        "manifests": records,
    }
    output = run_dir / "selection_manifests_frozen.json"
    if output.exists():
        raise FileExistsError(f"refusing to overwrite frozen manifest record: {output}")
    output.write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
