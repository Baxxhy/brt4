"""Full BRT3 pipeline CLI."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import json
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..core.config import (
    DEFAULT_COUNTERFACTUAL_SHADOW_MODE,
    DEFAULT_ENABLE_ADAPTIVE_TYPED_SEARCH,
    DEFAULT_ENABLE_BIDIRECTIONAL_COUNTERFACTUAL_VALIDATION,
    DEFAULT_ENABLE_CONTRASTIVE_OBSERVATION_ORACLE,
    DEFAULT_ENABLE_NEGATIVE_CONTROL,
    DEFAULT_MAX_EXTRA_UNIQUE_CANDIDATES,
    DEFAULT_MAX_FEEDBACK_ROUNDS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_WORKERS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
    DEFAULT_TOP_CODE,
    DEFAULT_TOP_TESTS,
)
from ..execution.feedback import run_instance_pipeline
from ..io.io_utils import build_instance_context, load_issue_data
from ..llm.call_logger import model_call_context
from ..llm.llm_client import LLMClient
from ..core.utils import ensure_dir, safe_json_dump
from ..runtime.conda_env_manager import default_env_name


_CONDA_ENV_LOCKS: dict[str, threading.Lock] = {}
_CONDA_ENV_LOCKS_GUARD = threading.Lock()


def _conda_env_lock(env_name: str) -> threading.Lock:
    """Serialize installs and tests that mutate the same conda environment."""
    key = env_name or "__direct_host__"
    with _CONDA_ENV_LOCKS_GUARD:
        return _CONDA_ENV_LOCKS.setdefault(key, threading.Lock())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run full BRT3 post-processing BRT pipeline.")
    parser.add_argument("--instances_path", required=True)
    parser.add_argument("--code_retrieval_path", required=True)
    parser.add_argument("--test_retrieval_path", required=True)
    parser.add_argument("--repo_root_base", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--model", default="deepseek-v3")
    parser.add_argument("--api_key", default=None)
    parser.add_argument("--base_url", default=None)
    parser.add_argument("--conda_env", default="")
    parser.add_argument("--max_workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--max_feedback_rounds", type=int, default=DEFAULT_MAX_FEEDBACK_ROUNDS)
    parser.add_argument("--max_env_rounds", type=int, default=None)
    parser.add_argument("--max_brt_rounds", type=int, default=None)
    parser.add_argument("--max_patch_rounds", type=int, default=3)
    parser.add_argument("--num_candidates", type=int, default=1)
    parser.add_argument("--instance_id", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--top_code", type=int, default=DEFAULT_TOP_CODE)
    parser.add_argument("--top_tests", type=int, default=DEFAULT_TOP_TESTS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--no_conda", action="store_true")
    parser.add_argument(
        "--validation_mode",
        choices=["buggy_only", "surrogate_patch"],
        default="surrogate_patch",
        help="surrogate_patch generates an independent temporary source fix; no golden patch is loaded.",
    )
    parser.add_argument("--generate_only", action="store_true", help="Only rewrite issue, recover static host context, and generate one test; do not execute pytest.")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--max_tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--enable_protocol_recovery", type=_parse_bool, default=True)
    parser.add_argument("--enable_seed_mutation", type=_parse_bool, default=True)
    parser.add_argument("--enable_observation_oracle", type=_parse_bool, default=True)
    parser.add_argument("--enable_strict_semantic_verifier", type=_parse_bool, default=True)
    parser.add_argument("--counterfactual_shadow_mode", type=_parse_bool, default=DEFAULT_COUNTERFACTUAL_SHADOW_MODE)
    parser.add_argument("--enable_bidirectional_counterfactual_validation", type=_parse_bool, default=DEFAULT_ENABLE_BIDIRECTIONAL_COUNTERFACTUAL_VALIDATION)
    parser.add_argument("--enable_negative_control", type=_parse_bool, default=DEFAULT_ENABLE_NEGATIVE_CONTROL)
    parser.add_argument("--max_negative_control_attempts", type=int, default=1)
    parser.add_argument("--max_negative_control_ast_edits", type=int, default=1)
    parser.add_argument("--enable_runtime_target_reachability", type=_parse_bool, default=True)
    parser.add_argument("--enable_contrastive_observation_oracle", type=_parse_bool, default=DEFAULT_ENABLE_CONTRASTIVE_OBSERVATION_ORACLE)
    parser.add_argument("--enable_counterfactual_repair_branch", type=_parse_bool, default=False)
    parser.add_argument("--min_valid_surrogate_patches_for_consensus", type=int, default=2)
    parser.add_argument("--surrogate_consensus_threshold", type=float, default=0.67)
    parser.add_argument(
        "--counterfactual_evidence_mode",
        choices=["soft", "strict"],
        default="soft",
    )
    parser.add_argument("--enable_adaptive_typed_search", type=_parse_bool, default=DEFAULT_ENABLE_ADAPTIVE_TYPED_SEARCH)
    parser.add_argument("--enable_structured_observation_extractor", type=_parse_bool, default=True)
    parser.add_argument("--enable_minimal_oracle_search", type=_parse_bool, default=True)
    parser.add_argument("--enable_trigger_search", type=_parse_bool, default=True)
    parser.add_argument("--enable_duplicate_aware_archive", type=_parse_bool, default=True)
    parser.add_argument("--enable_optional_recomposition", type=_parse_bool, default=True)
    parser.add_argument("--enable_selector_v2", type=_parse_bool, default=True)
    parser.add_argument("--max_extra_unique_candidates", type=int, default=DEFAULT_MAX_EXTRA_UNIQUE_CANDIDATES)
    parser.add_argument("--max_trigger_search_candidates", type=int, default=2)
    parser.add_argument("--max_minimal_oracle_candidates", type=int, default=2)
    parser.add_argument("--max_protocol_repair_candidates", type=int, default=1)
    parser.add_argument("--max_recomposition_candidates", type=int, default=1)
    return parser


def _parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


def _default_env_name(issue_row: dict) -> str:
    return default_env_name(issue_row, prefix=os.environ.get("BRT4_CONDA_ENV_PREFIX"))


def _interleave_by_conda_env(
    instance_ids: list[str], issues: dict[str, dict]
) -> list[str]:
    """Keep workers busy by avoiding adjacent tasks that share one env lock."""
    buckets: dict[str, deque[str]] = defaultdict(deque)
    env_order: list[str] = []
    for instance_id in instance_ids:
        env_name = _default_env_name(issues.get(instance_id, {})) or instance_id
        if env_name not in buckets:
            env_order.append(env_name)
        buckets[env_name].append(instance_id)
    scheduled: list[str] = []
    while len(scheduled) < len(instance_ids):
        for env_name in env_order:
            if buckets[env_name]:
                scheduled.append(buckets[env_name].popleft())
    return scheduled


def _run_one(args: argparse.Namespace, instance_id: str, issue_row: dict) -> dict:
    out_dir = Path(args.output_dir) / instance_id
    summary_path = out_dir / "summary.json"
    if args.resume and summary_path.exists():
        try:
            previous_status = json.loads(summary_path.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError, TypeError):
            previous_status = "ERROR"
        if previous_status not in {"ERROR", "SETUP_ERROR", "ENV_UNRESOLVED"}:
            return {"instance_id": instance_id, "status": "SKIP"}
    if args.num_candidates != 1:
        raise ValueError("BRT3 supports exactly --num_candidates 1")
    context = build_instance_context(
        instance_id,
        issue_row,
        args.code_retrieval_path,
        args.test_retrieval_path,
        args.repo_root_base,
        args.top_code,
        args.top_tests,
    )
    client = LLMClient(
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    ensure_dir(out_dir)
    running_marker = out_dir / ".running"
    running_marker.write_text(
        json.dumps({"instance_id": instance_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        conda_env = args.conda_env or _default_env_name(issue_row)
        # Editable installs and compiled extensions are environment-global. Two
        # instances sharing an env must not prepare or execute concurrently.
        with model_call_context(
            instance_id=instance_id,
            repo=str(issue_row.get("repo") or ""),
        ), _conda_env_lock(conda_env if not args.no_conda else ""):
            result = run_instance_pipeline(
                context,
                client,
                str(out_dir),
                conda_env=conda_env,
                timeout=args.timeout,
                no_conda=args.no_conda,
                max_feedback_rounds=args.max_feedback_rounds,
                max_env_rounds=args.max_env_rounds,
                max_brt_rounds=args.max_brt_rounds,
                max_patch_rounds=args.max_patch_rounds,
                validation_mode=args.validation_mode,
                generate_only=args.generate_only,
                enable_protocol_recovery=args.enable_protocol_recovery,
                enable_seed_mutation=args.enable_seed_mutation,
                enable_observation_oracle=args.enable_observation_oracle,
                enable_strict_semantic_verifier=args.enable_strict_semantic_verifier,
                counterfactual_shadow_mode=args.counterfactual_shadow_mode,
                enable_bidirectional_counterfactual_validation=args.enable_bidirectional_counterfactual_validation,
                enable_negative_control=args.enable_negative_control,
                max_negative_control_attempts=args.max_negative_control_attempts,
                max_negative_control_ast_edits=args.max_negative_control_ast_edits,
                enable_runtime_target_reachability=args.enable_runtime_target_reachability,
                enable_contrastive_observation_oracle=args.enable_contrastive_observation_oracle,
                min_valid_surrogate_patches_for_consensus=args.min_valid_surrogate_patches_for_consensus,
                surrogate_consensus_threshold=args.surrogate_consensus_threshold,
                counterfactual_evidence_mode=args.counterfactual_evidence_mode,
                enable_adaptive_typed_search=args.enable_adaptive_typed_search,
                enable_structured_observation_extractor=args.enable_structured_observation_extractor,
                enable_minimal_oracle_search=args.enable_minimal_oracle_search,
                enable_trigger_search=args.enable_trigger_search,
                enable_duplicate_aware_archive=args.enable_duplicate_aware_archive,
                enable_optional_recomposition=args.enable_optional_recomposition,
                enable_selector_v2=args.enable_selector_v2,
                max_extra_unique_candidates=args.max_extra_unique_candidates,
                max_trigger_search_candidates=args.max_trigger_search_candidates,
                max_minimal_oracle_candidates=args.max_minimal_oracle_candidates,
                max_protocol_repair_candidates=args.max_protocol_repair_candidates,
                max_recomposition_candidates=args.max_recomposition_candidates,
            )
        return {"instance_id": instance_id, "status": result.status, "summary": result.to_dict()}
    except Exception as exc:  # noqa: BLE001
        ensure_dir(out_dir)
        err = {
            "instance_id": instance_id,
            "status": "ERROR",
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "protocol_recovery_enabled": args.enable_protocol_recovery,
            "seed_mutation_enabled": args.enable_seed_mutation,
            "observation_oracle_enabled": args.enable_observation_oracle,
            "strict_verifier_enabled": args.enable_strict_semantic_verifier,
            "counterfactual_shadow_mode": args.counterfactual_shadow_mode,
            "enable_bidirectional_counterfactual_validation": args.enable_bidirectional_counterfactual_validation,
            "enable_negative_control": args.enable_negative_control,
            "max_negative_control_attempts": args.max_negative_control_attempts,
            "max_negative_control_ast_edits": args.max_negative_control_ast_edits,
            "enable_runtime_target_reachability": args.enable_runtime_target_reachability,
            "enable_contrastive_observation_oracle": args.enable_contrastive_observation_oracle,
            "enable_counterfactual_repair_branch": args.enable_counterfactual_repair_branch,
            "min_valid_surrogate_patches_for_consensus": args.min_valid_surrogate_patches_for_consensus,
            "surrogate_consensus_threshold": args.surrogate_consensus_threshold,
            "counterfactual_evidence_mode": args.counterfactual_evidence_mode,
            "enable_adaptive_typed_search": args.enable_adaptive_typed_search,
            "enable_structured_observation_extractor": args.enable_structured_observation_extractor,
            "enable_minimal_oracle_search": args.enable_minimal_oracle_search,
            "enable_trigger_search": args.enable_trigger_search,
            "enable_duplicate_aware_archive": args.enable_duplicate_aware_archive,
            "enable_optional_recomposition": args.enable_optional_recomposition,
            "enable_selector_v2": args.enable_selector_v2,
            "max_extra_unique_candidates": args.max_extra_unique_candidates,
            "selected_seed_file": "",
            "selected_seed_name": "",
            "seed_fallback_used": False,
            "mutation_ops": [],
            "oracle_type": "",
            "strict_verifier_decision": "",
            "strict_failure_class": "",
            "oracle_rebound": False,
            "final_reason": str(exc),
        }
        safe_json_dump(err, str(out_dir / "summary.json"))
        return err
    finally:
        try:
            running_marker.unlink()
        except OSError:
            pass


def _load_instance_summary(output_dir: Path, instance_id: str, fallback: dict | None = None) -> dict:
    summary_path = output_dir / instance_id / "summary.json"
    if summary_path.is_file():
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("instance_id", instance_id)
                return data
        except (OSError, ValueError, TypeError):
            pass
    if fallback:
        data = dict(fallback.get("summary") or fallback)
        data.setdefault("instance_id", instance_id)
        return data
    return {"instance_id": instance_id, "status": "MISSING_SUMMARY"}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    ensure_dir(args.output_dir)
    issues = load_issue_data(args.instances_path)
    ids = [args.instance_id] if args.instance_id else list(issues)
    if args.limit:
        ids = ids[: args.limit]
    if not args.instance_id and not args.conda_env:
        ids = _interleave_by_conda_env(ids, issues)
    results = []
    by_id: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {pool.submit(_run_one, args, iid, issues[iid]): iid for iid in ids if iid in issues}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            by_id[result["instance_id"]] = result
            print(result["instance_id"], result["status"], flush=True)
    output_dir = Path(args.output_dir)
    ordered_results = [
        {
            "instance_id": iid,
            "status": _load_instance_summary(output_dir, iid, by_id.get(iid)).get("status", "UNKNOWN"),
            "summary": _load_instance_summary(output_dir, iid, by_id.get(iid)),
        }
        for iid in ids
        if iid in issues
    ]
    summary = {
        "total": len(ordered_results),
        "ok": sum(1 for r in ordered_results if r.get("status") not in {"ERROR", "SKIP", "MISSING_SUMMARY"}),
        "skip": sum(1 for r in results if r.get("status") == "SKIP"),
        "error": sum(1 for r in ordered_results if r.get("status") in {"ERROR", "MISSING_SUMMARY"}),
        "completed_this_invocation": len(results),
        "results": ordered_results,
        "defaults": {
            "max_workers": DEFAULT_MAX_WORKERS,
            "num_candidates": 1,
            "max_feedback_rounds": DEFAULT_MAX_FEEDBACK_ROUNDS,
            "validation_mode": "surrogate_patch",
            "generate_only": False,
            "enable_protocol_recovery": args.enable_protocol_recovery,
            "enable_seed_mutation": args.enable_seed_mutation,
            "enable_observation_oracle": args.enable_observation_oracle,
            "enable_strict_semantic_verifier": args.enable_strict_semantic_verifier,
            "counterfactual_shadow_mode": args.counterfactual_shadow_mode,
            "enable_bidirectional_counterfactual_validation": args.enable_bidirectional_counterfactual_validation,
            "enable_negative_control": args.enable_negative_control,
            "max_negative_control_attempts": args.max_negative_control_attempts,
            "max_negative_control_ast_edits": args.max_negative_control_ast_edits,
            "enable_runtime_target_reachability": args.enable_runtime_target_reachability,
            "enable_contrastive_observation_oracle": args.enable_contrastive_observation_oracle,
            "enable_counterfactual_repair_branch": args.enable_counterfactual_repair_branch,
            "min_valid_surrogate_patches_for_consensus": args.min_valid_surrogate_patches_for_consensus,
            "surrogate_consensus_threshold": args.surrogate_consensus_threshold,
            "counterfactual_evidence_mode": args.counterfactual_evidence_mode,
            "method_name": "ATS-BRT" if args.enable_adaptive_typed_search else "P0",
            "enable_adaptive_typed_search": args.enable_adaptive_typed_search,
            "enable_structured_observation_extractor": args.enable_structured_observation_extractor,
            "enable_minimal_oracle_search": args.enable_minimal_oracle_search,
            "enable_trigger_search": args.enable_trigger_search,
            "enable_duplicate_aware_archive": args.enable_duplicate_aware_archive,
            "enable_optional_recomposition": args.enable_optional_recomposition,
            "enable_selector_v2": args.enable_selector_v2,
            "max_extra_unique_candidates": args.max_extra_unique_candidates,
            "max_trigger_search_candidates": args.max_trigger_search_candidates,
            "max_minimal_oracle_candidates": args.max_minimal_oracle_candidates,
            "max_protocol_repair_candidates": args.max_protocol_repair_candidates,
            "max_recomposition_candidates": args.max_recomposition_candidates,
        },
    }
    safe_json_dump(summary, str(Path(args.output_dir) / "summary.json"))


if __name__ == "__main__":
    main()
