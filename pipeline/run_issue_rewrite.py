"""CLI for BRT3 issue rewrite stage."""

from __future__ import annotations

import argparse
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..core.config import DEFAULT_MAX_TOKENS, DEFAULT_MAX_WORKERS, DEFAULT_TEMPERATURE, DEFAULT_TOP_CODE, DEFAULT_TOP_TESTS
from ..io.io_utils import build_instance_context, load_issue_data
from .issue_rewriter import rewrite_issue
from ..core.llm_client import LLMClient
from ..core.utils import ensure_dir, safe_json_dump


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run BRT3 issue rewrite stage.")
    parser.add_argument("--instances_path", required=True)
    parser.add_argument("--code_retrieval_path", required=True)
    parser.add_argument("--test_retrieval_path", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--model", default="deepseek-v3")
    parser.add_argument("--api_key", default=None)
    parser.add_argument("--base_url", default=None)
    parser.add_argument("--max_workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--top_code", type=int, default=DEFAULT_TOP_CODE)
    parser.add_argument("--top_tests", type=int, default=DEFAULT_TOP_TESTS)
    parser.add_argument("--instance_id", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--max_tokens", type=int, default=DEFAULT_MAX_TOKENS)
    return parser


def run_one(args: argparse.Namespace, instance_id: str, issue_row: dict) -> dict:
    out_dir = Path(args.output_dir) / instance_id
    if args.resume and (out_dir / "behavior_target.json").exists():
        return {"instance_id": instance_id, "status": "SKIP"}
    context = build_instance_context(
        instance_id,
        issue_row,
        args.code_retrieval_path,
        args.test_retrieval_path,
        top_code=args.top_code,
        top_tests=args.top_tests,
    )
    client = LLMClient(
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    try:
        rewrite_issue(context, client, str(out_dir))
        return {"instance_id": instance_id, "status": "OK"}
    except Exception as exc:  # noqa: BLE001
        return {"instance_id": instance_id, "status": "ERROR", "error": str(exc), "traceback": traceback.format_exc()}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    ensure_dir(args.output_dir)
    issues = load_issue_data(args.instances_path)
    ids = [args.instance_id] if args.instance_id else list(issues)
    if args.limit:
        ids = ids[: args.limit]
    results = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {pool.submit(run_one, args, iid, issues[iid]): iid for iid in ids if iid in issues}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(result["instance_id"], result["status"], flush=True)
    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r["status"] == "OK"),
        "skip": sum(1 for r in results if r["status"] == "SKIP"),
        "error": sum(1 for r in results if r["status"] == "ERROR"),
        "results": sorted(results, key=lambda x: x["instance_id"]),
        "defaults": {"max_workers": DEFAULT_MAX_WORKERS, "top_code": DEFAULT_TOP_CODE, "top_tests": DEFAULT_TOP_TESTS},
    }
    safe_json_dump(summary, str(Path(args.output_dir) / "summary.json"))


if __name__ == "__main__":
    main()
