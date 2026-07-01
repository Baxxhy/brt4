#!/usr/bin/env python3
"""Read-only monitor for BRT3 generation outputs."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path


def snapshot(outputs_dir: Path) -> None:
    directories = [path for path in outputs_dir.iterdir() if path.is_dir() and path.name != "formal_eval"] if outputs_dir.is_dir() else []
    statuses: Counter[str] = Counter()
    failures: list[tuple[float, str, str, str]] = []
    completed = 0
    running = 0
    newest = 0.0
    for directory in directories:
        summary = directory / "summary.json"
        if (directory / ".running").is_file():
            running += 1
            newest = max(newest, (directory / ".running").stat().st_mtime)
            continue
        if not summary.is_file():
            running += 1
            newest = max(newest, directory.stat().st_mtime)
            continue
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            running += 1
            continue
        completed += 1
        status = str(data.get("status") or "UNKNOWN")
        statuses[status] += 1
        mtime = summary.stat().st_mtime
        newest = max(newest, mtime)
        if status not in {"SURROGATE_F2P_SUCCESS", "ISSUE_ALIGNED_FAIL", "GENERATED"}:
            reason = str(data.get("final_reason") or data.get("notes") or data.get("error") or "")
            failures.append((mtime, directory.name, status, reason.replace("\n", " ")[:240]))
    print(f"时间: {datetime.now().isoformat(timespec='seconds')}")
    print(f"已完成: {completed}  正在运行/无 summary: {running}  已发现目录: {len(directories)}")
    print("状态:", json.dumps(dict(statuses.most_common()), ensure_ascii=False))
    print("最近更新时间:", datetime.fromtimestamp(newest).isoformat(timespec="seconds") if newest else "无")
    print("最近失败原因:")
    for _, instance_id, status, reason in sorted(failures, reverse=True)[:10]:
        print(f"  {instance_id} [{status}] {reason}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outputs_dir", required=True)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        snapshot(Path(args.outputs_dir))
        if args.once:
            return
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
