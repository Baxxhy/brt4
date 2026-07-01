#!/usr/bin/env python3
"""Read-only progress monitor for an organized BRT3 run directory."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def phase(run_dir: Path) -> str:
    if (run_dir / "export.done").is_file():
        return "完成"
    if (run_dir / "evaluation.done").is_file():
        return "导出"
    if (run_dir / "generation.done").is_file():
        return "正式评测"
    return "BRT 生成"


def snapshot(run_dir: Path) -> str:
    generation = run_dir / "generation"
    evaluation = run_dir / "evaluation"
    statuses: Counter[str] = Counter()
    recent_failures: list[tuple[float, str, str, str]] = []
    summaries = 0
    tests = 0
    running = 0
    newest = 0.0
    if generation.is_dir():
        for item in generation.iterdir():
            if not item.is_dir():
                continue
            marker = item / ".running"
            summary_path = item / "summary.json"
            if marker.is_file():
                running += 1
                newest = max(newest, marker.stat().st_mtime)
            if (item / "final_test.py").is_file():
                tests += 1
            if not summary_path.is_file():
                continue
            summaries += 1
            newest = max(newest, summary_path.stat().st_mtime)
            data = read_json(summary_path, {})
            status = str(data.get("status") or "INVALID_SUMMARY")
            statuses[status] += 1
            if status not in {"SURROGATE_F2P_SUCCESS", "ISSUE_ALIGNED_FAIL", "GENERATED"}:
                reason = str(
                    data.get("final_reason")
                    or data.get("notes")
                    or data.get("error")
                    or ""
                ).replace("\n", " ")[:240]
                recent_failures.append(
                    (summary_path.stat().st_mtime, item.name, status, reason)
                )

    formal: dict[str, Any] = {}
    merged = read_json(evaluation / "merged_results.json", None)
    if isinstance(merged, dict):
        formal = merged
    elif evaluation.is_dir():
        for path in sorted(evaluation.glob("worker_*/results.json")):
            data = read_json(path, {})
            if isinstance(data, dict):
                formal.update(data)
                newest = max(newest, path.stat().st_mtime)
    formal_statuses = Counter(
        str(value.get("status") or "UNKNOWN")
        for value in formal.values()
        if isinstance(value, dict)
    )
    config = read_json(run_dir / "run_config.json", {})
    total = int(config.get("instance_count") or 0)
    lines = [
        f"时间: {datetime.now().isoformat(timespec='seconds')}",
        f"阶段: {phase(run_dir)}  目标实例: {total or '未知'}",
        f"生成 summary: {summaries}/{total or '?'}  final_test: {tests}/{total or '?'}  正在运行: {running}",
        "生成状态: " + json.dumps(dict(statuses.most_common()), ensure_ascii=False),
        f"正式评测: {len(formal)}/{total or '?'}",
        "评测状态: " + json.dumps(dict(formal_statuses.most_common()), ensure_ascii=False),
        "最近更新时间: "
        + (datetime.fromtimestamp(newest).isoformat(timespec="seconds") if newest else "无"),
        "最近失败原因:",
    ]
    for _, instance_id, status, reason in sorted(recent_failures, reverse=True)[:10]:
        lines.append(f"  {instance_id} [{status}] {reason}")
    for log_name in ("generation.log", "evaluation.log", "export.log"):
        log_path = run_dir / "logs" / log_name
        if log_path.is_file():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-1:]
            if tail:
                lines.append(f"{log_name} 尾行: {tail[0][:300]}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--log-path", default="")
    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()
    log_path = Path(args.log_path).resolve() if args.log_path else None
    while True:
        report = snapshot(run_dir)
        print(report, flush=True)
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(report + "\n\n")
        if args.once:
            return 0
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
