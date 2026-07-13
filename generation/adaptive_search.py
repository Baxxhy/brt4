"""Explainable adaptive typed-search controller and online Selector V2."""

from __future__ import annotations

import ast
import builtins
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.schema import (
    AdaptiveSearchDecision,
    BehaviorTarget,
    CandidateArchiveEntry,
    CandidateCheckpoint,
    CandidateTest,
)
from .archive import CandidateArchive
from .segmenter import preservation_report, segment_test


ERROR_STATUSES = {"SETUP_ERROR", "COLLECT_ERROR", "SYNTAX_ERROR", "TIMEOUT", "ERROR"}


@dataclass
class SearchBudgets:
    extra_unique: int = 3
    starting_unique: int = 1
    protocol: int = 1
    trigger: int = 2
    oracle: int = 2
    recomposition: int = 1


def _status(execution: dict[str, Any]) -> str:
    return str(execution.get("outcome") or execution.get("status") or "UNKNOWN").upper()


def _surrogate_supported(surrogate: dict[str, Any] | None) -> bool:
    return str((surrogate or {}).get("status") or "").upper() in {
        "F2P_SUCCESS",
        "SURROGATE_F2P_SUCCESS",
    }


def _surrogate_assertion_fail(surrogate: dict[str, Any] | None) -> bool:
    data = surrogate or {}
    if "ASSERT" in str(data.get("status") or "").upper():
        return True
    patched = data.get("patched_execution")
    if isinstance(patched, dict):
        return str(patched.get("exception_type") or "") == "AssertionError"
    return False


def _next_unexplored(
    counts: dict[str, int],
    budgets: SearchBudgets,
    prefer: list[str],
) -> tuple[str, str]:
    limits = {
        "protocol_repair": budgets.protocol,
        "trigger_search": budgets.trigger,
        "minimal_oracle_search": budgets.oracle,
        "optional_recomposition": budgets.recomposition,
    }
    variants = {
        "trigger_search": ["contract_complete", "path_diverse"],
        "minimal_oracle_search": ["conservative", "public_invariant"],
        "protocol_repair": ["repository_native_scaffold"],
        "optional_recomposition": ["compatible_best_segments"],
    }
    for action in prefer:
        used = counts.get(action, 0)
        if used < limits[action]:
            options = variants[action]
            return action, options[min(used, len(options) - 1)]
    return "stop", ""


def decide_search_action(
    execution: dict[str, Any],
    verifier: dict[str, Any],
    runtime_target_hit: str,
    oracle_risk: dict[str, Any],
    surrogate: dict[str, Any] | None,
    branch_counts: dict[str, int],
    archive: CandidateArchive,
    budgets: SearchBudgets,
    duplicate: bool = False,
    allow_recomposition: bool = True,
) -> AdaptiveSearchDecision:
    status = _status(execution)
    issue_fail = str(verifier.get("decision") or "").lower() == "accept" or status == "ISSUE_ALIGNED_FAIL"
    target_hit = str(runtime_target_hit or "unknown").lower()
    risk = str(oracle_risk.get("level") or "MEDIUM").upper()
    evidence = {
        "buggy_status": status,
        "issue_aligned_fail": issue_fail,
        "runtime_target_hit": target_hit,
        "oracle_risk": risk,
        "surrogate_status": str((surrogate or {}).get("status") or "UNKNOWN"),
        "unique_candidates": archive.unique_count,
        "branch_counts": dict(branch_counts),
        "duplicate": duplicate,
    }
    if archive.unique_count >= budgets.starting_unique + budgets.extra_unique:
        return AdaptiveSearchDecision("stop", reason="extra unique candidate budget exhausted", evidence=evidence)
    if issue_fail and _surrogate_supported(surrogate) and risk == "LOW":
        return AdaptiveSearchDecision("stop", reason="high-confidence surrogate-supported issue failure", evidence=evidence)
    if duplicate:
        prefer = ["trigger_search", "minimal_oracle_search", "protocol_repair"]
        if allow_recomposition:
            prefer.append("optional_recomposition")
        action, variant = _next_unexplored(branch_counts, budgets, prefer)
        return AdaptiveSearchDecision(
            action,
            variant,
            "duplicate redirected to an unexplored typed-search branch",
            evidence,
            abstain=action == "stop",
        )
    if status in ERROR_STATUSES:
        action, variant = _next_unexplored(branch_counts, budgets, ["protocol_repair"])
        return AdaptiveSearchDecision(action, variant, "candidate protocol is not executable", evidence, action == "stop")
    if status in {"PASS", "BUGGY_PASS"} or target_hit == "false":
        action, variant = _next_unexplored(branch_counts, budgets, ["trigger_search"])
        return AdaptiveSearchDecision(action, variant, "buggy pass or runtime target miss requires trigger search", evidence, action == "stop")
    if issue_fail and risk in {"MEDIUM", "HIGH"}:
        action, variant = _next_unexplored(branch_counts, budgets, ["minimal_oracle_search"])
        return AdaptiveSearchDecision(action, variant, "issue failure has a risky oracle", evidence, action == "stop")
    if _surrogate_assertion_fail(surrogate):
        action, variant = _next_unexplored(branch_counts, budgets, ["minimal_oracle_search"])
        return AdaptiveSearchDecision(action, variant, "surrogate remains assertion-failing", evidence, action == "stop")
    decision_name = str(verifier.get("decision") or "").lower()
    if decision_name == "repair_setup":
        action, variant = _next_unexplored(branch_counts, budgets, ["protocol_repair"])
        return AdaptiveSearchDecision(action, variant, "verifier requested protocol repair", evidence, action == "stop")
    if decision_name == "repair_oracle":
        action, variant = _next_unexplored(branch_counts, budgets, ["minimal_oracle_search"])
        return AdaptiveSearchDecision(action, variant, "verifier requested oracle repair", evidence, action == "stop")
    if decision_name in {"repair_trigger", "reject"} or target_hit == "unknown":
        action, variant = _next_unexplored(branch_counts, budgets, ["trigger_search"])
        return AdaptiveSearchDecision(action, variant, "trigger evidence is incomplete", evidence, action == "stop")
    if allow_recomposition and archive.unique_count >= 2:
        action, variant = _next_unexplored(branch_counts, budgets, ["optional_recomposition"])
        return AdaptiveSearchDecision(action, variant, "typed branches exhausted; try compatible segment recomposition", evidence, action == "stop")
    return AdaptiveSearchDecision("stop", reason="no evidence-backed search action remains", evidence=evidence)


def search_prompt_context(
    action: str,
    variant: str,
    behavior: BehaviorTarget,
    segments: dict[str, Any],
) -> dict[str, Any]:
    return {
        "method": "ATS-BRT",
        "action": action,
        "variant": variant,
        "trigger_contract": behavior.trigger_contract,
        "failure_contract": behavior.failure_contract,
        "expected_contract": behavior.expected_contract,
        "localization_contract": behavior.localization_contract,
        "segment_hashes": {
            "scaffold": segments.get("scaffold_hash", ""),
            "trigger": segments.get("trigger_hash", ""),
            "oracle": segments.get("oracle_hash", ""),
        },
        "constraints": {
            "protocol_repair": "change scaffold only; preserve trigger and oracle",
            "trigger_search": "change trigger only; preserve scaffold and oracle",
            "minimal_oracle_search": "change oracle only; preserve scaffold and trigger; use at most two core assertions",
            "optional_recomposition": "combine only AST-compatible archived segments",
        }.get(action, "stop"),
    }


def validate_typed_transformation(
    before_code: str,
    after_code: str,
    behavior: BehaviorTarget,
    action: str,
) -> dict[str, Any]:
    mutable = {
        "protocol_repair": "scaffold",
        "trigger_search": "trigger",
        "minimal_oracle_search": "oracle",
    }.get(action, "")
    before = segment_test(before_code, behavior)
    after = segment_test(after_code, behavior)
    report = preservation_report(before, after, mutable) if mutable else {
        "valid": False,
        "frozen_segments_changed": ["unknown_action"],
        "target_api_preserved": bool(after.target_call_locations),
    }
    lowered = after_code.lower()
    forbidden = []
    for token in ("pytest.skip", "@pytest.mark.skip", "xfail", "assert true"):
        if token in lowered:
            forbidden.append(token)
    if action == "minimal_oracle_search" and len(after.oracle_nodes) > 2:
        forbidden.append("more_than_two_oracle_statements")
    if action != "protocol_repair" and not report.get("target_api_preserved"):
        forbidden.append("target_api_missing")
    report["forbidden_patterns"] = forbidden
    report["valid"] = bool(report.get("valid")) and not forbidden
    return report


def _tri(value: Any) -> int:
    return {"true": 2, "unknown": 1, "false": 0}.get(str(value or "unknown").lower(), 1)


def _risk(value: Any) -> int:
    return {"LOW": 2, "MEDIUM": 1, "HIGH": 0}.get(str(value or "MEDIUM").upper(), 1)


def _checkpoint_rank(
    checkpoint: CandidateCheckpoint,
    entry: CandidateArchiveEntry | None,
    first_scaffold_hash: str,
) -> tuple[list[int], dict[str, Any]]:
    execution = checkpoint.execution or {}
    verifier = checkpoint.verifier or {}
    surrogate = checkpoint.surrogate or {}
    status = _status(execution)
    executable = status not in ERROR_STATUSES
    issue_fail = str(verifier.get("decision") or "").lower() == "accept" or status == "ISSUE_ALIGNED_FAIL"
    target = str(
        execution.get("runtime_target_hit")
        or (entry.target_evidence.get("target_hit") if entry else "unknown")
        or "unknown"
    )
    semantic_target = str(execution.get("semantic_target_hit") or "unknown")
    valid_surrogate = _surrogate_supported(surrogate)
    risk_level = str((checkpoint.oracle_risk or {}).get("level") or "MEDIUM").upper()
    code = ""
    try:
        code = Path(checkpoint.code_path).read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        tree = ast.parse(code)
        assertion_count = sum(isinstance(node, ast.Assert) for node in ast.walk(tree))
        assertion_count += sum(
            isinstance(node, ast.Call) and "assert" in ast.unparse(node.func).lower()
            for node in ast.walk(tree)
        )
        private_assertion = any(
            isinstance(node, ast.Attribute)
            and node.attr.startswith("_")
            and any(isinstance(parent, ast.Assert) for parent in ast.walk(tree))
            for node in ast.walk(tree)
        )
    except (SyntaxError, ValueError):
        assertion_count = 99
        private_assertion = True
    public_assertion = assertion_count > 0 and not private_assertion
    scaffold_hash = (entry.segment_hashes.get("scaffold") if entry else "") or ""
    scaffold_fidelity = bool(first_scaffold_hash and scaffold_hash == first_scaffold_hash)
    novelty = float(entry.novelty if entry else 0.0)
    duplicate_free = bool(entry is None or entry.duplicate_status == "UNIQUE")
    key = [
        1 if checkpoint.evidence_rank.get("protocol_valid", True) else 0,
        1 if executable else 0,
        1 if issue_fail else 0,
        _tri(target),
        _tri(semantic_target),
        1 if valid_surrogate else 0,
        _risk(risk_level),
        1 if public_assertion else 0,
        1 if 1 <= assertion_count <= 2 else 0,
        1 if scaffold_fidelity else 0,
        1 if novelty > 0 else 0,
        1 if duplicate_free else 0,
        1 if checkpoint.legacy_selected else 0,
        -int(checkpoint.round_id),
    ]
    evidence = {
        "protocol_valid": bool(checkpoint.evidence_rank.get("protocol_valid", True)),
        "buggy_executable": executable,
        "buggy_issue_aligned_fail": issue_fail,
        "runtime_target_hit": target,
        "semantic_target_hit": semantic_target,
        "surrogate_supported": valid_surrogate,
        "oracle_risk": risk_level,
        "public_behavior_assertion": public_assertion,
        "assertion_count": assertion_count,
        "scaffold_fidelity": scaffold_fidelity,
        "novelty": novelty,
        "duplicate_free": duplicate_free,
        "legacy_tie_preference": checkpoint.legacy_selected,
    }
    return key, evidence


def select_checkpoint_v2(
    checkpoints: list[CandidateCheckpoint],
    archive: CandidateArchive,
) -> tuple[int, dict[str, Any]]:
    if not checkpoints:
        return -1, {"selector_version": "selector_v2", "rankings": []}
    entries_by_id = {item.candidate_id: item for item in archive.entries}
    unique = archive.unique_entries()
    first_scaffold = unique[0].segment_hashes.get("scaffold", "") if unique else ""
    rankings: list[dict[str, Any]] = []
    best_index = 0
    best_key: list[int] | None = None
    seen_ast: set[str] = set()
    for index, checkpoint in enumerate(checkpoints):
        entry = entries_by_id.get(checkpoint.candidate_id)
        if entry and entry.normalized_ast_hash in seen_ast:
            continue
        if entry and entry.normalized_ast_hash:
            seen_ast.add(entry.normalized_ast_hash)
        key, evidence = _checkpoint_rank(checkpoint, entry, first_scaffold)
        checkpoint.selector_v2_rank = key
        checkpoint.selector_v2_reason = "; ".join(
            name for name, value in evidence.items() if value not in {False, 0, "", "unknown", "UNKNOWN"}
        )
        rankings.append(
            {
                "candidate_id": checkpoint.candidate_id,
                "round_id": checkpoint.round_id,
                "origin": entry.origin if entry else "UNKNOWN",
                "rank_key": key,
                "evidence": evidence,
                "legacy_score": checkpoint.legacy_score,
            }
        )
        if best_key is None or key > best_key:
            best_key = key
            best_index = index
    checkpoints[best_index].selector_v2_selected = True
    return best_index, {
        "selector_version": "selector_v2",
        "uses_counterfactual_fields": False,
        "selected_candidate_id": checkpoints[best_index].candidate_id,
        "selected_round_id": checkpoints[best_index].round_id,
        "rankings": sorted(rankings, key=lambda item: item["rank_key"], reverse=True),
    }


def _first_test_function(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            return node
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                    return child
    return None


def _statements_for_role(code: str, behavior: BehaviorTarget, role: str) -> list[ast.stmt]:
    tree = ast.parse(code)
    function = _first_test_function(tree)
    if function is None:
        return []
    segments = segment_test(code, behavior)
    records = getattr(segments, f"{role}_nodes")
    lines = {
        int(item.get("lineno") or 0)
        for item in records
        if item.get("role") in {role, "setup"}
    }
    if role == "scaffold":
        lines = {int(item.get("lineno") or 0) for item in records if item.get("role") == "setup"}
    return [copy.deepcopy(item) for item in function.body if int(getattr(item, "lineno", 0) or 0) in lines]


def _defined_names(nodes: list[ast.stmt]) -> set[str]:
    names: set[str] = set(dir(builtins))
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Param)):
                names.add(child.id)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(child.name)
    return names


def _module_defined_names(code: str) -> set[str]:
    names: set[str] = set()
    tree = ast.parse(code)
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("test"):
                names.add(node.name)
    return names


def recompose_candidate(
    behavior: BehaviorTarget,
    candidates: list[CandidateTest],
    entries: list[CandidateArchiveEntry],
    round_id: int,
) -> tuple[CandidateTest | None, dict[str, Any]]:
    """Combine only high-confidence, AST-compatible archived segments."""

    by_id = {
        str((candidate.lineage or {}).get("candidate_id") or ""): candidate
        for candidate in candidates
    }
    usable = [entry for entry in entries if entry.candidate_id in by_id and entry.executed]
    if len(usable) < 2:
        return None, {"status": "ABSTAIN", "reason": "fewer than two executed unique candidates"}
    scaffold_entry = next(
        (item for item in usable if not _status(item.buggy_execution) in ERROR_STATUSES),
        usable[0],
    )
    trigger_entry = next(
        (
            item
            for item in usable
            if str(item.verifier_decision.get("decision") or "").lower() == "accept"
            or str(item.target_evidence.get("target_hit") or "").lower() == "true"
        ),
        None,
    )
    oracle_entry = min(
        usable,
        key=lambda item: {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(
            str(item.oracle_risk.get("level") or "MEDIUM").upper(), 1
        ),
    )
    if trigger_entry is None:
        return None, {"status": "ABSTAIN", "reason": "no archived issue-aligned trigger"}
    source_candidates = [by_id[item.candidate_id] for item in (scaffold_entry, trigger_entry, oracle_entry)]
    segment_sets = [segment_test(item.code, behavior) for item in source_candidates]
    if any(
        item.test_entry_count != 1
        or item.segment_confidence.get("scaffold", 0.0) < 0.8
        or item.segment_confidence.get("trigger", 0.0) < 0.8
        or item.segment_confidence.get("oracle", 0.0) < 0.8
        for item in segment_sets
    ):
        return None, {"status": "ABSTAIN", "reason": "segment confidence below recomposition threshold"}
    try:
        scaffold_tree = ast.parse(source_candidates[0].code)
        test_function = _first_test_function(scaffold_tree)
        if test_function is None:
            raise ValueError("missing test entry")
        setup_nodes = _statements_for_role(source_candidates[0].code, behavior, "scaffold")
        trigger_nodes = _statements_for_role(source_candidates[1].code, behavior, "trigger")
        oracle_nodes = _statements_for_role(source_candidates[2].code, behavior, "oracle")
        if not trigger_nodes or not oracle_nodes:
            raise ValueError("missing trigger or oracle segment")
        defined = _defined_names(setup_nodes + trigger_nodes)
        defined.update(_module_defined_names(source_candidates[0].code))
        oracle_loads = {
            child.id
            for node in oracle_nodes
            for child in ast.walk(node)
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
        }
        unknown = sorted(oracle_loads - defined)
        if unknown:
            return None, {"status": "ABSTAIN", "reason": "oracle depends on incompatible names", "unknown_names": unknown}
        test_function.body = setup_nodes + trigger_nodes + oracle_nodes
        ast.fix_missing_locations(scaffold_tree)
        code = ast.unparse(scaffold_tree).strip() + "\n"
        compile(code, "<ats-brt-recomposition>", "exec")
    except (SyntaxError, ValueError) as exc:
        return None, {"status": "ABSTAIN", "reason": str(exc)}
    base = source_candidates[0]
    candidate = CandidateTest(
        instance_id=base.instance_id,
        round_id=round_id,
        code=code,
        candidate_file_path=base.candidate_file_path,
        candidate_repo_path=base.candidate_repo_path,
        pytest_nodeid=base.pytest_nodeid,
        command=base.command,
        lineage={
            "origin": "recomposition",
            "parent_candidate_id": scaffold_entry.candidate_id,
            "seed_id": scaffold_entry.seed_id,
            "round": round_id,
            "search_action": "compatible_best_segments",
            "component_candidate_ids": {
                "scaffold": scaffold_entry.candidate_id,
                "trigger": trigger_entry.candidate_id,
                "oracle": oracle_entry.candidate_id,
            },
        },
    )
    return candidate, {
        "status": "CREATED",
        "component_candidate_ids": candidate.lineage["component_candidate_ids"],
    }
