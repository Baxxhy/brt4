#!/usr/bin/env python3
"""Report whether a generation directory has one final_test.py per instance."""

from __future__ import annotations

import argparse
from collections import Counter
import json
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances_path", required=True)
    parser.add_argument("--generation_dir", required=True)
    parser.add_argument("--summary_path", default="")
    parser.add_argument("--fail_incomplete", type=parse_bool, default=True)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    rows = load_rows(Path(args.instances_path))
    if args.limit is not None:
        rows = rows[: max(0, args.limit)]
    generation_dir = Path(args.generation_dir)
    missing: list[str] = []
    generated: list[str] = []
    missing_statuses: Counter[str] = Counter()
    for row in rows:
        instance_id = str(row.get("instance_id") or "")
        instance_dir = generation_dir / instance_id
        if (instance_dir / "final_test.py").is_file():
            generated.append(instance_id)
            continue
        missing.append(instance_id)
        summary = load_json(instance_dir / "summary.json")
        missing_statuses[str(summary.get("status") or "MISSING_SUMMARY")] += 1

    summary = {
        "total": len(rows),
        "generated_final_tests": len(generated),
        "missing_count": len(missing),
        "complete": not missing,
        "missing": missing,
        "missing_statuses": dict(sorted(missing_statuses.items())),
        "generation_dir": str(generation_dir),
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.summary_path:
        path = Path(args.summary_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text, end="")
    if missing and args.fail_incomplete:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
