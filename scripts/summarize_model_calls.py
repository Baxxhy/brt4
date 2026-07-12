#!/usr/bin/env python3
"""Summarize model-call counts by project and instance."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any


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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def artifact_response_paths(instance_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for path in instance_dir.rglob("*.txt"):
        parts = set(path.relative_to(instance_dir).parts)
        if "worktree" in parts:
            continue
        if path.parent.name == "responses" or path.name.startswith("response"):
            paths.append(path)
    return sorted(paths)


def summarize_from_audit(
    events: list[dict[str, Any]],
    instance_to_repo: dict[str, str],
) -> dict[str, Any]:
    by_repo: dict[str, Counter[str]] = defaultdict(Counter)
    by_instance: dict[str, Counter[str]] = defaultdict(Counter)
    models: Counter[str] = Counter()
    for event in events:
        status = str(event.get("status") or "unknown")
        instance_id = str(event.get("instance_id") or "")
        repo = str(event.get("repo") or instance_to_repo.get(instance_id) or "unknown")
        model = str(event.get("model") or "unknown")
        by_repo[repo]["http_attempts"] += 1
        by_repo[repo][status] += 1
        by_instance[instance_id or "unknown"]["http_attempts"] += 1
        by_instance[instance_id or "unknown"][status] += 1
        models[model] += 1
    return {
        "counting_basis": "BRT4_MODEL_CALL_LOG HTTP-attempt audit",
        "total_http_attempts": len(events),
        "total_successful_chat_completions": sum(
            1 for event in events if str(event.get("status") or "") == "success"
        ),
        "total_failed_or_non_success_attempts": sum(
            1 for event in events if str(event.get("status") or "") != "success"
        ),
        "by_project": {
            repo: dict(counter)
            for repo, counter in sorted(by_repo.items())
        },
        "by_instance": {
            instance_id: dict(counter)
            for instance_id, counter in sorted(by_instance.items())
        },
        "models": dict(sorted(models.items())),
    }


def summarize_from_artifacts(
    generation_dir: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    by_repo: Counter[str] = Counter()
    by_instance: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    for row in rows:
        instance_id = str(row.get("instance_id") or "")
        repo = str(row.get("repo") or "unknown")
        paths = artifact_response_paths(generation_dir / instance_id)
        count = len(paths)
        by_instance[instance_id] = count
        by_repo[repo] += count
        if count:
            examples[instance_id] = [str(path) for path in paths[:5]]
    return {
        "counting_basis": "response artifact count outside worktree",
        "total_successful_chat_completions": sum(by_instance.values()),
        "by_project": dict(sorted(by_repo.items())),
        "by_instance": dict(sorted(by_instance.items())),
        "example_response_artifacts": examples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run_dir", required=True)
    parser.add_argument("--instances_path", default="data/issues/swt276_issues.json")
    parser.add_argument("--generation_dir", default="")
    parser.add_argument("--model_call_log", default="")
    parser.add_argument("--output_path", default="")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    generation_dir = Path(args.generation_dir).resolve() if args.generation_dir else run_dir / "generation"
    log_path = Path(args.model_call_log).resolve() if args.model_call_log else run_dir / "logs" / "model_calls.jsonl"
    output_path = Path(args.output_path).resolve() if args.output_path else run_dir / "model_call_summary.json"
    rows = load_rows(Path(args.instances_path))
    instance_to_repo = {
        str(row.get("instance_id") or ""): str(row.get("repo") or "unknown")
        for row in rows
    }
    events = read_jsonl(log_path)
    artifact_summary = summarize_from_artifacts(generation_dir, rows)
    if events:
        summary = summarize_from_audit(events, instance_to_repo)
        summary["artifact_cross_check"] = artifact_summary
    else:
        summary = artifact_summary
    summary["run_dir"] = str(run_dir)
    summary["model_call_log"] = str(log_path)
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
