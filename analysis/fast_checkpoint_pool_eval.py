"""Fast grouped checkpoint-pool upper-bound evaluator.

This module is analysis-only. It does not generate candidates, call LLMs, or
write back to generation/ranking artifacts. Golden patches are used only for
offline formal F2P measurement.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any

from ..core.utils import ensure_dir, safe_json_dump, sanitize_instance_id
from ..evaluation.direct_eval import (
    apply_patch_text,
    classify_env_error,
    classify_run,
    fill_patches_from_swebench_lite,
    first_test_selector,
    git_reset_to,
    group_ids_by_repo,
    prepare_eval_clone,
    run_setup_with_fallback,
    run_shell,
    setup_command,
    test_command,
    write_generated_test,
)
from ..io.io_utils import load_issue_data
from ..runtime.conda_env_manager import conda_activate_cmd, preflight_system, resolve_eval_env


F2P_STATUS = "F2P_SUCCESS"
DEFAULT_INSTANCES_PATH = "data/issues/swt276_issues.json"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
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


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _normalized_code_hash(code: str) -> str:
    try:
        parsed = ast.parse(code)
        normalized = ast.dump(parsed, include_attributes=False)
    except SyntaxError:
        lines: list[str] = []
        for raw in code.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            lines.append(re.sub(r"\s+", " ", stripped))
        normalized = "\n".join(lines)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_read_code(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _risk_rank(value: str) -> int:
    return {"LOW": 2, "MEDIUM": 1, "HIGH": 0}.get(str(value or "").upper(), 1)


def _candidate_order_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    checkpoint = candidate.get("checkpoint") if isinstance(candidate.get("checkpoint"), dict) else {}
    evidence_key = checkpoint.get("evidence_rank_key")
    if not isinstance(evidence_key, list):
        evidence_key = []
    padded = [int(item) for item in evidence_key[:12]]
    padded.extend([0] * (12 - len(padded)))
    evidence = checkpoint.get("evidence_rank") if isinstance(checkpoint.get("evidence_rank"), dict) else {}
    surrogate = checkpoint.get("surrogate") if isinstance(checkpoint.get("surrogate"), dict) else {}
    verifier = checkpoint.get("verifier") if isinstance(checkpoint.get("verifier"), dict) else {}
    oracle_risk = checkpoint.get("oracle_risk") if isinstance(checkpoint.get("oracle_risk"), dict) else {}
    surrogate_success = str(surrogate.get("status") or "") in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"}
    semantic_accept = bool(evidence.get("semantic_accept")) or verifier.get("decision") == "accept"
    return (
        *padded,
        int(checkpoint.get("legacy_score") or checkpoint.get("selector_score_after_risk") or checkpoint.get("score") or 0),
        1 if semantic_accept else 0,
        1 if surrogate_success else 0,
        _risk_rank(str(oracle_risk.get("level") or "LOW")),
        -int(candidate.get("round_id") or 0),
    )


def _infer_origin(instance_dir: Path, round_id: int, checkpoint: dict[str, Any], by_round: dict[int, dict[str, Any]]) -> str:
    lineage = checkpoint.get("lineage")
    if isinstance(lineage, dict) and lineage.get("origin"):
        return str(lineage["origin"])
    if round_id == 0:
        return "generation"
    if (instance_dir / f"oracle_round_{round_id}_rebuilt_test.py").is_file():
        if (instance_dir / "contrastive_observation.json").is_file():
            return "contrastive_observation_oracle"
        return "buggy_observation_oracle"
    previous = by_round.get(round_id - 1) or {}
    decision = str((previous.get("verifier") or {}).get("decision") or "")
    if decision == "repair_setup":
        return "repair_setup"
    if decision == "repair_oracle":
        return "repair_oracle"
    if decision == "repair_trigger":
        return "repair_trigger"
    if str((checkpoint.get("execution") or {}).get("status") or "") in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR"}:
        return "repair_setup"
    return "UNKNOWN"


def _iter_rankings(instance_dir: Path) -> list[Path]:
    paths: list[Path] = []
    top_level = instance_dir / "candidate_ranking.json"
    if top_level.is_file():
        paths.append(top_level)
    seed_root = instance_dir / "seed_candidates"
    if seed_root.is_dir():
        paths.extend(sorted(seed_root.glob("seed_*/candidate_ranking.json")))
    return list(dict.fromkeys(paths))


def _enumerate_candidates(run_dir: Path, rows_by_id: dict[str, dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    generation_dir = run_dir / "generation"
    raw_checkpoint_count = 0
    missing_pool: list[str] = []
    by_instance: dict[str, list[dict[str, Any]]] = {}
    origin_counts = Counter()
    duplicate_count = 0
    for instance_id in rows_by_id:
        instance_dir = generation_dir / instance_id
        ranking_paths = _iter_rankings(instance_dir) if instance_dir.is_dir() else []
        seen_hashes: set[str] = set()
        candidates: list[dict[str, Any]] = []
        for ranking_path in ranking_paths:
            ranking = _load_json(ranking_path)
            checkpoints = ranking.get("checkpoints") if isinstance(ranking, dict) else []
            if not isinstance(checkpoints, list):
                continue
            by_round = {
                int(item.get("round_id") or 0): item
                for item in checkpoints
                if isinstance(item, dict)
            }
            ranking_dir = ranking_path.parent
            for checkpoint in checkpoints:
                if not isinstance(checkpoint, dict):
                    continue
                raw_checkpoint_count += 1
                code_path = Path(str(checkpoint.get("code_path") or ""))
                if not code_path.is_absolute():
                    code_path = ranking_dir / code_path
                code = _safe_read_code(code_path)
                if not code:
                    continue
                code_hash = _normalized_code_hash(code)
                if code_hash in seen_hashes:
                    duplicate_count += 1
                    continue
                seen_hashes.add(code_hash)
                round_id = int(checkpoint.get("round_id") or 0)
                origin = _infer_origin(ranking_dir, round_id, checkpoint, by_round)
                origin_counts[origin] += 1
                candidates.append(
                    {
                        "instance_id": instance_id,
                        "candidate_id": f"{ranking_dir.relative_to(instance_dir).as_posix() or '.'}:round_{round_id}:{code_hash[:12]}",
                        "round_id": round_id,
                        "code_path": str(code_path),
                        "code": code,
                        "code_hash": code_hash,
                        "origin": origin,
                        "ranking_path": str(ranking_path),
                        "checkpoint": checkpoint,
                        "legacy_selected": bool(checkpoint.get("legacy_selected")),
                        "counterfactual_would_select": bool(checkpoint.get("counterfactual_would_select")),
                    }
                )
        if candidates:
            candidates.sort(key=_candidate_order_key, reverse=True)
            by_instance[instance_id] = candidates
        else:
            missing_pool.append(instance_id)
    manifest = {
        "raw_checkpoint_count": raw_checkpoint_count,
        "unique_candidate_count": sum(len(items) for items in by_instance.values()),
        "duplicate_candidate_count": duplicate_count,
        "instances_with_candidate_pool": len(by_instance),
        "instances_without_candidate_pool": missing_pool,
        "candidate_count_by_origin": dict(sorted(origin_counts.items())),
    }
    return by_instance, manifest


def _formal_successes(run_dir: Path) -> tuple[set[str], dict[str, Any]]:
    legacy = _load_json(run_dir / "evaluation" / "formal_legacy_276" / "merged_results.json")
    counterfactual = _load_json(run_dir / "evaluation" / "formal_counterfactual_276" / "merged_results.json")
    legacy = legacy if isinstance(legacy, dict) else {}
    counterfactual = counterfactual if isinstance(counterfactual, dict) else {}
    legacy_success = {iid for iid, row in legacy.items() if isinstance(row, dict) and row.get("status") == F2P_STATUS}
    cf_success = {iid for iid, row in counterfactual.items() if isinstance(row, dict) and row.get("status") == F2P_STATUS}
    return legacy_success | cf_success, {
        "legacy_selected_success": len(legacy_success),
        "counterfactual_selected_success": len(cf_success),
        "known_pool_success_count": len(legacy_success | cf_success),
        "known_pool_success_instances": sorted(legacy_success | cf_success),
        "legacy_merged_count": len(legacy),
        "counterfactual_merged_count": len(counterfactual),
    }


def _import_selected_candidate_cache(run_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    evals = {
        "legacy": _load_json(run_dir / "evaluation" / "formal_legacy_276" / "merged_results.json"),
        "counterfactual": _load_json(run_dir / "evaluation" / "formal_counterfactual_276" / "merged_results.json"),
    }
    for selection in ("legacy", "counterfactual"):
        manifest_path = run_dir / "exports" / f"{selection}_selection" / "selection_manifest.jsonl"
        merged = evals.get(selection) if isinstance(evals.get(selection), dict) else {}
        for row in _load_jsonl(manifest_path):
            instance_id = str(row.get("instance_id") or "")
            source = Path(str(row.get("source_final_test") or ""))
            code = _safe_read_code(source)
            result = merged.get(instance_id) if isinstance(merged, dict) else {}
            if not instance_id or not code or not isinstance(result, dict):
                continue
            code_hash = _normalized_code_hash(code)
            cache[(instance_id, code_hash)] = {
                "instance_id": instance_id,
                "candidate_id": f"existing_{selection}_selection",
                "code_hash": code_hash,
                "origin": f"existing_{selection}_selection",
                "buggy_status": str((result.get("buggy") or {}).get("status") or ""),
                "fixed_status": str((result.get("fixed") or {}).get("status") or ""),
                "formal_status": str(result.get("status") or ""),
                "is_f2p": result.get("status") == F2P_STATUS,
                "execution_time_seconds": 0,
                "cached": True,
                "cache_source": f"{selection}_formal_eval",
            }
    return cache


def _candidate_cache_path(output_dir: Path, instance_id: str, code_hash: str) -> Path:
    return output_dir / "candidate_results" / sanitize_instance_id(instance_id) / f"{code_hash}.json"


def _remove_test_file(repo_dir: str, rel_file: str) -> None:
    path = Path(repo_dir) / rel_file
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _prepare_instance_repos(
    issue: dict[str, Any],
    run_dir: Path,
    output_dir: Path,
    repo_root_base: str,
    timeout: int,
) -> tuple[str, str, dict[str, Any]]:
    instance_id = str(issue["instance_id"])
    generation_dir = run_dir / "generation"
    env_resolution = resolve_eval_env(issue, str(generation_dir))
    meta: dict[str, Any] = {
        "env_resolution": env_resolution.to_dict(),
        "requested_env": env_resolution.requested_env,
        "resolved_env": env_resolution.resolved_env,
        "env_exists": env_resolution.env_exists,
        "env_health": env_resolution.env_health,
    }
    if not env_resolution.env_exists or not env_resolution.env_health.get("ok"):
        category = str(env_resolution.env_health.get("category") or "ENV_INCOMPLETE")
        meta.update(
            {
                "status": category,
                "env_error_category": category,
                "error": "; ".join(env_resolution.errors) or str(env_resolution.env_health.get("reason") or ""),
            }
        )
        return "", "", meta
    buggy_dir, buggy_meta = prepare_eval_clone(
        issue,
        repo_root_base,
        str(output_dir / "worktrees" / "buggy"),
    )
    fixed_dir, fixed_meta = prepare_eval_clone(
        issue,
        repo_root_base,
        str(output_dir / "worktrees" / "fixed"),
    )
    meta["buggy_clone"] = buggy_meta
    meta["fixed_clone"] = fixed_meta
    env_name = env_resolution.resolved_env
    setup_full_command = f"{conda_activate_cmd(env_name)} && {setup_command(issue['repo'], issue['version'])}"
    meta["buggy_reset"] = git_reset_to(buggy_dir, issue["base_commit"], clean=True)
    meta["buggy_setup"] = run_setup_with_fallback(setup_full_command, buggy_dir, timeout)
    if meta["buggy_setup"]["returncode"] != 0:
        meta["status"] = "BUGGY_SETUP_ERROR"
        meta["env_error_category"] = classify_env_error(
            (meta["buggy_setup"].get("stdout") or "") + "\n" + (meta["buggy_setup"].get("stderr") or "")
        )
        return buggy_dir, fixed_dir, meta
    meta["fixed_reset"] = git_reset_to(fixed_dir, issue["base_commit"], clean=True)
    patch_result = apply_patch_text(fixed_dir, str(issue.get("patch") or ""))
    if patch_result["returncode"] != 0:
        meta["patch_apply_initial"] = patch_result
        meta["patch_retry_reset"] = git_reset_to(fixed_dir, issue["base_commit"], clean=True)
        patch_result = apply_patch_text(fixed_dir, str(issue.get("patch") or ""))
    meta["patch_apply"] = patch_result
    if patch_result["returncode"] != 0:
        meta["status"] = "PATCH_APPLY_ERROR"
        return buggy_dir, fixed_dir, meta
    meta["fixed_setup"] = run_setup_with_fallback(setup_full_command, fixed_dir, timeout)
    if meta["fixed_setup"]["returncode"] != 0:
        meta["status"] = "FIXED_SETUP_ERROR"
        meta["env_error_category"] = classify_env_error(
            (meta["fixed_setup"].get("stdout") or "") + "\n" + (meta["fixed_setup"].get("stderr") or "")
        )
        return buggy_dir, fixed_dir, meta
    meta["status"] = "PREPARED"
    return buggy_dir, fixed_dir, meta


def _evaluate_candidate(
    issue: dict[str, Any],
    candidate: dict[str, Any],
    run_dir: Path,
    buggy_dir: str,
    fixed_dir: str,
    env_name: str,
    timeout: int,
) -> dict[str, Any]:
    started = time.time()
    instance_id = str(issue["instance_id"])
    code = str(candidate["code"])
    rel_file = _direct_test_relpath(instance_id, run_dir)
    selector = first_test_selector(code)
    command = test_command(str(issue["repo"]), str(issue["version"]), rel_file, selector)
    result = {
        "instance_id": instance_id,
        "candidate_id": candidate["candidate_id"],
        "code_hash": candidate["code_hash"],
        "origin": candidate["origin"],
        "round_id": candidate["round_id"],
        "code_path": candidate["code_path"],
        "command": command,
        "cached": False,
    }
    pythonpath_buggy = f"{buggy_dir}:{buggy_dir}/src:{buggy_dir}/lib"
    pythonpath_fixed = f"{fixed_dir}:{fixed_dir}/src:{fixed_dir}/lib"
    buggy_command = f"{conda_activate_cmd(env_name)} && export PYTHONPATH={pythonpath_buggy}:$PYTHONPATH && {command}"
    fixed_command = f"{conda_activate_cmd(env_name)} && export PYTHONPATH={pythonpath_fixed}:$PYTHONPATH && {command}"
    _remove_test_file(buggy_dir, rel_file)
    _remove_test_file(fixed_dir, rel_file)
    write_generated_test(buggy_dir, rel_file, code)
    write_generated_test(fixed_dir, rel_file, code)
    buggy_run = run_shell(buggy_command, buggy_dir, timeout)
    fixed_run = run_shell(fixed_command, fixed_dir, timeout)
    buggy = classify_run(buggy_run)
    fixed = classify_run(fixed_run)
    is_f2p = bool(buggy["failed"] and not fixed["failed"])
    result.update(
        {
            "buggy_status": buggy["status"],
            "fixed_status": fixed["status"],
            "buggy_failed": buggy["failed"],
            "fixed_failed": fixed["failed"],
            "is_f2p": is_f2p,
            "formal_status": F2P_STATUS if is_f2p else ("BUGGY_PASS" if not buggy["failed"] else "FIXED_FAIL" if fixed["failed"] else "UNKNOWN"),
            "execution_time_seconds": round(time.time() - started, 3),
            "buggy": buggy,
            "fixed": fixed,
            "buggy_run": {
                "returncode": buggy_run.get("returncode"),
                "timeout": buggy_run.get("timeout"),
                "duration": buggy_run.get("duration"),
                "stdout_tail": "\n".join(str(buggy_run.get("stdout") or "").splitlines()[-60:]),
                "stderr_tail": "\n".join(str(buggy_run.get("stderr") or "").splitlines()[-60:]),
            },
            "fixed_run": {
                "returncode": fixed_run.get("returncode"),
                "timeout": fixed_run.get("timeout"),
                "duration": fixed_run.get("duration"),
                "stdout_tail": "\n".join(str(fixed_run.get("stdout") or "").splitlines()[-60:]),
                "stderr_tail": "\n".join(str(fixed_run.get("stderr") or "").splitlines()[-60:]),
            },
        }
    )
    return result


def _direct_test_relpath(instance_id: str, run_dir: Path) -> str:
    host_path = run_dir / "generation" / instance_id / "host_context.json"
    test_dir = "tests"
    host = _load_json(host_path)
    if isinstance(host, dict):
        host_file = str(host.get("host_file") or "")
        if host_file:
            test_dir = os.path.dirname(host_file) or "."
    return os.path.join(test_dir, f"test_brt_{sanitize_instance_id(instance_id)}.py")


def _evaluate_instance(
    issue: dict[str, Any],
    candidates: list[dict[str, Any]],
    run_dir: Path,
    output_dir: Path,
    repo_root_base: str,
    timeout: int,
    stop_on_first_f2p: bool,
    imported_cache: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    instance_id = str(issue["instance_id"])
    instance_dir = output_dir / "instances" / sanitize_instance_id(instance_id)
    ensure_dir(instance_dir)
    summary_path = instance_dir / "summary.json"
    if summary_path.is_file():
        summary = _load_json(summary_path)
        if isinstance(summary, dict) and summary.get("complete"):
            return summary
    evaluated: list[dict[str, Any]] = []
    skipped_after_first = 0
    pool_has_f2p = False
    cache_hits = 0
    executed = 0
    buggy_dir = ""
    fixed_dir = ""
    prep: dict[str, Any] = {}
    try:
        for candidate in candidates:
            cache_key = (instance_id, str(candidate["code_hash"]))
            cache_path = _candidate_cache_path(output_dir, instance_id, str(candidate["code_hash"]))
            cached = imported_cache.get(cache_key)
            if cached is None and cache_path.is_file():
                loaded = _load_json(cache_path)
                cached = loaded if isinstance(loaded, dict) else None
            if cached is not None:
                item = dict(cached)
                item.setdefault("candidate_id", candidate["candidate_id"])
                item.setdefault("origin", candidate["origin"])
                item["cached"] = True
                evaluated.append(item)
                cache_hits += 1
                if item.get("is_f2p"):
                    pool_has_f2p = True
                    if stop_on_first_f2p:
                        skipped_after_first = len(candidates) - len(evaluated)
                        break
                continue
            if not buggy_dir or not fixed_dir:
                buggy_dir, fixed_dir, prep = _prepare_instance_repos(
                    issue,
                    run_dir,
                    output_dir,
                    repo_root_base,
                    timeout,
                )
                safe_json_dump(prep, str(instance_dir / "prepare.json"))
                if prep.get("status") != "PREPARED":
                    break
            result = _evaluate_candidate(
                issue,
                candidate,
                run_dir,
                buggy_dir,
                fixed_dir,
                str(prep["env_resolution"]["resolved_env"]),
                timeout,
            )
            executed += 1
            safe_json_dump(result, str(cache_path))
            evaluated.append(result)
            if result.get("is_f2p"):
                pool_has_f2p = True
                if stop_on_first_f2p:
                    skipped_after_first = len(candidates) - len(evaluated)
                    break
        not_evaluable = bool(prep and prep.get("status") not in {"", "PREPARED"})
        summary = {
            "instance_id": instance_id,
            "complete": True,
            "pool_has_f2p": pool_has_f2p,
            "not_evaluable": not_evaluable,
            "prepare_status": prep.get("status") if prep else ("NOT_NEEDED_CACHE_HIT" if evaluated else "NO_CANDIDATES"),
            "candidate_count": len(candidates),
            "evaluated_or_cached_count": len(evaluated),
            "executed_count": executed,
            "cache_hit_count": cache_hits,
            "skipped_after_first_f2p": skipped_after_first,
            "f2p_candidate_ids": [item.get("candidate_id") for item in evaluated if item.get("is_f2p")],
            "results": evaluated,
        }
        safe_json_dump(summary, str(summary_path))
        return summary
    finally:
        for repo_dir in (buggy_dir, fixed_dir):
            if repo_dir:
                shutil.rmtree(repo_dir, ignore_errors=True)


def _build_summary(
    output_dir: Path,
    total_instances: int,
    known_info: dict[str, Any],
    enum_manifest: dict[str, Any],
    pending_count: int,
    instance_summaries: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    known_success_count = int(known_info["known_pool_success_count"])
    new_success = sorted(
        item["instance_id"]
        for item in instance_summaries
        if item.get("pool_has_f2p")
    )
    not_evaluable = sorted(
        item["instance_id"]
        for item in instance_summaries
        if item.get("not_evaluable")
    )
    processed = len(instance_summaries)
    no_f2p = processed - len(new_success) - len(not_evaluable)
    no_candidate = list(enum_manifest.get("instances_without_candidate_pool") or [])
    U = known_success_count + len(new_success)
    summary = {
        "total_instances": total_instances,
        "known_pool_success_from_existing_eval": known_success_count,
        "known_pool_success_instances": known_info["known_pool_success_instances"],
        "new_pool_success_found": len(new_success),
        "new_pool_success_instances": new_success,
        "U": U,
        "legacy_selected_success": known_info["legacy_selected_success"],
        "counterfactual_selected_success": known_info["counterfactual_selected_success"],
        "legacy_selection_gap": U - int(known_info["legacy_selected_success"]),
        "counterfactual_selection_gap": U - int(known_info["counterfactual_selected_success"]),
        "instances_with_no_f2p_candidate": no_f2p + len(no_candidate),
        "instances_without_candidate_pool": no_candidate,
        "instances_not_evaluable": len(not_evaluable),
        "not_evaluable_instances": not_evaluable,
        "remaining_instance_count": pending_count,
        "remaining_instances_processed": processed,
        "raw_checkpoint_count": enum_manifest["raw_checkpoint_count"],
        "unique_candidate_count": enum_manifest["unique_candidate_count"],
        "duplicate_candidate_count": enum_manifest["duplicate_candidate_count"],
        "unique_candidates_evaluated": sum(int(item.get("executed_count") or 0) for item in instance_summaries),
        "candidate_cache_hits": sum(int(item.get("cache_hit_count") or 0) for item in instance_summaries),
        "candidates_skipped_after_first_f2p": sum(int(item.get("skipped_after_first_f2p") or 0) for item in instance_summaries),
        "elapsed_seconds": round(time.time() - started_at, 3),
        "output_dir": str(output_dir),
        "complete": processed == pending_count,
        "candidate_count_by_origin": enum_manifest.get("candidate_count_by_origin") or {},
    }
    _write_json(output_dir / "pool_upper_bound_summary.json", summary)
    return summary


def _worker(
    worker_id: int,
    ids: list[str],
    issues: dict[str, dict[str, Any]],
    candidates_by_instance: dict[str, list[dict[str, Any]]],
    args: argparse.Namespace,
    run_dir: Path,
    output_dir: Path,
    imported_cache: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    worker_dir = output_dir / f"worker_{worker_id}"
    ensure_dir(worker_dir)
    summaries: list[dict[str, Any]] = []
    for instance_id in ids:
        summary = _evaluate_instance(
            issues[instance_id],
            candidates_by_instance.get(instance_id, []),
            run_dir,
            output_dir,
            args.repo_root_base,
            args.timeout,
            args.stop_on_first_f2p,
            imported_cache,
        )
        summaries.append(summary)
        safe_json_dump(summaries, str(worker_dir / "instance_summaries.json"))
        print(
            f"worker_{worker_id} {instance_id} pool_has_f2p={summary.get('pool_has_f2p')} "
            f"executed={summary.get('executed_count')} cached={summary.get('cache_hit_count')} "
            f"skipped={summary.get('skipped_after_first_f2p')}",
            flush=True,
        )
    return {"worker_id": worker_id, "count": len(summaries), "path": str(worker_dir / "instance_summaries.json")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fast grouped checkpoint-pool upper-bound evaluator.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--instances-path", default=DEFAULT_INSTANCES_PATH)
    parser.add_argument("--repo-root-base", default="")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--stop-on-first-f2p", type=lambda x: str(x).lower() in {"1", "true", "yes", "on"}, default=True)
    parser.add_argument("--reuse-existing-formal-results", type=lambda x: str(x).lower() in {"1", "true", "yes", "on"}, default=True)
    parser.add_argument("--resume", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_dir = Path(args.run_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    ensure_dir(output_dir)
    started_at = time.time()
    run_config = _load_json(run_dir / "run_config.json")
    if not args.repo_root_base:
        args.repo_root_base = str((run_config or {}).get("repo_root_base") or run_dir.parents[2] / "swe_repos")
    instances_path = Path(args.instances_path)
    if not instances_path.is_absolute():
        instances_path = Path.cwd() / instances_path
    issues = load_issue_data(str(instances_path))
    fill_patches_from_swebench_lite(issues)
    rows_by_id = {instance_id: row for instance_id, row in issues.items()}
    preflight = preflight_system([str(output_dir), str(run_dir / "generation"), args.repo_root_base, os.environ.get("TMPDIR") or "/tmp"])
    safe_json_dump(preflight, str(output_dir / "environment_preflight.json"))
    if not preflight.get("ok"):
        raise SystemExit("environment preflight failed")
    candidates_by_instance, enum_manifest = _enumerate_candidates(run_dir, rows_by_id)
    known_success, known_info = _formal_successes(run_dir) if args.reuse_existing_formal_results else (set(), {
        "legacy_selected_success": 0,
        "counterfactual_selected_success": 0,
        "known_pool_success_count": 0,
        "known_pool_success_instances": [],
    })
    imported_cache = _import_selected_candidate_cache(run_dir) if args.reuse_existing_formal_results else {}
    pending_ids = [
        instance_id
        for instance_id in rows_by_id
        if instance_id not in known_success and instance_id in candidates_by_instance
    ]
    no_candidate_ids = [
        instance_id
        for instance_id in rows_by_id
        if instance_id not in known_success and instance_id not in candidates_by_instance
    ]
    enum_manifest["instances_without_candidate_pool"] = no_candidate_ids
    enum_manifest["remaining_instance_count"] = len(pending_ids)
    _write_json(output_dir / "known_pool_success.json", known_info)
    _write_json(output_dir / "candidate_pool_manifest.json", enum_manifest)
    _write_json(output_dir / "pending_instances.json", pending_ids)
    buckets = group_ids_by_repo(issues, pending_ids, max(1, args.workers))
    futures = []
    worker_results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for worker_id, bucket in enumerate(buckets):
            if not bucket:
                continue
            futures.append(
                pool.submit(
                    _worker,
                    worker_id,
                    bucket,
                    issues,
                    candidates_by_instance,
                    args,
                    run_dir,
                    output_dir,
                    imported_cache,
                )
            )
        for future in as_completed(futures):
            worker_results.append(future.result())
            instance_summaries: list[dict[str, Any]] = []
            for item in worker_results:
                path = Path(item["path"])
                data = _load_json(path)
                if isinstance(data, list):
                    instance_summaries.extend(row for row in data if isinstance(row, dict))
            summary = _build_summary(
                output_dir,
                len(rows_by_id),
                known_info,
                enum_manifest,
                len(pending_ids),
                instance_summaries,
                started_at,
            )
            print(
                "progress "
                f"processed={summary['remaining_instances_processed']}/{len(pending_ids)} "
                f"new_pool_f2p={summary['new_pool_success_found']} "
                f"U_lower_bound={summary['U']} "
                f"executed={summary['unique_candidates_evaluated']} "
                f"dedup={summary['duplicate_candidate_count']} "
                f"skipped={summary['candidates_skipped_after_first_f2p']}",
                flush=True,
            )
    instance_summaries = []
    for path in sorted((output_dir).glob("worker_*/instance_summaries.json")):
        data = _load_json(path)
        if isinstance(data, list):
            instance_summaries.extend(row for row in data if isinstance(row, dict))
    summary = _build_summary(
        output_dir,
        len(rows_by_id),
        known_info,
        enum_manifest,
        len(pending_ids),
        instance_summaries,
        started_at,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
