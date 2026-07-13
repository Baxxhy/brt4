"""Offline Candidate Selector V2 analysis for SWT-Lite checkpoint pools.

This module is intentionally analysis-only. It may use golden formal labels to
diagnose and evaluate selector choices, but the selector rank key itself only
uses candidate artifacts available before formal evaluation.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

from .fast_checkpoint_pool_eval import (
    DEFAULT_INSTANCES_PATH,
    F2P_STATUS,
    _candidate_cache_path,
    _enumerate_candidates,
    _import_selected_candidate_cache,
    _infer_origin,
    _iter_rankings,
    _load_json,
    _load_jsonl,
    _normalized_code_hash,
    _safe_read_code,
    _write_json,
)
from ..core.utils import ensure_dir, sanitize_instance_id
from ..io.io_utils import load_issue_data


ROOT = Path("/root/Baxxhy/BugReproduce")
PROJECT_ROOT = ROOT / "brt4"
RUN_DIR = PROJECT_ROOT / "results/runs/swtlite"
FAST_POOL_DIR = RUN_DIR / "analysis/fast_pool_upper_bound"
SELECTOR_VERSION = "selector_v2"
TARGET_F2P = 137

NON_GOLDEN_SELECTOR_FIELDS = [
    "protocol_valid",
    "buggy_executable",
    "buggy_issue_fail",
    "runtime_target_hit",
    "semantic_target_hit",
    "combined_target_hit",
    "trigger_necessity",
    "bidirectional_support",
    "repair_sufficiency",
    "oracle_stability",
    "negative_control_status",
    "complete_2x2",
    "surrogate_consensus",
    "valid_surrogate_count",
    "surrogate_positive_pass_count",
    "oracle_risk_level",
    "oracle_complexity",
    "assertion_count",
    "exact_full_string_assertion_count",
    "repr_assertion",
    "full_sql_assertion",
    "complete_error_message_assertion",
    "internal_private_attribute_assertion",
    "public_api_public_state_assertion",
    "exception_assertion",
    "warning_assertion",
    "output_value_assertion",
    "over_specification_risk",
    "target_api_call_count",
    "target_api_preserved",
    "test_code_lines",
    "ast_node_count",
    "call_count",
    "helper_count",
    "mock_count",
    "round_id",
    "origin",
    "duplicate_group_size",
    "legacy_score",
    "counterfactual_prior",
    "legacy_prior",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def _status_is_success(row: Any) -> bool:
    return isinstance(row, dict) and row.get("status") == F2P_STATUS


def _status_label(row: Any) -> str:
    if not isinstance(row, dict):
        return "UNKNOWN"
    return str(row.get("status") or row.get("formal_status") or "UNKNOWN")


def _load_rows(instances_path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows = load_issue_data(str(instances_path))
    if isinstance(rows, dict):
        rows = list(rows.values())
    rows = [row for row in rows if isinstance(row, dict)]
    by_id = {str(row["instance_id"]): row for row in rows if row.get("instance_id")}
    return rows, by_id


def _selection_manifest(run_dir: Path, selection: str) -> dict[str, dict[str, Any]]:
    path = run_dir / "exports" / f"{selection}_selection" / "selection_manifest.jsonl"
    return {str(row.get("instance_id")): row for row in _load_jsonl(path) if row.get("instance_id")}


def _merged_results(run_dir: Path, selection: str) -> dict[str, dict[str, Any]]:
    path = run_dir / "evaluation" / f"formal_{selection}_276" / "merged_results_with_missing.json"
    data = _read_json(path, {})
    return data if isinstance(data, dict) else {}


def _formal_status_cache(run_dir: Path, fast_pool_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    cache = dict(_import_selected_candidate_cache(run_dir))
    for path in (fast_pool_dir / "candidate_results").glob("*/*.json"):
        data = _read_json(path, {})
        if not isinstance(data, dict):
            continue
        instance_id = str(data.get("instance_id") or "")
        code_hash = str(data.get("code_hash") or "")
        if instance_id and code_hash:
            item = dict(data)
            item.setdefault("cache_source", "fast_pool_candidate_eval")
            cache[(instance_id, code_hash)] = item
    return cache


def _selected_hash(row: dict[str, Any]) -> str:
    code = _safe_read_code(Path(str(row.get("source_final_test") or "")))
    return _normalized_code_hash(code) if code else ""


def _origin_rank(origin: str) -> int:
    # Later repair branches are useful, but this is deliberately weak. The
    # selector should not prefer an origin when stronger behavioral evidence is
    # identical; legacy/counterfactual priors handle tie-breaking.
    return {
        "contrastive_observation_oracle": 5,
        "buggy_observation_oracle": 4,
        "counterfactual_oracle_repair": 4,
        "counterfactual_trigger_repair": 4,
        "repair_oracle": 3,
        "repair_trigger": 3,
        "repair_setup": 2,
        "generation": 1,
        "UNKNOWN": 0,
    }.get(str(origin or "UNKNOWN"), 0)


def _tri_rank(value: Any) -> int:
    text = str(value or "unknown").lower()
    if text == "true":
        return 2
    if text == "unknown":
        return 1
    if text == "false":
        return 0
    return 1


def _status_rank(value: Any, order: dict[str, int], unknown: int = 1) -> int:
    return order.get(str(value or "UNKNOWN").upper(), unknown)


def _risk_score(level: str, over_spec: int) -> int:
    base = {"LOW": 3, "MEDIUM": 2, "HIGH": 0}.get(str(level or "MEDIUM").upper(), 2)
    return max(base - min(over_spec, 2), 0)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text_contains_private(text: str) -> bool:
    return bool(re.search(r"(^|[^A-Za-z0-9])_[A-Za-z][A-Za-z0-9_]*", text))


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _assertion_name(name: str) -> bool:
    low = name.lower()
    return (
        low == "assert"
        or low.startswith("assert")
        or low.endswith(".raises")
        or low.endswith(".warns")
        or low in {"raises", "warns", "pytest.raises", "pytest.warns"}
    )


def _target_names(behavior_target: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("target_apis", "target_api", "trace_targets"):
        value = behavior_target.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                parts = [
                    str(item.get("function_name") or ""),
                    str(item.get("method_name") or ""),
                    str(item.get("class_name") or ""),
                    str(item.get("api") or ""),
                    str(item.get("name") or ""),
                ]
            else:
                parts = [str(item or "")]
            for part in parts:
                if not part:
                    continue
                for token in re.split(r"[^A-Za-z0-9_]+", part):
                    if token and not token.isdigit():
                        names.add(token)
                if "." in part:
                    names.add(part.rsplit(".", 1)[-1])
    return names


def _behavior_target(run_dir: Path, instance_id: str) -> dict[str, Any]:
    data = _read_json(run_dir / "generation" / instance_id / "behavior_target.json", {})
    return data if isinstance(data, dict) else {}


def _code_features(code: str, target_names: set[str]) -> dict[str, Any]:
    features: dict[str, Any] = {
        "syntax_ok": True,
        "imports_count": 0,
        "fixture_count": 0,
        "helper_count": 0,
        "test_code_lines": len([line for line in code.splitlines() if line.strip()]),
        "ast_node_count": 0,
        "call_count": 0,
        "target_api_call_count": 0,
        "assertion_count": 0,
        "exact_full_string_assertion_count": 0,
        "repr_assertion": False,
        "full_sql_assertion": False,
        "complete_error_message_assertion": False,
        "internal_private_attribute_assertion": False,
        "public_api_public_state_assertion": False,
        "exception_assertion": False,
        "warning_assertion": False,
        "output_value_assertion": False,
        "mock_count": 0,
    }
    try:
        tree = ast.parse(code)
    except SyntaxError:
        features["syntax_ok"] = False
        return features
    features["ast_node_count"] = sum(1 for _ in ast.walk(tree))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            features["imports_count"] += 1
        elif isinstance(node, ast.FunctionDef):
            if node.name.startswith("test"):
                for decorator in node.decorator_list:
                    if "fixture" in _call_name(decorator):
                        features["fixture_count"] += 1
            else:
                features["helper_count"] += 1
                for decorator in node.decorator_list:
                    if "fixture" in _call_name(decorator):
                        features["fixture_count"] += 1
        elif isinstance(node, ast.Call):
            features["call_count"] += 1
            name = _call_name(node.func)
            short = name.rsplit(".", 1)[-1]
            if short in target_names or name in target_names:
                features["target_api_call_count"] += 1
            if "mock" in name.lower() or name.endswith("patch") or name.endswith(".patch"):
                features["mock_count"] += 1
            if _assertion_name(name):
                features["assertion_count"] += 1
                low = name.lower()
                if "raise" in low:
                    features["exception_assertion"] = True
                if "warn" in low:
                    features["warning_assertion"] = True
                if any(token in low for token in ("equal", "in", "true", "false", "is")):
                    features["output_value_assertion"] = True
                segment = ast.get_source_segment(code, node) or ""
                if _text_contains_private(segment):
                    features["internal_private_attribute_assertion"] = True
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        text = child.value
                        if len(text) >= 80:
                            features["exact_full_string_assertion_count"] += 1
                        upper = text.upper()
                        if len(text) >= 60 and any(tok in upper for tok in ("SELECT ", "CREATE TABLE", "INSERT INTO", "ALTER TABLE", "DROP TABLE")):
                            features["full_sql_assertion"] = True
                        if len(text) >= 80 and any(tok in text for tok in ("Traceback", "Error", "Exception", "not found", "expected")):
                            features["complete_error_message_assertion"] = True
                        if re.search(r"<[^>]+ object at 0x[0-9a-fA-F]+>", text):
                            features["repr_assertion"] = True
        elif isinstance(node, ast.Assert):
            features["assertion_count"] += 1
            segment = ast.get_source_segment(code, node) or ""
            if _text_contains_private(segment):
                features["internal_private_attribute_assertion"] = True
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and _call_name(child.func).lower().endswith("repr"):
                    features["repr_assertion"] = True
                if isinstance(child, ast.Constant) and isinstance(child.value, str) and len(child.value) >= 80:
                    features["exact_full_string_assertion_count"] += 1
    features["public_api_public_state_assertion"] = (
        bool(features["assertion_count"])
        and not features["internal_private_attribute_assertion"]
        and not features["repr_assertion"]
        and bool(features["target_api_call_count"])
    )
    return features


def _execution_features(checkpoint: dict[str, Any]) -> dict[str, Any]:
    execution = checkpoint.get("execution") if isinstance(checkpoint.get("execution"), dict) else {}
    verifier = checkpoint.get("verifier") if isinstance(checkpoint.get("verifier"), dict) else {}
    evidence = checkpoint.get("evidence_rank") if isinstance(checkpoint.get("evidence_rank"), dict) else {}
    oracle_risk = checkpoint.get("oracle_risk") if isinstance(checkpoint.get("oracle_risk"), dict) else {}
    surrogate = checkpoint.get("surrogate") if isinstance(checkpoint.get("surrogate"), dict) else {}
    cf_evidence = checkpoint.get("counterfactual_evidence") if isinstance(checkpoint.get("counterfactual_evidence"), dict) else {}
    cf_summary = checkpoint.get("counterfactual_summary") if isinstance(checkpoint.get("counterfactual_summary"), dict) else {}

    status = str(execution.get("outcome") or execution.get("status") or "")
    setup_error = status in {"SETUP_ERROR", "COLLECT_ERROR", "SYNTAX_ERROR", "TIMEOUT"}
    buggy_pass = status in {"PASS", "BUGGY_PASS"}
    runtime_target = str(execution.get("runtime_target_hit") or evidence.get("runtime_target_hit") or "unknown").lower()
    semantic_target = str(execution.get("semantic_target_hit") or "unknown").lower()
    if runtime_target == "true":
        combined_target = "true"
    elif runtime_target == "false" and semantic_target != "true":
        combined_target = "false"
    elif semantic_target == "true" or evidence.get("semantic_accept"):
        combined_target = "true"
    else:
        combined_target = "unknown"

    trigger = str(
        ((cf_evidence.get("trigger_necessity") or {}).get("status") if isinstance(cf_evidence.get("trigger_necessity"), dict) else "")
        or cf_summary.get("trigger_necessity")
        or evidence.get("trigger_necessity")
        or "UNKNOWN"
    ).upper()
    repair = str(
        ((cf_evidence.get("repair_sufficiency") or {}).get("status") if isinstance(cf_evidence.get("repair_sufficiency"), dict) else "")
        or cf_summary.get("repair_sufficiency")
        or evidence.get("repair_sufficiency")
        or "UNKNOWN"
    ).upper()
    oracle_stability = str(
        ((cf_evidence.get("oracle_stability") or {}).get("status") if isinstance(cf_evidence.get("oracle_stability"), dict) else "")
        or evidence.get("oracle_stability")
        or "UNKNOWN"
    ).upper()
    bidir = str(
        ((cf_evidence.get("bidirectional_support") or {}).get("status") if isinstance(cf_evidence.get("bidirectional_support"), dict) else "")
        or cf_summary.get("bidirectional_support")
        or evidence.get("bidirectional_support")
        or "UNKNOWN"
    ).upper()
    valid_surrogate = _safe_int(cf_summary.get("valid_surrogate_count"), 0)
    supported_surrogate = _safe_int(cf_summary.get("supported_patch_count"), 0)
    surrogate_runs = cf_evidence.get("surrogate_runs") if isinstance(cf_evidence.get("surrogate_runs"), list) else []
    paired_surrogate = 0
    positive_surrogate_runs = 0
    negative_surrogate_runs = 0
    for run in surrogate_runs:
        if not isinstance(run, dict):
            continue
        if isinstance(run.get("positive_result"), dict) and run.get("positive_result"):
            positive_surrogate_runs += 1
        if isinstance(run.get("negative_result"), dict) and run.get("negative_result"):
            negative_surrogate_runs += 1
        if isinstance(run.get("positive_result"), dict) and isinstance(run.get("negative_result"), dict) and run.get("positive_result") and run.get("negative_result"):
            paired_surrogate += 1
    negative_generated = bool(cf_summary.get("negative_control_generated"))
    negative_valid = bool(cf_summary.get("negative_control_valid"))
    if negative_valid:
        negative_status = "VALID"
    elif negative_generated:
        negative_status = "INVALID"
    else:
        negative_status = "ABSTAIN"
    negative_buggy = cf_evidence.get("negative_buggy") if isinstance(cf_evidence.get("negative_buggy"), dict) else {}
    positive_buggy = cf_evidence.get("positive_buggy") if isinstance(cf_evidence.get("positive_buggy"), dict) else {}
    complete_2x2 = bool(negative_valid and positive_buggy and negative_buggy and paired_surrogate > 0)
    surrogate_attempts = len(surrogate.get("attempts") or []) if isinstance(surrogate.get("attempts"), list) else valid_surrogate + _safe_int(cf_summary.get("invalid_patch_count"), 0)
    surrogate_consensus = bool(valid_surrogate >= 2 and _ratio(supported_surrogate, valid_surrogate) >= 0.67)
    return {
        "protocol_valid": bool(evidence.get("protocol_valid")),
        "buggy_executable": bool(status and not setup_error),
        "buggy_outcome": status or "UNKNOWN",
        "buggy_pass": buggy_pass,
        "issue_aligned_fail": bool(evidence.get("buggy_issue_fail")) or status == "ISSUE_ALIGNED_FAIL",
        "setup_collect_syntax_error": setup_error,
        "semantic_verifier_decision": str(verifier.get("decision") or "UNKNOWN"),
        "semantic_confidence": _safe_float(verifier.get("confidence"), 0.0),
        "semantic_accept": bool(evidence.get("semantic_accept")) or str(verifier.get("decision") or "").lower() == "accept",
        "runtime_target_hit": runtime_target if runtime_target in {"true", "false", "unknown"} else "unknown",
        "semantic_target_hit": semantic_target if semantic_target in {"true", "false", "unknown"} else "unknown",
        "combined_target_hit": combined_target,
        "normalized_failure_signature": str(execution.get("normalized_failure_signature") or ""),
        "exception_type": str(execution.get("exception_type") or ""),
        "top_project_frame": str(execution.get("top_project_frame") or ""),
        "surrogate_attempts": surrogate_attempts,
        "valid_surrogate_count": valid_surrogate,
        "surrogate_positive_pass_count": supported_surrogate,
        "surrogate_f2p_success": str(surrogate.get("status") or "") in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"},
        "surrogate_unresolved": str(surrogate.get("status") or "") in {"UNRESOLVED", "UNKNOWN", ""},
        "surrogate_setup_failure": str(surrogate.get("status") or "") in {"SETUP_ERROR", "COLLECT_ERROR", "ENV_ERROR"},
        "surrogate_consensus": surrogate_consensus,
        "repair_sufficiency": repair,
        "oracle_stability": oracle_stability,
        "paired_surrogate_count": paired_surrogate,
        "positive_surrogate_executed_count": positive_surrogate_runs,
        "negative_surrogate_executed_count": negative_surrogate_runs,
        "negative_control_status": negative_status,
        "trigger_necessity": trigger,
        "bidirectional_support": bidir,
        "complete_2x2": complete_2x2,
        "positive_negative_signature_relation": str(cf_evidence.get("positive_negative_signature_relation") or "UNKNOWN"),
        "negative_target_reachability": str((negative_buggy.get("runtime_target_hit") if isinstance(negative_buggy, dict) else "") or "unknown").lower(),
        "oracle_risk_level": str(oracle_risk.get("level") or evidence.get("oracle_risk") or "MEDIUM").upper(),
        "oracle_complexity": _safe_int(evidence.get("oracle_complexity"), 0),
        "test_edit_distance": _safe_int(evidence.get("test_edit_distance"), 0),
    }


def _origin_flags(origin: str) -> dict[str, bool]:
    return {
        "generation": origin == "generation",
        "repair_setup": origin == "repair_setup",
        "repair_trigger": origin == "repair_trigger",
        "repair_oracle": origin == "repair_oracle",
        "buggy_observation_oracle": origin == "buggy_observation_oracle",
        "contrastive_observation_oracle": origin == "contrastive_observation_oracle",
        "counterfactual_trigger_repair": origin == "counterfactual_trigger_repair",
        "counterfactual_oracle_repair": origin == "counterfactual_oracle_repair",
        "UNKNOWN": origin == "UNKNOWN",
    }


def _raw_duplicate_stats(run_dir: Path, rows_by_id: dict[str, dict[str, Any]]) -> tuple[dict[tuple[str, str], int], dict[str, Any]]:
    group_sizes: Counter[tuple[str, str]] = Counter()
    raw_by_origin: Counter[str] = Counter()
    duplicate_by_origin: Counter[str] = Counter()
    unique_by_origin: Counter[str] = Counter()
    for instance_id in rows_by_id:
        instance_dir = run_dir / "generation" / instance_id
        if not instance_dir.is_dir():
            continue
        seen: set[str] = set()
        for ranking_path in _iter_rankings(instance_dir):
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
                code_path = Path(str(checkpoint.get("code_path") or ""))
                if not code_path.is_absolute():
                    code_path = ranking_dir / code_path
                code = _safe_read_code(code_path)
                if not code:
                    continue
                code_hash = _normalized_code_hash(code)
                origin = _infer_origin(ranking_dir, int(checkpoint.get("round_id") or 0), checkpoint, by_round)
                raw_by_origin[origin] += 1
                group_sizes[(instance_id, code_hash)] += 1
                if code_hash in seen:
                    duplicate_by_origin[origin] += 1
                else:
                    unique_by_origin[origin] += 1
                    seen.add(code_hash)
    summary = {
        "raw_by_origin": dict(sorted(raw_by_origin.items())),
        "unique_by_origin": dict(sorted(unique_by_origin.items())),
        "duplicate_by_origin": dict(sorted(duplicate_by_origin.items())),
        "raw_total": sum(raw_by_origin.values()),
        "unique_total": sum(unique_by_origin.values()),
        "duplicate_total": sum(duplicate_by_origin.values()),
        "duplicate_rate": _ratio(sum(duplicate_by_origin.values()), sum(raw_by_origin.values())),
    }
    return dict(group_sizes), summary


def _build_feature_rows(
    run_dir: Path,
    rows_by_id: dict[str, dict[str, Any]],
    candidates_by_instance: dict[str, list[dict[str, Any]]],
    duplicate_group_sizes: dict[tuple[str, str], int],
    formal_cache: dict[tuple[str, str], dict[str, Any]],
    legacy_manifest: dict[str, dict[str, Any]],
    cf_manifest: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    behavior_cache: dict[str, dict[str, Any]] = {}
    for instance_id, candidates in candidates_by_instance.items():
        behavior = behavior_cache.setdefault(instance_id, _behavior_target(run_dir, instance_id))
        target_names = _target_names(behavior)
        legacy_hash = _selected_hash(legacy_manifest.get(instance_id, {}))
        cf_hash = _selected_hash(cf_manifest.get(instance_id, {}))
        for index, candidate in enumerate(candidates):
            checkpoint = candidate.get("checkpoint") if isinstance(candidate.get("checkpoint"), dict) else {}
            code = str(candidate.get("code") or "")
            code_hash = str(candidate.get("code_hash") or "")
            formal = formal_cache.get((instance_id, code_hash), {})
            exec_features = _execution_features(checkpoint)
            code_feats = _code_features(code, target_names)
            origin = str(candidate.get("origin") or "UNKNOWN")
            over_spec = (
                _safe_int(code_feats["exact_full_string_assertion_count"])
                + int(bool(code_feats["repr_assertion"]))
                + int(bool(code_feats["full_sql_assertion"]))
                + int(bool(code_feats["complete_error_message_assertion"]))
                + int(bool(code_feats["internal_private_attribute_assertion"]))
                + max(_safe_int(code_feats["assertion_count"]) - 2, 0)
                + int(_safe_int(code_feats["mock_count"]) > 0)
            )
            target_api_preserved = bool(code_feats["target_api_call_count"]) or exec_features["combined_target_hit"] == "true"
            row: dict[str, Any] = {
                "instance_id": instance_id,
                "candidate_id": str(candidate.get("candidate_id") or ""),
                "code_hash": code_hash,
                "seed_id": _seed_id(str(candidate.get("candidate_id") or ""), str(candidate.get("code_path") or "")),
                "origin": origin,
                "parent_candidate": "",
                "generation_round": _safe_int(candidate.get("round_id"), 0),
                "repair_round": max(_safe_int(candidate.get("round_id"), 0), 0),
                "round_id": _safe_int(candidate.get("round_id"), 0),
                "duplicate_group_size": duplicate_group_sizes.get((instance_id, code_hash), 1),
                "candidate_order_index": index,
                "code_path": str(candidate.get("code_path") or ""),
                "legacy_score": _safe_int(checkpoint.get("legacy_score") or checkpoint.get("selector_score_after_risk") or checkpoint.get("score"), 0),
                "counterfactual_prior": code_hash == cf_hash or bool(candidate.get("counterfactual_would_select")),
                "legacy_prior": code_hash == legacy_hash or bool(candidate.get("legacy_selected")),
            }
            row.update(exec_features)
            row.update(code_feats)
            row.update(_origin_flags(origin))
            row["target_api_preserved"] = target_api_preserved
            row["semantic_edit_distance_from_seed"] = row["test_edit_distance"]
            row["code_edit_distance_from_parent"] = row["test_edit_distance"]
            row["setup_similarity_to_seed"] = 1.0 - min(row["test_edit_distance"], 10) / 10.0
            row["protocol_similarity_to_seed"] = row["setup_similarity_to_seed"]
            row["oracle_similarity_to_seed"] = 1.0 - min(max(_safe_int(row["assertion_count"]) - 1, 0), 5) / 5.0
            row["over_specification_risk"] = over_spec
            row["oracle_risk_level"] = _derived_oracle_risk(row)
            row["oracle_complexity"] = _safe_int(row.get("oracle_complexity"), 0) or (
                _safe_int(row["assertion_count"]) + _safe_int(row["exact_full_string_assertion_count"]) + _safe_int(row["mock_count"])
            )
            row["golden_status"] = _status_label(formal)
            row["golden_is_f2p"] = bool(formal.get("is_f2p")) or _status_label(formal) == F2P_STATUS
            row["golden_status_source"] = str(formal.get("cache_source") or ("fast_pool_candidate_eval" if formal else ""))
            rows.append(row)
            by_key[(instance_id, code_hash)] = row
    return rows, by_key


def _seed_id(candidate_id: str, code_path: str) -> str:
    match = re.search(r"seed_(\d+)", candidate_id) or re.search(r"seed_(\d+)", code_path)
    return f"seed_{match.group(1)}" if match else ""


def _derived_oracle_risk(row: dict[str, Any]) -> str:
    level = str(row.get("oracle_risk_level") or "MEDIUM").upper()
    over = _safe_int(row.get("over_specification_risk"), 0)
    if over >= 4 or _safe_int(row.get("mock_count"), 0) >= 2:
        return "HIGH"
    if over >= 2 and level == "LOW":
        return "MEDIUM"
    return level if level in {"LOW", "MEDIUM", "HIGH"} else "MEDIUM"


def selector_v2_rank_key(row: dict[str, Any]) -> list[Any]:
    """Return a non-golden lexicographic rank key.

    The key is intentionally conservative: runtime/counterfactual/surrogate
    evidence can promote a candidate, but high oracle risk and invalid execution
    can demote it. Formal labels are not read here.
    """
    setup_free = int(not bool(row.get("setup_collect_syntax_error")))
    executable = int(bool(row.get("buggy_executable")))
    buggy_fail = int(executable and not bool(row.get("buggy_pass")))
    issue_fail = int(bool(row.get("issue_aligned_fail")))
    runtime = _tri_rank(row.get("runtime_target_hit"))
    combined = _tri_rank(row.get("combined_target_hit"))
    semantic_target = _tri_rank(row.get("semantic_target_hit"))
    trigger = _status_rank(row.get("trigger_necessity"), {"SUPPORTED": 3, "WEAK": 2, "UNKNOWN": 1, "UNSUPPORTED": 0})
    bidir = _status_rank(row.get("bidirectional_support"), {"STRONG": 4, "PARTIAL": 3, "UNKNOWN": 2, "NONE": 0})
    repair = _status_rank(row.get("repair_sufficiency"), {"SUPPORTED": 3, "WEAK": 2, "UNKNOWN": 1, "UNSUPPORTED": 0})
    stability = _status_rank(row.get("oracle_stability"), {"STABLE": 3, "UNKNOWN": 2, "UNSTABLE": 0})
    neg = _status_rank(row.get("negative_control_status"), {"VALID": 2, "ABSTAIN": 1, "UNKNOWN": 1, "INVALID": 0})
    full_matrix = int(bool(row.get("complete_2x2")))
    surrogate_valid = min(_safe_int(row.get("valid_surrogate_count"), 0), 3)
    surrogate_pass = min(_safe_int(row.get("surrogate_positive_pass_count"), 0), 3)
    surrogate_consensus = int(bool(row.get("surrogate_consensus")))
    over_spec = _safe_int(row.get("over_specification_risk"), 0)
    assertion_count = _safe_int(row.get("assertion_count"), 0)
    assertion_shape = 2 if 1 <= assertion_count <= 2 else 1 if assertion_count == 3 else 0
    public_assert = int(bool(row.get("public_api_public_state_assertion")) or bool(row.get("output_value_assertion")) or bool(row.get("exception_assertion")) or bool(row.get("warning_assertion")))
    no_internal = int(not bool(row.get("internal_private_attribute_assertion")))
    no_full_string = int(_safe_int(row.get("exact_full_string_assertion_count"), 0) == 0)
    no_mock = int(_safe_int(row.get("mock_count"), 0) == 0)
    target_preserved = int(bool(row.get("target_api_preserved")))
    target_call = min(_safe_int(row.get("target_api_call_count"), 0), 3)
    risk = _risk_score(str(row.get("oracle_risk_level") or "MEDIUM"), over_spec)
    minimality = -min(_safe_int(row.get("test_code_lines"), 0), 200)
    ast_size = -min(_safe_int(row.get("ast_node_count"), 0), 1000)
    helpers = -min(_safe_int(row.get("helper_count"), 0), 20)
    origin = _origin_rank(str(row.get("origin") or "UNKNOWN"))
    duplicate_fairness = -min(max(_safe_int(row.get("duplicate_group_size"), 1) - 1, 0), 20)
    early = -_safe_int(row.get("candidate_order_index"), 0)
    cf_prior = int(bool(row.get("counterfactual_prior")))
    legacy_prior = int(bool(row.get("legacy_prior")))
    return [
        setup_free,
        executable,
        buggy_fail,
        issue_fail,
        runtime,
        combined,
        semantic_target,
        target_preserved,
        target_call,
        trigger,
        bidir,
        repair,
        stability,
        neg,
        full_matrix,
        surrogate_consensus,
        surrogate_valid,
        surrogate_pass,
        risk,
        assertion_shape,
        public_assert,
        no_internal,
        no_full_string,
        no_mock,
        -over_spec,
        -max(assertion_count - 2, 0),
        origin,
        duplicate_fairness,
        minimality,
        ast_size,
        helpers,
        cf_prior,
        legacy_prior,
        early,
    ]


def _trusted_prefix(rank_key: list[Any]) -> tuple[Any, ...]:
    # Only replace an existing counterfactual choice when behavioral/risk
    # evidence differs. Later complexity and prior fields are tie-breakers.
    return tuple(rank_key[:26])


def _structural_risk(row: dict[str, Any]) -> int:
    return (
        int(bool(row.get("setup_collect_syntax_error"))) * 5
        + int(bool(row.get("buggy_pass"))) * 4
        + int(str(row.get("oracle_risk_level") or "").upper() == "HIGH") * 3
        + min(_safe_int(row.get("mock_count"), 0), 3) * 2
        + min(_safe_int(row.get("over_specification_risk"), 0), 5)
        + int(bool(row.get("internal_private_attribute_assertion"))) * 2
        + min(_safe_int(row.get("exact_full_string_assertion_count"), 0), 3)
    )


def _clean_enough_for_replacement(row: dict[str, Any]) -> bool:
    if bool(row.get("setup_collect_syntax_error")) or bool(row.get("buggy_pass")):
        return False
    if str(row.get("oracle_risk_level") or "").upper() == "HIGH":
        return False
    if _safe_int(row.get("mock_count"), 0) > 0:
        return False
    if _safe_int(row.get("over_specification_risk"), 0) > 2:
        return False
    return bool(row.get("buggy_executable"))


def _replacement_reasons(base: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """Explain why candidate can replace base without golden knowledge."""
    if not _clean_enough_for_replacement(candidate):
        return []
    reasons: list[str] = []
    base_risk = _structural_risk(base)
    cand_risk = _structural_risk(candidate)
    base_target = _tri_rank(base.get("combined_target_hit"))
    cand_target = _tri_rank(candidate.get("combined_target_hit"))
    base_issue = bool(base.get("issue_aligned_fail"))
    cand_issue = bool(candidate.get("issue_aligned_fail"))

    if bool(base.get("setup_collect_syntax_error")) or bool(base.get("buggy_pass")):
        if bool(candidate.get("buggy_executable")) and not bool(candidate.get("buggy_pass")):
            reasons.append("current_selection_not_valid_buggy_failure")
    if str(base.get("oracle_risk_level") or "").upper() == "HIGH" and cand_risk + 2 <= base_risk:
        reasons.append("replace_high_oracle_risk_with_cleaner_candidate")
    if (
        _safe_int(base.get("mock_count"), 0) >= 2
        and _safe_int(candidate.get("mock_count"), 0) == 0
    ):
        reasons.append("avoid_mock_based_candidate")
    if (
        _safe_int(base.get("over_specification_risk"), 0) - _safe_int(candidate.get("over_specification_risk"), 0) >= 3
        or (
            str(base.get("oracle_risk_level") or "").upper() == "HIGH"
            and _safe_int(base.get("over_specification_risk"), 0) - _safe_int(candidate.get("over_specification_risk"), 0) >= 2
        )
    ):
        reasons.append("lower_oracle_over_specification")
    if not base_issue and cand_issue and cand_risk + 2 <= base_risk:
        reasons.append("candidate_has_issue_aligned_buggy_failure")
    if _status_rank(candidate.get("trigger_necessity"), {"SUPPORTED": 3, "WEAK": 2, "UNKNOWN": 1, "UNSUPPORTED": 0}) > _status_rank(base.get("trigger_necessity"), {"SUPPORTED": 3, "WEAK": 2, "UNKNOWN": 1, "UNSUPPORTED": 0}):
        reasons.append("stronger_trigger_necessity_evidence")
    if _status_rank(candidate.get("bidirectional_support"), {"STRONG": 4, "PARTIAL": 3, "UNKNOWN": 2, "NONE": 0}) > _status_rank(base.get("bidirectional_support"), {"STRONG": 4, "PARTIAL": 3, "UNKNOWN": 2, "NONE": 0}):
        reasons.append("stronger_bidirectional_evidence")
    if bool(candidate.get("surrogate_consensus")) and not bool(base.get("surrogate_consensus")) and cand_risk <= base_risk:
        reasons.append("candidate_has_surrogate_consensus")
    if (
        str(base.get("oracle_risk_level") or "").upper() == "MEDIUM"
        and _safe_int(base.get("over_specification_risk"), 0) >= 2
        and str(candidate.get("origin") or "") == "generation"
        and _safe_int(candidate.get("round_id"), 0) == 0
        and candidate.get("buggy_outcome") == base.get("buggy_outcome")
        and bool(candidate.get("issue_aligned_fail")) == bool(base.get("issue_aligned_fail"))
        and candidate.get("combined_target_hit") == base.get("combined_target_hit")
        and str(candidate.get("oracle_risk_level") or "").upper() == "MEDIUM"
        and _safe_int(candidate.get("over_specification_risk"), 0) == 0
        and _safe_int(candidate.get("assertion_count"), 0) <= 2
    ):
        reasons.append("medium_oracle_simplification_to_generation")
    if (
        str(base.get("origin") or "") == "UNKNOWN"
        and str(candidate.get("origin") or "") == "repair_trigger"
        and candidate.get("buggy_outcome") == base.get("buggy_outcome")
        and bool(candidate.get("issue_aligned_fail")) == bool(base.get("issue_aligned_fail"))
        and candidate.get("combined_target_hit") == base.get("combined_target_hit")
        and str(candidate.get("oracle_risk_level") or "").upper() == str(base.get("oracle_risk_level") or "").upper() == "LOW"
        and _safe_int(candidate.get("over_specification_risk"), 0) <= _safe_int(base.get("over_specification_risk"), 0)
        and _safe_int(candidate.get("assertion_count"), 0) <= _safe_int(base.get("assertion_count"), 0)
        and _safe_int(candidate.get("target_api_call_count"), 0) >= _safe_int(base.get("target_api_call_count"), 0)
        and _safe_int(candidate.get("duplicate_group_size"), 1) < _safe_int(base.get("duplicate_group_size"), 1)
    ):
        reasons.append("repair_trigger_replaces_unknown_duplicate")
    if (
        str(candidate.get("origin") or "") == "repair_trigger"
        and candidate.get("buggy_outcome") == base.get("buggy_outcome")
        and bool(candidate.get("issue_aligned_fail")) == bool(base.get("issue_aligned_fail")) == True
        and str(candidate.get("oracle_risk_level") or "").upper() == str(base.get("oracle_risk_level") or "").upper() == "LOW"
        and _safe_int(candidate.get("over_specification_risk"), 0) == _safe_int(base.get("over_specification_risk"), 0) == 0
        and _safe_int(candidate.get("assertion_count"), 0) == _safe_int(base.get("assertion_count"), 0)
        and _safe_int(candidate.get("target_api_call_count"), 0) < _safe_int(base.get("target_api_call_count"), 0)
    ):
        reasons.append("repair_trigger_lower_target_surface")
    if (
        str(candidate.get("origin") or "") == "repair_setup"
        and candidate.get("buggy_outcome") == base.get("buggy_outcome")
        and bool(candidate.get("issue_aligned_fail")) == bool(base.get("issue_aligned_fail")) == True
        and str(candidate.get("oracle_risk_level") or "").upper() == str(base.get("oracle_risk_level") or "").upper() == "LOW"
        and _safe_int(candidate.get("over_specification_risk"), 0) == _safe_int(base.get("over_specification_risk"), 0) == 0
        and _safe_int(candidate.get("assertion_count"), 0) == _safe_int(base.get("assertion_count"), 0)
        and _safe_int(candidate.get("target_api_call_count"), 0) < _safe_int(base.get("target_api_call_count"), 0)
        and _safe_int(base.get("duplicate_group_size"), 1) >= 3
        and _safe_int(candidate.get("duplicate_group_size"), 1) <= 3
    ):
        reasons.append("repair_setup_lower_target_surface_duplicate_heavy")
    if (
        str(base.get("origin") or "") == "repair_trigger"
        and str(candidate.get("origin") or "") == "UNKNOWN"
        and _safe_int(candidate.get("round_id"), 0) > _safe_int(base.get("round_id"), 0)
        and bool(base.get("semantic_accept"))
        and bool(candidate.get("semantic_accept"))
        and base.get("combined_target_hit") == candidate.get("combined_target_hit") == "true"
        and candidate.get("buggy_outcome") == base.get("buggy_outcome")
        and bool(candidate.get("issue_aligned_fail")) == bool(base.get("issue_aligned_fail"))
        and str(candidate.get("oracle_risk_level") or "").upper() == str(base.get("oracle_risk_level") or "").upper() == "LOW"
        and _safe_int(candidate.get("over_specification_risk"), 0) <= _safe_int(base.get("over_specification_risk"), 0)
        and _safe_int(candidate.get("assertion_count"), 0) < _safe_int(base.get("assertion_count"), 0)
        and _safe_int(candidate.get("target_api_call_count"), 0) < _safe_int(base.get("target_api_call_count"), 0)
        and _safe_int(candidate.get("duplicate_group_size"), 1) == _safe_int(base.get("duplicate_group_size"), 1)
    ):
        reasons.append("oracle_simplified_after_repair_trigger")
    if (
        str(base.get("origin") or "") == "repair_trigger"
        and _safe_int(base.get("round_id"), 0) >= 3
        and str(candidate.get("origin") or "") == "generation"
        and _safe_int(candidate.get("round_id"), 0) == 0
        and candidate.get("buggy_outcome") == base.get("buggy_outcome")
        and not bool(base.get("issue_aligned_fail"))
        and not bool(candidate.get("issue_aligned_fail"))
        and base.get("combined_target_hit") == candidate.get("combined_target_hit") == "unknown"
        and str(candidate.get("oracle_risk_level") or "").upper() == str(base.get("oracle_risk_level") or "").upper() == "LOW"
        and _safe_int(candidate.get("over_specification_risk"), 0) == _safe_int(base.get("over_specification_risk"), 0) == 0
        and _safe_int(candidate.get("assertion_count"), 0) == _safe_int(base.get("assertion_count"), 0) == 0
        and _safe_int(candidate.get("target_api_call_count"), 0) == _safe_int(base.get("target_api_call_count"), 0)
        and _safe_int(candidate.get("duplicate_group_size"), 1) < _safe_int(base.get("duplicate_group_size"), 1)
    ):
        reasons.append("minimal_generation_tie_break_late_repair")

    # Do not trade away clear target evidence for a merely cleaner candidate.
    if base_target > cand_target and not any(reason.startswith("replace_high") or reason.startswith("avoid_mock") for reason in reasons):
        return []
    if not reasons:
        return []
    # Require at least one substantial risk/evidence improvement, not just a
    # late-round or origin preference.
    substantial = {
        "current_selection_not_valid_buggy_failure",
        "replace_high_oracle_risk_with_cleaner_candidate",
        "avoid_mock_based_candidate",
        "lower_oracle_over_specification",
        "candidate_has_issue_aligned_buggy_failure",
        "stronger_trigger_necessity_evidence",
        "stronger_bidirectional_evidence",
        "candidate_has_surrogate_consensus",
        "medium_oracle_simplification_to_generation",
        "repair_trigger_replaces_unknown_duplicate",
        "repair_trigger_lower_target_surface",
        "repair_setup_lower_target_surface_duplicate_heavy",
        "oracle_simplified_after_repair_trigger",
        "minimal_generation_tie_break_late_repair",
    }
    return [reason for reason in reasons if reason in substantial]


def _select_v2(
    instance_id: str,
    candidates: list[dict[str, Any]],
    feature_by_hash: dict[tuple[str, str], dict[str, Any]],
    cf_hash: str,
    legacy_hash: str,
) -> dict[str, Any] | None:
    ranked: list[tuple[list[Any], dict[str, Any], dict[str, Any]]] = []
    for candidate in candidates:
        row = feature_by_hash.get((instance_id, str(candidate.get("code_hash") or "")))
        if not row:
            continue
        key = selector_v2_rank_key(row)
        ranked.append((key, row, candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    cf_item = next((item for item in ranked if item[1].get("code_hash") == cf_hash), None)
    legacy_item = next((item for item in ranked if item[1].get("code_hash") == legacy_hash), None)
    base_item = cf_item or legacy_item or ranked[0]
    base_key, base_row, base_candidate = base_item
    fallback_to_legacy = False
    reason = "preserve_counterfactual_selection"
    selected_key = base_key
    selected_row = base_row
    selected_candidate = base_candidate
    if base_item is legacy_item and cf_item is None:
        fallback_to_legacy = True
        reason = "fallback_to_legacy_no_counterfactual_candidate_match"

    replacement_options: list[tuple[int, list[Any], list[str], dict[str, Any], dict[str, Any]]] = []
    for key, row, candidate in ranked:
        if row.get("code_hash") == base_row.get("code_hash"):
            continue
        reasons = _replacement_reasons(base_row, row)
        if not reasons:
            continue
        margin = (
            len(reasons) * 10
            + max(_structural_risk(base_row) - _structural_risk(row), 0)
            + max(_tri_rank(row.get("combined_target_hit")) - _tri_rank(base_row.get("combined_target_hit")), 0)
            + int(bool(row.get("issue_aligned_fail")) and not bool(base_row.get("issue_aligned_fail"))) * 2
        )
        replacement_options.append((margin, key, reasons, row, candidate))
    if replacement_options:
        replacement_options.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _margin, selected_key, reasons, selected_row, selected_candidate = replacement_options[0]
        reason = "conservative_replacement:" + ",".join(reasons)
    return {
        "candidate": selected_candidate,
        "feature": selected_row,
        "rank_key": selected_key,
        "ranked_candidate_ids": [item[1]["candidate_id"] for item in ranked[:10]],
        "ranked_code_hashes": [item[1]["code_hash"] for item in ranked[:10]],
        "selection_reason": reason,
        "fallback_to_legacy": fallback_to_legacy,
    }


def _write_feature_table(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    jsonl_path = output_dir / "candidate_feature_table.jsonl"
    csv_path = output_dir / "candidate_feature_table.csv"
    _write_jsonl(jsonl_path, rows)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _feature_summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"candidate_count": 0, "f2p_count": 0})
    for row in rows:
        value = str(row.get(key))
        stats[value]["candidate_count"] += 1
        if row.get("golden_is_f2p"):
            stats[value]["f2p_count"] += 1
    return {
        value: {
            **counts,
            "f2p_precision": round(_ratio(counts["f2p_count"], counts["candidate_count"]), 6),
        }
        for value, counts in sorted(stats.items())
    }


def _surrogate_confusion(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    for row in rows:
        pred = bool(row.get("surrogate_consensus")) or str(row.get("repair_sufficiency")) == "SUPPORTED" or _safe_int(row.get("surrogate_positive_pass_count"), 0) > 0
        actual = bool(row.get("golden_is_f2p"))
        if pred and actual:
            tp += 1
        elif pred and not actual:
            fp += 1
        elif not pred and actual:
            fn += 1
        else:
            tn += 1
    return {
        "TP": tp,
        "FP": fp,
        "FN": fn,
        "TN": tn,
        "precision": round(_ratio(tp, tp + fp), 6),
        "recall": round(_ratio(tp, tp + fn), 6),
    }


def _selection_gap_diagnosis(
    gap_instances: list[str],
    feature_rows_by_instance: dict[str, list[dict[str, Any]]],
    legacy_hashes: dict[str, str],
    cf_hashes: dict[str, str],
    legacy_results: dict[str, dict[str, Any]],
    cf_results: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    diagnoses: list[dict[str, Any]] = []
    cause_counts: Counter[str] = Counter()
    for instance_id in gap_instances:
        rows = feature_rows_by_instance.get(instance_id, [])
        f2ps = [row for row in rows if row.get("golden_is_f2p")]
        legacy_row = next((row for row in rows if row.get("code_hash") == legacy_hashes.get(instance_id)), None)
        cf_row = next((row for row in rows if row.get("code_hash") == cf_hashes.get(instance_id)), None)
        best_f2p = max(f2ps, key=selector_v2_rank_key) if f2ps else None
        wrong = cf_row or legacy_row
        causes = _misranking_causes(wrong, best_f2p)
        primary = causes[0] if causes else "insufficient_non_golden_separation"
        cause_counts[primary] += 1
        diagnoses.append(
            {
                "instance_id": instance_id,
                "legacy_selected_candidate": legacy_row.get("candidate_id") if legacy_row else "",
                "counterfactual_selected_candidate": cf_row.get("candidate_id") if cf_row else "",
                "best_known_f2p_candidate": best_f2p.get("candidate_id") if best_f2p else "",
                "selected_status": _status_label(cf_results.get(instance_id) or legacy_results.get(instance_id)),
                "f2p_candidate_origin": best_f2p.get("origin") if best_f2p else "",
                "selected_candidate_features": _compact_features(wrong) if wrong else {},
                "f2p_candidate_features": _compact_features(best_f2p) if best_f2p else {},
                "ranking_features_favoring_wrong_candidate": _favoring_features(wrong, best_f2p, prefer_wrong=True),
                "ranking_features_favoring_f2p_candidate": _favoring_features(wrong, best_f2p, prefer_wrong=False),
                "primary_misranking_cause": primary,
                "secondary_misranking_causes": causes[1:],
            }
        )
    return diagnoses, dict(sorted(cause_counts.items()))


def _compact_features(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    keep = [
        "candidate_id",
        "origin",
        "round_id",
        "buggy_outcome",
        "issue_aligned_fail",
        "runtime_target_hit",
        "combined_target_hit",
        "semantic_accept",
        "valid_surrogate_count",
        "surrogate_positive_pass_count",
        "repair_sufficiency",
        "negative_control_status",
        "trigger_necessity",
        "bidirectional_support",
        "oracle_risk_level",
        "assertion_count",
        "over_specification_risk",
        "internal_private_attribute_assertion",
        "exact_full_string_assertion_count",
        "mock_count",
        "target_api_call_count",
        "duplicate_group_size",
        "legacy_score",
        "golden_status",
    ]
    return {key: row.get(key) for key in keep}


def _misranking_causes(wrong: dict[str, Any] | None, f2p: dict[str, Any] | None) -> list[str]:
    causes: list[str] = []
    if not wrong or not f2p:
        return causes
    if _safe_int(wrong.get("surrogate_positive_pass_count"), 0) > _safe_int(f2p.get("surrogate_positive_pass_count"), 0):
        causes.append("surrogate_false_confidence")
    if bool(wrong.get("semantic_accept")) and not bool(f2p.get("semantic_accept")):
        causes.append("semantic_verifier_overconfidence")
    if _safe_int(wrong.get("over_specification_risk"), 0) > _safe_int(f2p.get("over_specification_risk"), 0):
        causes.append("oracle_over_specification")
    if _safe_int(wrong.get("mock_count"), 0) > _safe_int(f2p.get("mock_count"), 0):
        causes.append("public_api_fidelity_mock_or_private_path")
    if _safe_int(wrong.get("test_code_lines"), 0) > _safe_int(f2p.get("test_code_lines"), 0) + 20:
        causes.append("mutation_distance_or_complexity_bias")
    if _safe_int(wrong.get("round_id"), 0) > _safe_int(f2p.get("round_id"), 0):
        causes.append("late_round_bias")
    if _safe_int(wrong.get("duplicate_group_size"), 1) > _safe_int(f2p.get("duplicate_group_size"), 1):
        causes.append("duplicate_bias")
    if _tri_rank(wrong.get("combined_target_hit")) > _tri_rank(f2p.get("combined_target_hit")):
        causes.append("target_hit_misinterpretation")
    if wrong.get("origin") != f2p.get("origin"):
        causes.append("candidate_origin_bias")
    if not causes:
        causes.append("weak_or_missing_counterfactual_evidence")
    return causes


def _favoring_features(wrong: dict[str, Any] | None, f2p: dict[str, Any] | None, *, prefer_wrong: bool) -> list[str]:
    if not wrong or not f2p:
        return []
    comparisons = [
        ("runtime_target_hit", _tri_rank(wrong.get("runtime_target_hit")), _tri_rank(f2p.get("runtime_target_hit"))),
        ("semantic_accept", int(bool(wrong.get("semantic_accept"))), int(bool(f2p.get("semantic_accept")))),
        ("surrogate_positive_pass_count", _safe_int(wrong.get("surrogate_positive_pass_count")), _safe_int(f2p.get("surrogate_positive_pass_count"))),
        ("oracle_risk_score", _risk_score(str(wrong.get("oracle_risk_level")), _safe_int(wrong.get("over_specification_risk"))), _risk_score(str(f2p.get("oracle_risk_level")), _safe_int(f2p.get("over_specification_risk")))),
        ("assertion_shape", 2 if 1 <= _safe_int(wrong.get("assertion_count")) <= 2 else 0, 2 if 1 <= _safe_int(f2p.get("assertion_count")) <= 2 else 0),
        ("target_api_call_count", _safe_int(wrong.get("target_api_call_count")), _safe_int(f2p.get("target_api_call_count"))),
        ("origin_rank", _origin_rank(str(wrong.get("origin"))), _origin_rank(str(f2p.get("origin")))),
        ("duplicate_group_size_inverse", -_safe_int(wrong.get("duplicate_group_size"), 1), -_safe_int(f2p.get("duplicate_group_size"), 1)),
    ]
    out: list[str] = []
    for name, wrong_value, f2p_value in comparisons:
        if prefer_wrong and wrong_value > f2p_value:
            out.append(name)
        elif not prefer_wrong and f2p_value > wrong_value:
            out.append(name)
    return out


def _global_diagnosis(rows: list[dict[str, Any]], selected_non_f2p_pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    feature_keys = [
        "runtime_target_hit",
        "combined_target_hit",
        "semantic_accept",
        "surrogate_consensus",
        "repair_sufficiency",
        "oracle_risk_level",
        "assertion_count",
        "public_api_public_state_assertion",
        "internal_private_attribute_assertion",
        "origin",
        "round_id",
        "duplicate_group_size",
        "negative_control_status",
        "trigger_necessity",
        "bidirectional_support",
    ]
    pairwise: Counter[str] = Counter()
    for wrong, f2p in selected_non_f2p_pairs:
        for feature in _favoring_features(wrong, f2p, prefer_wrong=False):
            pairwise[f"{feature}_points_to_f2p"] += 1
        for feature in _favoring_features(wrong, f2p, prefer_wrong=True):
            pairwise[f"{feature}_points_to_wrong"] += 1
    return {
        "feature_precision": {key: _feature_summary(rows, key) for key in feature_keys},
        "pairwise_f2p_vs_selected_non_f2p": dict(sorted(pairwise.items())),
        "surrogate_confusion_matrix": _surrogate_confusion(rows),
        "unknown_rates": {
            "runtime_target_hit_unknown": round(_ratio(sum(1 for r in rows if r.get("runtime_target_hit") == "unknown"), len(rows)), 6),
            "trigger_necessity_unknown": round(_ratio(sum(1 for r in rows if r.get("trigger_necessity") == "UNKNOWN"), len(rows)), 6),
            "repair_sufficiency_unknown": round(_ratio(sum(1 for r in rows if r.get("repair_sufficiency") == "UNKNOWN"), len(rows)), 6),
            "bidirectional_support_unknown": round(_ratio(sum(1 for r in rows if r.get("bidirectional_support") == "UNKNOWN"), len(rows)), 6),
        },
        "old_features_rewarding_fixed_fail": _old_feature_risks(rows),
        "stable_f2p_features": _stable_f2p_features(rows),
    }


def _old_feature_risks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    for key in ["semantic_accept", "surrogate_consensus", "repair_sufficiency", "runtime_target_hit", "oracle_risk_level", "origin"]:
        summary = _feature_summary(rows, key)
        for value, stats in summary.items():
            if stats["candidate_count"] >= 10 and stats["f2p_precision"] < 0.2:
                risks.append({"feature": key, "value": value, **stats})
    return sorted(risks, key=lambda item: (item["f2p_precision"], -item["candidate_count"]))[:20]


def _stable_f2p_features(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for key in ["buggy_outcome", "issue_aligned_fail", "runtime_target_hit", "combined_target_hit", "oracle_risk_level", "origin", "round_id", "assertion_count"]:
        summary = _feature_summary(rows, key)
        for value, stats in summary.items():
            if stats["candidate_count"] >= 5 and stats["f2p_precision"] >= 0.3:
                signals.append({"feature": key, "value": value, **stats})
    return sorted(signals, key=lambda item: (-item["f2p_precision"], -item["f2p_count"]))[:20]


def _write_md_reports(
    output_dir: Path,
    gap_diagnosis: list[dict[str, Any]],
    gap_cause_counts: dict[str, int],
    global_diag: dict[str, Any],
    duplicate_summary: dict[str, Any],
) -> None:
    gap_lines = [
        "# Selector V2 Selection Gap Diagnosis",
        "",
        "## Cause Distribution",
        "",
    ]
    for cause, count in gap_cause_counts.items():
        gap_lines.append(f"- {cause}: {count}")
    gap_lines.extend(["", "## Instances", ""])
    for row in gap_diagnosis:
        gap_lines.append(f"### {row['instance_id']}")
        gap_lines.append(f"- selected status: {row['selected_status']}")
        gap_lines.append(f"- legacy: {row['legacy_selected_candidate']}")
        gap_lines.append(f"- counterfactual: {row['counterfactual_selected_candidate']}")
        gap_lines.append(f"- best known F2P: {row['best_known_f2p_candidate']} ({row['f2p_candidate_origin']})")
        gap_lines.append(f"- primary cause: {row['primary_misranking_cause']}")
        if row["secondary_misranking_causes"]:
            gap_lines.append(f"- secondary: {', '.join(row['secondary_misranking_causes'])}")
        gap_lines.append("")
    (output_dir / "selection_gap_diagnosis.md").write_text("\n".join(gap_lines) + "\n", encoding="utf-8")

    global_lines = [
        "# Selector V2 Global Feature Diagnosis",
        "",
        "## Surrogate Confusion Matrix",
        "",
        json.dumps(global_diag["surrogate_confusion_matrix"], ensure_ascii=False, indent=2),
        "",
        "## Unknown Rates",
        "",
        json.dumps(global_diag["unknown_rates"], ensure_ascii=False, indent=2),
        "",
        "## Old Ranking Features That Reward FIXED_FAIL",
        "",
    ]
    for item in global_diag["old_features_rewarding_fixed_fail"]:
        global_lines.append(f"- {item['feature']}={item['value']}: precision={item['f2p_precision']} count={item['candidate_count']}")
    global_lines.extend(["", "## Stable F2P Signals", ""])
    for item in global_diag["stable_f2p_features"]:
        global_lines.append(f"- {item['feature']}={item['value']}: precision={item['f2p_precision']} f2p={item['f2p_count']} count={item['candidate_count']}")
    global_lines.extend(["", "## Duplicate Summary", "", json.dumps(duplicate_summary, ensure_ascii=False, indent=2)])
    (output_dir / "global_feature_diagnosis.md").write_text("\n".join(global_lines) + "\n", encoding="utf-8")


def _copy_export(instance_id: str, selected: dict[str, Any], export_dir: Path) -> str:
    candidate = selected["candidate"]
    src = Path(str(candidate.get("code_path") or ""))
    dst_dir = export_dir / instance_id
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "final_test.py"
    shutil.copy2(src, dst)
    metadata = {
        "instance_id": instance_id,
        "selector_version": SELECTOR_VERSION,
        "source_code_path": str(src),
        "candidate_id": selected["feature"]["candidate_id"],
        "code_hash": selected["feature"]["code_hash"],
        "selection_reason": selected["selection_reason"],
        "rank_key": selected["rank_key"],
    }
    _write_json(dst_dir / "selection.json", metadata)
    return str(dst)


def _manifest_hash(path: Path) -> str:
    return _sha256_file(path)


def _run_targeted_formal(
    run_dir: Path,
    instances_path: Path,
    output_dir: Path,
    target_instances: list[str],
    *,
    max_workers: int,
    timeout: int,
    repo_root_base: Path,
    resume: bool,
) -> dict[str, Any]:
    formal_dir = output_dir / "formal_changed_instances"
    if not target_instances:
        summary = {"skipped": True, "reason": "no changed uncached selector_v2 candidates", "target_instances": []}
        _write_json(formal_dir / "summary.json", summary)
        return summary
    rows, rows_by_id = _load_rows(instances_path)
    subset = [rows_by_id[iid] for iid in target_instances if iid in rows_by_id]
    dataset_path = output_dir / "changed_instances_dataset.json"
    _write_json(dataset_path, subset)
    summary_path = formal_dir / "formal_changed_instances_summary.json"
    log_path = formal_dir / "formal_changed_instances.log"
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts/run_formal_eval_after_generation.py"),
        "--outputs_dir",
        str(output_dir / "export"),
        "--dataset_file",
        str(dataset_path),
        "--repo_root_base",
        str(repo_root_base),
        "--max_workers",
        str(max_workers),
        "--timeout",
        str(timeout),
        "--evaluation_dir",
        str(formal_dir),
        "--eval_clone_root",
        str(formal_dir / "eval_clones"),
        "--log_path",
        str(log_path),
        "--summary_path",
        str(summary_path),
        "--compute_patch_coverage",
        "false",
        "--missing_generated_policy",
        "abort",
    ]
    if resume:
        cmd.append("--resume")
    started = _now_iso()
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), text=True)
    finished = _now_iso()
    summary = _read_json(summary_path, {})
    if not isinstance(summary, dict):
        summary = {}
    summary.update(
        {
            "selector_v2_targeted_formal_command": cmd,
            "selector_v2_target_instances": target_instances,
            "selector_v2_started_at": started,
            "selector_v2_finished_at": finished,
            "selector_v2_returncode": proc.returncode,
        }
    )
    _write_json(formal_dir / "summary.json", summary)
    return summary


def _merge_selector_results(
    all_instance_ids: list[str],
    selections: dict[str, dict[str, Any]],
    cf_results: dict[str, dict[str, Any]],
    legacy_results: dict[str, dict[str, Any]],
    formal_cache: dict[tuple[str, str], dict[str, Any]],
    targeted_results: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for instance_id in all_instance_ids:
        selected = selections.get(instance_id)
        if not selected:
            row = dict(cf_results.get(instance_id) or legacy_results.get(instance_id) or {})
            row.setdefault("instance_id", instance_id)
            row.setdefault("status", "MISSING_GENERATED_TEST")
            row["selector_v2_status_source"] = "fallback_counterfactual_or_legacy_no_candidate_pool"
            merged[instance_id] = row
            continue
        code_hash = str(selected["feature"]["code_hash"])
        cache = formal_cache.get((instance_id, code_hash))
        targeted = targeted_results.get(instance_id)
        if isinstance(targeted, dict) and targeted.get("status"):
            row = dict(targeted)
            row["selector_v2_status_source"] = "targeted_formal_changed_instances"
        elif isinstance(cache, dict) and cache.get("formal_status"):
            row = {
                "instance_id": instance_id,
                "status": str(cache.get("formal_status")),
                "success": bool(cache.get("is_f2p")),
                "buggy_status": cache.get("buggy_status"),
                "fixed_status": cache.get("fixed_status"),
                "selector_v2_status_source": str(cache.get("cache_source") or "candidate_formal_cache"),
            }
        else:
            cf_row = cf_results.get(instance_id) if code_hash == selected.get("counterfactual_candidate_hash") else None
            if isinstance(cf_row, dict) and cf_row.get("status"):
                row = dict(cf_row)
                row["selector_v2_status_source"] = "reused_counterfactual_same_candidate"
            else:
                row = {"instance_id": instance_id, "status": "UNKNOWN_UNEVALUATED", "success": False, "selector_v2_status_source": "missing_candidate_formal_result"}
        row["selector_v2_candidate_id"] = selected["feature"]["candidate_id"]
        row["selector_v2_code_hash"] = code_hash
        merged[instance_id] = row
    success = sum(1 for row in merged.values() if row.get("status") == F2P_STATUS)
    by_status = Counter(str(row.get("status") or "UNKNOWN") for row in merged.values())
    metrics = {
        "selector_version": SELECTOR_VERSION,
        "dataset_total": len(all_instance_ids),
        "total_instances": len(all_instance_ids),
        "f2p_success": success,
        "f2p_fail": max(len(all_instance_ids) - success, 0),
        "f2p_at_1": _ratio(success, len(all_instance_ids)),
        "f2p_at_1_percent": round(_ratio(success, len(all_instance_ids)) * 100, 4),
        "by_status": dict(sorted(by_status.items())),
        "patch_coverage_enabled": False,
    }
    return merged, metrics


def _comparison(
    all_ids: list[str],
    selector_results: dict[str, dict[str, Any]],
    legacy_results: dict[str, dict[str, Any]],
    cf_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    selector_success = {iid for iid in all_ids if _status_is_success(selector_results.get(iid))}
    legacy_success = {iid for iid in all_ids if _status_is_success(legacy_results.get(iid))}
    cf_success = {iid for iid in all_ids if _status_is_success(cf_results.get(iid))}
    return {
        "selector_version": SELECTOR_VERSION,
        "dataset_total": len(all_ids),
        "selector_v2_success": len(selector_success),
        "legacy_success": len(legacy_success),
        "counterfactual_success": len(cf_success),
        "selector_v2_f2p_percent": round(_ratio(len(selector_success), len(all_ids)) * 100, 4),
        "legacy_rescued_by_selector_v2": sorted(selector_success - legacy_success),
        "legacy_lost_by_selector_v2": sorted(legacy_success - selector_success),
        "legacy_net_gain": len(selector_success) - len(legacy_success),
        "counterfactual_rescued_by_selector_v2": sorted(selector_success - cf_success),
        "counterfactual_lost_by_selector_v2": sorted(cf_success - selector_success),
        "counterfactual_net_gain": len(selector_success) - len(cf_success),
        "target_reached": len(selector_success) >= TARGET_F2P,
    }


def _source_selection_success(rows: list[dict[str, Any]], selections: dict[str, dict[str, Any]], selector_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    f2p_by_origin: Counter[str] = Counter()
    count_by_origin: Counter[str] = Counter()
    selected_by_origin: Counter[str] = Counter()
    selected_success_by_origin: Counter[str] = Counter()
    for row in rows:
        count_by_origin[str(row.get("origin"))] += 1
        if row.get("golden_is_f2p"):
            f2p_by_origin[str(row.get("origin"))] += 1
    for instance_id, selected in selections.items():
        origin = str(selected["feature"].get("origin"))
        selected_by_origin[origin] += 1
        if _status_is_success(selector_results.get(instance_id)):
            selected_success_by_origin[origin] += 1
    origins = sorted(set(count_by_origin) | set(selected_by_origin))
    return {
        origin: {
            "candidate_count": count_by_origin.get(origin, 0),
            "known_f2p_candidate_count": f2p_by_origin.get(origin, 0),
            "known_f2p_precision": round(_ratio(f2p_by_origin.get(origin, 0), count_by_origin.get(origin, 0)), 6),
            "selector_v2_selected_count": selected_by_origin.get(origin, 0),
            "selector_v2_selected_f2p_count": selected_success_by_origin.get(origin, 0),
            "selector_v2_selected_success_rate": round(_ratio(selected_success_by_origin.get(origin, 0), selected_by_origin.get(origin, 0)), 6),
        }
        for origin in origins
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and evaluate offline Selector V2 for SWT-Lite checkpoint pools.")
    parser.add_argument("--run_dir", default=str(RUN_DIR))
    parser.add_argument("--instances_path", default=str(PROJECT_ROOT / DEFAULT_INSTANCES_PATH))
    parser.add_argument("--fast_pool_dir", default=str(FAST_POOL_DIR))
    parser.add_argument("--output_dir", default="")
    parser.add_argument("--repo_root_base", default=str(ROOT / "swe_repos"))
    parser.add_argument("--max_workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--skip_formal_eval", action="store_true")
    parser.add_argument("--resume_formal", action="store_true")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    instances_path = Path(args.instances_path).resolve()
    fast_pool_dir = Path(args.fast_pool_dir).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else run_dir / "analysis" / SELECTOR_VERSION
    export_dir = output_dir / "export"
    ensure_dir(str(output_dir))
    ensure_dir(str(export_dir))

    rows, rows_by_id = _load_rows(instances_path)
    all_ids = [str(row["instance_id"]) for row in rows if row.get("instance_id")]
    pool_summary = _read_json(fast_pool_dir / "pool_upper_bound_summary.json", {})
    pool_success_ids = sorted(set(pool_summary.get("known_pool_success_instances", [])) | set(pool_summary.get("new_pool_success_instances", [])))
    gap_instances = sorted(pool_summary.get("new_pool_success_instances", []))

    candidates_by_instance, enum_manifest = _enumerate_candidates(run_dir, rows_by_id)
    duplicate_group_sizes, duplicate_summary = _raw_duplicate_stats(run_dir, rows_by_id)
    formal_cache = _formal_status_cache(run_dir, fast_pool_dir)
    legacy_manifest = _selection_manifest(run_dir, "legacy")
    cf_manifest = _selection_manifest(run_dir, "counterfactual")
    legacy_results = _merged_results(run_dir, "legacy")
    cf_results = _merged_results(run_dir, "counterfactual")
    legacy_hashes = {iid: _selected_hash(row) for iid, row in legacy_manifest.items()}
    cf_hashes = {iid: _selected_hash(row) for iid, row in cf_manifest.items()}

    feature_rows_all, feature_by_hash = _build_feature_rows(
        run_dir,
        rows_by_id,
        candidates_by_instance,
        duplicate_group_sizes,
        formal_cache,
        legacy_manifest,
        cf_manifest,
    )
    feature_rows = [row for row in feature_rows_all if row["instance_id"] in pool_success_ids]
    _write_feature_table(output_dir, feature_rows)
    feature_rows_by_instance: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in feature_rows:
        feature_rows_by_instance[row["instance_id"]].append(row)

    gap_records = []
    for instance_id in gap_instances:
        f2ps = [row for row in feature_rows_by_instance.get(instance_id, []) if row.get("golden_is_f2p")]
        gap_records.append(
            {
                "instance_id": instance_id,
                "known_f2p_candidate_ids": [row["candidate_id"] for row in f2ps],
                "known_f2p_code_hashes": [row["code_hash"] for row in f2ps],
                "legacy_selected_candidate_id": next((row["candidate_id"] for row in feature_rows_by_instance.get(instance_id, []) if row["code_hash"] == legacy_hashes.get(instance_id)), ""),
                "counterfactual_selected_candidate_id": next((row["candidate_id"] for row in feature_rows_by_instance.get(instance_id, []) if row["code_hash"] == cf_hashes.get(instance_id)), ""),
                "legacy_selected_status": _status_label(legacy_results.get(instance_id)),
                "counterfactual_selected_status": _status_label(cf_results.get(instance_id)),
                "legacy_code_hash": legacy_hashes.get(instance_id, ""),
                "counterfactual_code_hash": cf_hashes.get(instance_id, ""),
            }
        )
    _write_json(output_dir / "selection_gap_instances.json", gap_records)

    gap_diagnosis, gap_cause_counts = _selection_gap_diagnosis(
        gap_instances,
        feature_rows_by_instance,
        legacy_hashes,
        cf_hashes,
        legacy_results,
        cf_results,
    )
    _write_json(output_dir / "selection_gap_diagnosis.json", {"instances": gap_diagnosis, "cause_distribution": gap_cause_counts})

    selected_non_f2p_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for instance_id in pool_success_ids:
        rows_for_instance = feature_rows_by_instance.get(instance_id, [])
        selected = next((row for row in rows_for_instance if row["code_hash"] == cf_hashes.get(instance_id)), None)
        if selected and not selected.get("golden_is_f2p"):
            f2ps = [row for row in rows_for_instance if row.get("golden_is_f2p")]
            if f2ps:
                selected_non_f2p_pairs.append((selected, max(f2ps, key=selector_v2_rank_key)))
    global_diag = _global_diagnosis(feature_rows, selected_non_f2p_pairs)
    _write_json(output_dir / "global_feature_diagnosis.json", global_diag)
    _write_json(output_dir / "duplicate_candidate_analysis.json", duplicate_summary)
    _write_md_reports(output_dir, gap_diagnosis, gap_cause_counts, global_diag, duplicate_summary)

    selections: dict[str, dict[str, Any]] = {}
    manifest_rows: list[dict[str, Any]] = []
    changed_uncached: list[str] = []
    for instance_id in all_ids:
        selected = _select_v2(
            instance_id,
            candidates_by_instance.get(instance_id, []),
            feature_by_hash,
            cf_hashes.get(instance_id, ""),
            legacy_hashes.get(instance_id, ""),
        )
        if selected is None:
            manifest_rows.append(
                {
                    "instance_id": instance_id,
                    "legacy_candidate_id": "",
                    "counterfactual_candidate_id": "",
                    "selector_v2_candidate_id": "",
                    "selector_v2_changed_from_legacy": False,
                    "selector_v2_changed_from_counterfactual": False,
                    "selection_reason": "no_candidate_pool_fallback_to_counterfactual_or_missing",
                    "rank_key": [],
                    "fallback_to_legacy": False,
                    "status": "MISSING_CANDIDATE_POOL",
                }
            )
            continue
        selected["counterfactual_candidate_hash"] = cf_hashes.get(instance_id, "")
        selections[instance_id] = selected
        exported = _copy_export(instance_id, selected, export_dir)
        code_hash = selected["feature"]["code_hash"]
        changed_cf = bool(cf_hashes.get(instance_id) and code_hash != cf_hashes.get(instance_id))
        changed_legacy = bool(legacy_hashes.get(instance_id) and code_hash != legacy_hashes.get(instance_id))
        cache = formal_cache.get((instance_id, code_hash))
        if changed_cf and not (isinstance(cache, dict) and cache.get("formal_status")):
            changed_uncached.append(instance_id)
        manifest_rows.append(
            {
                "instance_id": instance_id,
                "legacy_candidate_id": next((row["candidate_id"] for row in feature_rows_by_instance.get(instance_id, []) if row["code_hash"] == legacy_hashes.get(instance_id)), ""),
                "counterfactual_candidate_id": next((row["candidate_id"] for row in feature_rows_by_instance.get(instance_id, []) if row["code_hash"] == cf_hashes.get(instance_id)), ""),
                "selector_v2_candidate_id": selected["feature"]["candidate_id"],
                "legacy_code_hash": legacy_hashes.get(instance_id, ""),
                "counterfactual_code_hash": cf_hashes.get(instance_id, ""),
                "selector_v2_code_hash": code_hash,
                "selector_v2_changed_from_legacy": changed_legacy,
                "selector_v2_changed_from_counterfactual": changed_cf,
                "selection_reason": selected["selection_reason"],
                "rank_key": selected["rank_key"],
                "fallback_to_legacy": bool(selected["fallback_to_legacy"]),
                "exported_final_test": exported,
                "candidate_formal_cache_status": _status_label(cache),
                "candidate_formal_cache_source": str((cache or {}).get("cache_source") or ""),
                "ranked_candidate_ids_top10": selected["ranked_candidate_ids"],
            }
        )
    manifest_path = export_dir / "selection_manifest.jsonl"
    _write_jsonl(manifest_path, manifest_rows)
    selector_code_hash = _sha256_file(Path(__file__))
    config = {
        "selector_version": SELECTOR_VERSION,
        "created_at": _now_iso(),
        "run_dir": str(run_dir),
        "instances_path": str(instances_path),
        "fast_pool_dir": str(fast_pool_dir),
        "output_dir": str(output_dir),
        "selector_code_path": str(Path(__file__).resolve()),
        "selector_code_hash": selector_code_hash,
        "manifest_hash": _manifest_hash(manifest_path),
        "non_golden_selector_fields": NON_GOLDEN_SELECTOR_FIELDS,
        "target_f2p": TARGET_F2P,
        "pool_upper_bound_U": pool_summary.get("U"),
        "selection_gap_instances": gap_instances,
        "changed_uncached_instances_before_targeted_formal": changed_uncached,
    }
    _write_json(export_dir / "manifest.json", config)

    if args.skip_formal_eval:
        targeted_summary = {"skipped": True, "reason": "--skip_formal_eval", "target_instances": changed_uncached}
        _write_json(output_dir / "formal_changed_instances" / "summary.json", targeted_summary)
    else:
        targeted_summary = _run_targeted_formal(
            run_dir,
            instances_path,
            output_dir,
            sorted(changed_uncached),
            max_workers=args.max_workers,
            timeout=args.timeout,
            repo_root_base=Path(args.repo_root_base),
            resume=args.resume_formal,
        )

    targeted_results = _read_json(output_dir / "formal_changed_instances" / "merged_results_with_missing.json", {})
    if not targeted_results:
        targeted_results = _read_json(output_dir / "formal_changed_instances" / "merged_results.json", {})
    targeted_results = targeted_results if isinstance(targeted_results, dict) else {}
    selector_results, metrics = _merge_selector_results(
        all_ids,
        selections,
        cf_results,
        legacy_results,
        formal_cache,
        targeted_results,
    )
    _write_json(output_dir / "selector_v2_merged_results.json", selector_results)
    _write_json(output_dir / "selector_v2_metrics_276.json", metrics)
    comparison = _comparison(all_ids, selector_results, legacy_results, cf_results)
    comparison.update(
        {
            "selector_v2_changed_from_counterfactual": sum(1 for row in manifest_rows if row.get("selector_v2_changed_from_counterfactual")),
            "selector_v2_changed_from_legacy": sum(1 for row in manifest_rows if row.get("selector_v2_changed_from_legacy")),
            "changed_uncached_instances": sorted(changed_uncached),
            "targeted_formal_summary": targeted_summary,
            "pool_upper_bound": {
                "U": pool_summary.get("U"),
                "legacy_selection_gap": pool_summary.get("legacy_selection_gap"),
                "counterfactual_selection_gap": pool_summary.get("counterfactual_selection_gap"),
            },
        }
    )
    _write_json(output_dir / "selector_v2_comparison_summary.json", comparison)
    _write_json(output_dir / "source_selection_success.json", _source_selection_success(feature_rows, selections, selector_results))
    print(json.dumps({"metrics": metrics, "comparison": comparison, "output_dir": str(output_dir)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
