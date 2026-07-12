"""Behavior-constrained counterfactual validation helpers."""

from __future__ import annotations

import ast
import difflib
import hashlib
import io
import json
import re
import tokenize
from pathlib import Path
from typing import Any

from ..core.schema import (
    BehaviorTarget,
    CandidateTest,
    CounterfactualEvidence,
    CounterfactualPlan,
    DualVersionResult,
    ExecutionResult,
    FailureSignature,
    NegativeControlMetadata,
    ProtocolRecovery,
    TargetReachability,
)
from ..core.utils import ensure_dir, safe_json_dump, write_text
from ..retrieval.icore_runtime import first_test_selector, icore_test_command


NON_EXECUTABLE_STATUSES = {
    "SETUP_ERROR",
    "COLLECT_ERROR",
    "SYNTAX_ERROR",
    "TIMEOUT",
}


def _factor_id(item: dict[str, Any]) -> str:
    return str(item.get("factor_id") or "").strip()


def _target_names(behavior: BehaviorTarget) -> list[str]:
    names: list[str] = []
    for api in behavior.target_apis:
        name = str(api.get("name") or "").strip()
        if name:
            names.append(name.rsplit(".", 1)[-1])
    for target in behavior.trace_targets:
        for key in ("function_name", "class_name"):
            name = str(target.get(key) or "").strip()
            if name:
                names.append(name.rsplit(".", 1)[-1])
    return [name for name in dict.fromkeys(names) if len(name) >= 3]


def build_counterfactual_plan(
    behavior: BehaviorTarget,
    candidate: CandidateTest,
    output_dir: str,
    max_ast_edits: int = 1,
) -> CounterfactualPlan:
    """Select one issue-specific trigger factor for a minimal ablation."""

    factors = [
        factor
        for factor in behavior.essential_trigger_factors
        if isinstance(factor, dict) and _factor_id(factor)
    ]
    rules = [
        rule
        for rule in behavior.trigger_ablation_rules
        if isinstance(rule, dict) and _factor_id(rule)
    ]
    factor_ids = [_factor_id(factor) for factor in factors]
    selected_rule: dict[str, Any] | None = None
    selected_factor: dict[str, Any] | None = None
    fallback_rule: dict[str, Any] | None = None
    fallback_factor: dict[str, Any] | None = None
    for rule in rules:
        if factor_ids and _factor_id(rule) not in factor_ids:
            continue
        positive = str(rule.get("positive_form") or "").strip()
        negative = str(rule.get("negative_control_form") or "").strip()
        if positive and negative and fallback_rule is None:
            fallback_rule = rule
            fallback_factor = next(
                (factor for factor in factors if _factor_id(factor) == _factor_id(rule)),
                None,
            )
        if positive and negative and positive in candidate.code:
            selected_rule = rule
            selected_factor = next(
                (factor for factor in factors if _factor_id(factor) == _factor_id(rule)),
                None,
            )
            break
    if selected_rule is None:
        for factor in factors:
            positive = str(factor.get("positive_form") or "").strip()
            negative = str(factor.get("negative_control_form") or "").strip()
            if positive and negative and fallback_rule is None:
                fallback_factor = factor
                fallback_rule = {
                    "factor_id": _factor_id(factor),
                    "operation": "OTHER",
                    "positive_form": positive,
                    "negative_control_form": negative,
                    "max_ast_edits": max_ast_edits,
                    "preserve_setup": True,
                    "preserve_oracle": True,
                    "preserve_target_api": True,
                }
            if positive and negative and positive in candidate.code:
                selected_factor = factor
                selected_rule = {
                    "factor_id": _factor_id(factor),
                    "operation": "OTHER",
                    "positive_form": positive,
                    "negative_control_form": negative,
                    "max_ast_edits": max_ast_edits,
                    "preserve_setup": True,
                    "preserve_oracle": True,
                    "preserve_target_api": True,
                }
                break
    if selected_rule is None and fallback_rule is not None:
        selected_rule = fallback_rule
        selected_factor = fallback_factor
    if selected_rule is None:
        plan = CounterfactualPlan(
            instance_id=behavior.instance_id,
            positive_trigger_factors=factor_ids,
            max_ast_edits=max_ast_edits,
            abstain=True,
            abstain_reason=(
                "No essential trigger factor with positive and negative forms "
                "was available for a one-slice ablation."
            ),
        )
    else:
        selected_id = _factor_id(selected_rule)
        plan = CounterfactualPlan(
            instance_id=behavior.instance_id,
            positive_trigger_factors=factor_ids,
            selected_ablation_factor=selected_id,
            negative_control_goal=str(
                (selected_factor or {}).get("description")
                or "Remove or reverse one issue-specific trigger factor."
            ),
            negative_control_operation=str(
                selected_rule.get("operation") or "OTHER"
            ),
            expected_buggy_effect=str(
                selected_rule.get("expected_buggy_effect") or "UNKNOWN"
            ),
            preserve_target_api=bool(selected_rule.get("preserve_target_api", True)),
            max_ast_edits=int(selected_rule.get("max_ast_edits") or max_ast_edits),
        )
    plan.save_json(str(Path(output_dir) / "counterfactual_plan.json"))
    return plan


def _node_signature(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        return f"Constant:{type(node.value).__name__}:{repr(node.value)[:80]}"
    if isinstance(node, ast.Name):
        return f"Name:{node.id}"
    if isinstance(node, ast.Attribute):
        return f"Attribute:{node.attr}"
    if isinstance(node, ast.Call):
        return "Call:" + ast.dump(node.func, include_attributes=False)
    if isinstance(node, ast.Compare):
        return "Compare:" + ",".join(type(op).__name__ for op in node.ops)
    return type(node).__name__


def _changed_ast_nodes(positive_code: str, negative_code: str) -> list[str]:
    try:
        positive_tree = ast.parse(positive_code)
        negative_tree = ast.parse(negative_code)
    except SyntaxError:
        return ["SyntaxError"]
    positive_nodes = [_node_signature(node) for node in ast.walk(positive_tree)]
    negative_nodes = [_node_signature(node) for node in ast.walk(negative_tree)]
    changed: list[str] = []
    matcher = difflib.SequenceMatcher(a=positive_nodes, b=negative_nodes)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed.extend(positive_nodes[i1:i2])
        changed.extend(negative_nodes[j1:j2])
    return list(dict.fromkeys(changed))[:40]


def _import_fingerprint(tree: ast.AST) -> list[str]:
    return [
        ast.dump(node, include_attributes=False)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]


def _decorator_fingerprint(tree: ast.AST) -> list[str]:
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            values.append(
                node.name
                + ":"
                + json.dumps(
                    [ast.dump(item, include_attributes=False) for item in node.decorator_list],
                    ensure_ascii=False,
                )
            )
    return values


def _signature_fingerprint(tree: ast.AST) -> list[str]:
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            values.append(
                f"{node.name}:{[arg.arg for arg in node.args.args]}:"
                f"{[arg.arg for arg in node.args.kwonlyargs]}"
            )
        elif isinstance(node, ast.ClassDef):
            values.append(f"class:{node.name}")
    return values


def _assert_fingerprint(tree: ast.AST) -> list[str]:
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            values.append(ast.dump(node.test, include_attributes=False))
        elif isinstance(node, ast.Call):
            func = ast.dump(node.func, include_attributes=False)
            if "assert" in func.lower() or "raises" in func.lower():
                values.append(ast.dump(node, include_attributes=False))
    return values


def _test_entry_fingerprint(tree: ast.AST) -> list[str]:
    entries: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            entries.append(
                f"{type(node).__name__}:{node.name}:"
                f"{[arg.arg for arg in node.args.args]}:"
                f"{[arg.arg for arg in node.args.kwonlyargs]}"
            )
    return entries


def _has_banned_constructs(tree: ast.AST, target_names: list[str]) -> list[str]:
    reasons: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            reasons.append("negative control added or kept an explicit return")
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant) and node.test.value is True:
            reasons.append("negative control contains assert True")
        if isinstance(node, ast.Call):
            text = ast.dump(node, include_attributes=False)
            lowered = text.lower()
            if "skip" in lowered or "xfail" in lowered:
                reasons.append("negative control contains skip/xfail")
            if "mock" in lowered or "patch" in lowered or "monkeypatch" in lowered:
                if any(name in text for name in target_names) or not target_names:
                    reasons.append("negative control mocks or patches target behavior")
            if "raises" in lowered and not _call_existed_in_assert_context(node, tree):
                reasons.append("negative control may swallow or reframe exceptions")
    return list(dict.fromkeys(reasons))


def _call_existed_in_assert_context(call: ast.Call, tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assert, ast.With)):
            for child in ast.walk(node):
                if child is call:
                    return True
    return False


def _count_target_occurrences(code: str, target_names: list[str]) -> int:
    if not target_names:
        return 0
    total = 0
    for name in target_names:
        total += len(re.findall(rf"\b{re.escape(name)}\b", code))
    return total


def _short_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def negative_control_cache_key(
    behavior: BehaviorTarget,
    plan: CounterfactualPlan,
    candidate: CandidateTest,
) -> str:
    return _short_hash(
        {
            "candidate_code_hash": _short_hash(candidate.code),
            "behavior_target_hash": _short_hash(behavior.to_dict()),
            "selected_factor": plan.selected_ablation_factor,
            "seed_id": candidate.pytest_nodeid or candidate.candidate_repo_path,
            "round_id": candidate.round_id,
        }
    )


def _token_aware_replace(code: str, positive: str, negative: str) -> tuple[str, str]:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except tokenize.TokenError:
        return code, ""
    matches = [
        index
        for index, token in enumerate(tokens)
        if token.type
        in {
            tokenize.NAME,
            tokenize.NUMBER,
            tokenize.STRING,
            tokenize.OP,
        }
        and token.string == positive
    ]
    if len(matches) != 1:
        return code, ""
    index = matches[0]
    old = tokens[index]
    tokens[index] = tokenize.TokenInfo(
        old.type,
        negative,
        old.start,
        old.end,
        old.line,
    )
    try:
        return tokenize.untokenize(tokens), "TOKEN_AWARE_REPLACE"
    except (ValueError, tokenize.TokenError):
        return code, ""


def validate_negative_control_structure(
    behavior: BehaviorTarget,
    positive_code: str,
    negative_code: str,
    plan: CounterfactualPlan,
) -> NegativeControlMetadata:
    selected_factor_id = plan.selected_ablation_factor
    changed_nodes = _changed_ast_nodes(positive_code, negative_code)
    semantic_edit_count = 0 if not changed_nodes else max(1, (len(changed_nodes) + 1) // 2)
    metadata = NegativeControlMetadata(
        instance_id=behavior.instance_id,
        selected_factor_id=selected_factor_id,
        changed_ast_nodes=changed_nodes,
        ast_edit_count=semantic_edit_count,
        max_ast_edits=plan.max_ast_edits,
    )
    if not negative_code.strip():
        metadata.status = "INVALID"
        metadata.validation_reasons.append("negative control code is empty")
        return metadata
    try:
        positive_tree = ast.parse(positive_code)
        negative_tree = ast.parse(negative_code)
    except SyntaxError as exc:
        metadata.status = "INVALID"
        metadata.validation_reasons.append(f"negative control syntax error: {exc}")
        return metadata
    target_names = _target_names(behavior)
    metadata.test_entry_preserved = (
        _test_entry_fingerprint(positive_tree) == _test_entry_fingerprint(negative_tree)
    )
    metadata.target_api_preserved = (
        not target_names
        or _count_target_occurrences(negative_code, target_names)
        >= max(1, _count_target_occurrences(positive_code, target_names))
    )
    metadata.oracle_preserved = _assert_fingerprint(positive_tree) == _assert_fingerprint(negative_tree)
    metadata.setup_preserved = (
        _import_fingerprint(positive_tree) == _import_fingerprint(negative_tree)
        and _decorator_fingerprint(positive_tree) == _decorator_fingerprint(negative_tree)
        and _signature_fingerprint(positive_tree) == _signature_fingerprint(negative_tree)
    )
    metadata.frozen_region_changed = not (
        metadata.oracle_preserved and metadata.setup_preserved
    )
    if not metadata.target_api_preserved:
        metadata.validation_reasons.append("target API call/name was not preserved")
    if not metadata.oracle_preserved:
        metadata.validation_reasons.append("oracle/assertion structure changed")
    if not metadata.setup_preserved:
        metadata.validation_reasons.append("imports, decorators, class context, or test signature changed")
    if not metadata.test_entry_preserved:
        metadata.validation_reasons.append("test entry structure changed")
    if semantic_edit_count > max(1, plan.max_ast_edits):
        metadata.validation_reasons.append(
            f"AST edit count {semantic_edit_count} exceeds max_ast_edits={plan.max_ast_edits}"
        )
    metadata.validation_reasons.extend(_has_banned_constructs(negative_tree, target_names))
    metadata.status = "VALID" if not metadata.validation_reasons else "INVALID"
    return metadata


def generate_negative_control(
    behavior: BehaviorTarget,
    plan: CounterfactualPlan,
    candidate: CandidateTest,
    output_dir: str,
    repo: str,
    version: str,
) -> tuple[CandidateTest | None, NegativeControlMetadata]:
    """Create a one-slice negative-control test without modifying final_test.py."""

    output_path = Path(output_dir)
    ensure_dir(output_path)
    cache_key = negative_control_cache_key(behavior, plan, candidate)
    if plan.abstain:
        metadata = NegativeControlMetadata(
            instance_id=behavior.instance_id,
            status="ABSTAIN",
            selected_factor_id=plan.selected_ablation_factor,
            validation_reasons=[plan.abstain_reason],
            max_ast_edits=plan.max_ast_edits,
            cache_key=cache_key,
            generation_method="ABSTAIN",
        )
        metadata.save_json(str(output_path / "negative_control_metadata.json"))
        return None, metadata
    rule = next(
        (
            item
            for item in behavior.trigger_ablation_rules
            if isinstance(item, dict) and _factor_id(item) == plan.selected_ablation_factor
        ),
        {},
    )
    if not rule:
        rule = next(
            (
                item
                for item in behavior.essential_trigger_factors
                if isinstance(item, dict) and _factor_id(item) == plan.selected_ablation_factor
            ),
            {},
        )
    positive = str(rule.get("positive_form") or "").strip()
    negative = str(rule.get("negative_control_form") or "").strip()
    occurrences = candidate.code.count(positive) if positive else 0
    generation_method = "EXACT_TEXT_REPLACE"
    if positive and negative and occurrences == 0:
        token_code, token_method = _token_aware_replace(candidate.code, positive, negative)
        if token_method:
            negative_code = token_code
            generation_method = token_method
            occurrences = 1
        else:
            negative_code = ""
    else:
        negative_code = candidate.code.replace(positive, negative, 1) if occurrences == 1 else ""
    if not positive or not negative or occurrences != 1 or not negative_code:
        reason = (
            "ablation forms are missing"
            if not positive or not negative
            else (
                "positive_form did not have a safe exact or token-aware match; "
                f"found {occurrences}; LLM fallback is unavailable in static/deterministic mode"
            )
        )
        metadata = NegativeControlMetadata(
            instance_id=behavior.instance_id,
            status="ABSTAIN",
            selected_factor_id=plan.selected_ablation_factor,
            validation_reasons=[reason],
            max_ast_edits=plan.max_ast_edits,
            cache_key=cache_key,
            generation_method="ABSTAIN_LLM_FALLBACK_UNAVAILABLE",
        )
        metadata.save_json(str(output_path / "negative_control_metadata.json"))
        return None, metadata
    write_text(str(output_path / "negative_control_test.py"), negative_code)
    metadata = validate_negative_control_structure(
        behavior, candidate.code, negative_code, plan
    )
    metadata.cache_key = cache_key
    metadata.generation_method = generation_method
    metadata.save_json(str(output_path / "negative_control_metadata.json"))
    if metadata.status != "VALID":
        return None, metadata
    candidate_path = Path(candidate.candidate_file_path)
    repo_path = Path(candidate.candidate_repo_path)
    negative_repo_path = repo_path.with_name(repo_path.stem + "_negative_control.py")
    negative_full_path = candidate_path.with_name(candidate_path.stem + "_negative_control.py")
    write_text(str(negative_full_path), negative_code)
    negative_candidate = CandidateTest(
        instance_id=candidate.instance_id,
        round_id=candidate.round_id,
        code=negative_code,
        candidate_file_path=str(negative_full_path),
        candidate_repo_path=str(negative_repo_path),
        pytest_nodeid=str(negative_repo_path),
        command=icore_test_command(repo, version, str(negative_repo_path), first_test_selector(negative_code)),
        status="NEGATIVE_CONTROL",
        notes=(
            "Internal negative control generated by one trigger-factor ablation; "
            "not eligible for final_test.py."
        ),
    )
    return negative_candidate, metadata


def _traceback_frames(text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    pattern = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([A-Za-z_][A-Za-z0-9_]*))?')
    for match in pattern.finditer(text):
        frames.append(
            {
                "file": match.group(1),
                "line": int(match.group(2)),
                "function": match.group(3) or "",
            }
        )
    return frames[:80]


def collect_target_reachability(
    behavior: BehaviorTarget,
    candidate: CandidateTest,
    execution: ExecutionResult,
) -> TargetReachability:
    text = execution.stdout + "\n" + execution.stderr
    frames = _traceback_frames(text)
    names = _target_names(behavior)
    target_files = [
        str(item.get("source_file") or item.get("source_path") or "").replace("\\", "/")
        for item in behavior.trace_targets + behavior.target_apis
        if isinstance(item, dict)
    ]
    hit_functions: list[str] = []
    hit_files: list[str] = []
    dynamic_hit = False
    for frame in frames:
        function = str(frame.get("function") or "")
        file_name = str(frame.get("file") or "").replace("\\", "/")
        if any(name and name == function for name in names):
            hit_functions.append(function)
            dynamic_hit = True
        if any(path and path in file_name for path in target_files):
            hit_files.append(file_name)
            dynamic_hit = True
    static_calls = [
        name
        for name in names
        if re.search(rf"\b{re.escape(name)}\b", candidate.code)
    ]
    sources: list[str] = []
    confidence = 0.0
    target_hit = "unknown"
    if dynamic_hit:
        target_hit = "true"
        sources.append("traceback")
        confidence = 0.75
    elif static_calls:
        target_hit = "unknown"
        sources.append("static_call")
        confidence = 0.25
    elif frames and names:
        target_hit = "unknown"
        sources.append("traceback")
        confidence = 0.2
    reachability = TargetReachability(
        instance_id=behavior.instance_id,
        target_hit=target_hit,
        hit_functions=list(dict.fromkeys(hit_functions + static_calls)),
        hit_files=list(dict.fromkeys(hit_files)),
        traceback_frames=frames,
        target_call_count=len(hit_functions) or len(static_calls),
        reachability_source=list(dict.fromkeys(sources)),
        confidence=confidence,
        evidence_complete=bool(frames),
    )
    return reachability


def normalize_failure_signature(
    execution: ExecutionResult | None,
    runtime_target_hit: str = "unknown",
    semantic_target_hit: str = "unknown",
) -> FailureSignature:
    if execution is None:
        return FailureSignature(
            outcome="UNKNOWN",
            runtime_target_hit=runtime_target_hit,
            semantic_target_hit=semantic_target_hit,
        )
    signature = execution.normalized_failure_signature
    if not signature:
        parts = [
            execution.status,
            execution.exception_type,
            execution.exception_message_normalized,
            execution.top_project_frame or execution.failure_location,
        ]
        signature = "|".join(part for part in parts if part)
    signature = re.sub(r"/tmp/[^\s|:]+", "<tmp>", signature)
    signature = re.sub(r"brt3_surrogate_[A-Za-z0-9_/-]+", "brt_surrogate", signature)
    signature = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", signature)
    signature = re.sub(r"line \d+", "line N", signature)
    return FailureSignature(
        instance_id=execution.instance_id,
        outcome=execution.outcome or execution.status,
        exception_type=execution.exception_type,
        exception_message_normalized=execution.exception_message_normalized,
        failure_location=execution.failure_location,
        top_project_frame=execution.top_project_frame,
        normalized_failure_signature=signature[:1000],
        runtime_target_hit=runtime_target_hit,
        semantic_target_hit=semantic_target_hit,
    )


def execution_payload(
    execution: ExecutionResult | None,
    runtime_target_hit: str = "unknown",
    semantic_target_hit: str = "unknown",
) -> dict[str, Any]:
    if execution is None:
        return {
            "outcome": "UNKNOWN",
            "runtime_target_hit": runtime_target_hit,
            "semantic_target_hit": semantic_target_hit,
        }
    data = execution.to_dict()
    signature = normalize_failure_signature(
        execution, runtime_target_hit, semantic_target_hit
    )
    data.update(signature.to_dict())
    data["return_code"] = execution.return_code if execution.return_code else execution.returncode
    data["runtime_target_hit"] = runtime_target_hit
    data["semantic_target_hit"] = semantic_target_hit
    return data


def compare_failure_signatures(
    positive: dict[str, Any],
    negative: dict[str, Any],
) -> str:
    pos = str(positive.get("normalized_failure_signature") or "")
    neg = str(negative.get("normalized_failure_signature") or "")
    if not pos or not neg:
        return "UNKNOWN"
    if pos == neg:
        return "SAME"
    if positive.get("exception_type") and positive.get("exception_type") == negative.get("exception_type"):
        if positive.get("top_project_frame") == negative.get("top_project_frame"):
            return "RELATED_BUT_DIFFERENT"
        pos_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", pos.lower()))
        neg_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", neg.lower()))
        if len(pos_tokens & neg_tokens) >= 3:
            return "RELATED_BUT_DIFFERENT"
    return "DIFFERENT"


def _is_executable_failure(execution: ExecutionResult | None) -> bool:
    return bool(
        execution
        and execution.returncode != 0
        and execution.status not in NON_EXECUTABLE_STATUSES
    )


def _is_non_executable_status(status: str) -> bool:
    return str(status or "") in NON_EXECUTABLE_STATUSES


def _usable_surrogate_execution(execution: dict[str, Any]) -> bool:
    status = str(execution.get("status") or "")
    return bool(execution) and status not in NON_EXECUTABLE_STATUSES


def _execution_dict_pass(execution: dict[str, Any]) -> bool:
    return int(execution.get("returncode") or execution.get("return_code") or 0) == 0


def _execution_dict_signature(execution: dict[str, Any]) -> dict[str, Any]:
    return {
        "normalized_failure_signature": str(
            execution.get("normalized_failure_signature") or ""
        ),
        "exception_type": str(execution.get("exception_type") or ""),
        "top_project_frame": str(execution.get("top_project_frame") or ""),
    }


def _surrogate_runs(dual: DualVersionResult | None) -> list[dict[str, Any]]:
    if dual is None:
        return []
    runs: list[dict[str, Any]] = []
    for attempt in dual.attempts:
        execution = attempt.get("execution") if isinstance(attempt, dict) else {}
        negative_execution = (
            attempt.get("negative_execution") if isinstance(attempt, dict) else {}
        )
        patch_valid = (
            isinstance(execution, dict)
            and _usable_surrogate_execution(execution)
            and str(attempt.get("status") or "") == "APPLIED"
        )
        runs.append(
            {
                "patch_id": str(attempt.get("round_id") if isinstance(attempt, dict) else ""),
                "patch_valid": patch_valid,
                "positive_result": execution if isinstance(execution, dict) else {},
                "negative_result": negative_execution if isinstance(negative_execution, dict) else {},
                "negative_skip_reason": str(attempt.get("negative_skip_reason") or ""),
            }
        )
    return runs


def build_counterfactual_evidence(
    behavior: BehaviorTarget,
    positive_execution: ExecutionResult,
    negative_execution: ExecutionResult | None,
    negative_metadata: NegativeControlMetadata | None,
    positive_reachability: TargetReachability | None,
    semantic_target_hit: str,
    dual: DualVersionResult | None,
    min_valid_surrogate_patches: int = 2,
    surrogate_consensus_threshold: float = 0.67,
    positive_issue_aligned: bool = False,
    negative_reachability: TargetReachability | None = None,
) -> CounterfactualEvidence:
    runtime_hit = positive_reachability.target_hit if positive_reachability else "unknown"
    negative_runtime_hit = (
        negative_reachability.target_hit if negative_reachability else "unknown"
    )
    positive_payload = execution_payload(
        positive_execution, runtime_hit, semantic_target_hit
    )
    negative_payload = execution_payload(
        negative_execution, negative_runtime_hit, "unknown"
    )
    evidence = CounterfactualEvidence(
        instance_id=behavior.instance_id,
        positive_buggy=positive_payload,
        negative_buggy=negative_payload,
        surrogate_runs=_surrogate_runs(dual),
    )
    metadata_status = negative_metadata.status if negative_metadata else "ABSTAIN"
    negative_invalid = bool(
        negative_metadata
        and (
            negative_metadata.target_api_preserved is False
            or negative_metadata.oracle_preserved is False
            or negative_metadata.setup_preserved is False
            or negative_metadata.test_entry_preserved is False
        )
    )
    if (
        metadata_status != "VALID"
        or negative_execution is None
        or negative_invalid
        or _is_non_executable_status(positive_execution.status)
        or _is_non_executable_status(negative_execution.status)
    ):
        reason = f"negative control {metadata_status.lower()}"
        if negative_invalid:
            reason = "negative control failed structural preservation checks"
        elif negative_execution is not None and _is_non_executable_status(negative_execution.status):
            reason = f"negative control execution is {negative_execution.status}"
        elif _is_non_executable_status(positive_execution.status):
            reason = f"positive execution is {positive_execution.status}"
        evidence.trigger_necessity = {
            "status": "UNKNOWN",
            "score": 0.0,
            "reason": reason,
        }
    else:
        comparison = compare_failure_signatures(positive_payload, negative_payload)
        positive_issue_fail = (
            positive_issue_aligned
            and _is_executable_failure(positive_execution)
        )
        same_target_evidence = (
            positive_payload.get("runtime_target_hit")
            == negative_payload.get("runtime_target_hit")
        )
        complete_execution_evidence = bool(
            positive_reachability
            and negative_reachability
            and positive_reachability.evidence_complete
            and negative_reachability.evidence_complete
        )
        if positive_issue_fail and negative_execution.returncode == 0:
            evidence.trigger_necessity = {
                "status": "SUPPORTED",
                "score": 1.0,
                "reason": "positive fails on buggy while trigger-ablated negative control passes",
            }
        elif positive_issue_fail and comparison == "DIFFERENT":
            evidence.trigger_necessity = {
                "status": "SUPPORTED",
                "score": 0.8,
                "reason": "negative control no longer has the same normalized issue failure",
            }
        elif positive_issue_fail and comparison == "RELATED_BUT_DIFFERENT":
            evidence.trigger_necessity = {
                "status": "WEAK",
                "score": 0.45,
                "reason": "positive and negative fail, but failure evidence differs",
            }
        elif (
            positive_issue_fail
            and comparison == "SAME"
            and same_target_evidence
            and complete_execution_evidence
        ):
            evidence.trigger_necessity = {
                "status": "UNSUPPORTED",
                "score": 0.0,
                "reason": "positive and negative controls share the same normalized failure signature",
            }
        elif positive_issue_fail and comparison == "SAME":
            evidence.trigger_necessity = {
                "status": "UNKNOWN",
                "score": 0.0,
                "reason": "same failure signature but execution/target evidence is incomplete",
            }
        else:
            evidence.trigger_necessity = {
                "status": "UNKNOWN",
                "score": 0.0,
                "reason": "positive execution did not provide an executable issue failure",
            }
    valid_runs = [
        run
        for run in evidence.surrogate_runs
        if run.get("patch_valid")
    ]
    pass_runs = [
        run
        for run in valid_runs
        if _execution_dict_pass(run.get("positive_result") or {})
    ]
    supported_runs: list[dict[str, Any]] = []
    conflicting_runs: list[dict[str, Any]] = []
    for run in valid_runs:
        positive_result = run.get("positive_result") or {}
        negative_result = run.get("negative_result") or {}
        conflict = False
        if (
            positive_result
            and negative_result
            and not _execution_dict_pass(positive_result)
            and not _execution_dict_pass(negative_result)
        ):
            conflict = (
                compare_failure_signatures(
                    _execution_dict_signature(positive_result),
                    _execution_dict_signature(negative_result),
                )
                == "SAME"
            )
        if conflict:
            conflicting_runs.append(run)
        elif _execution_dict_pass(positive_result):
            supported_runs.append(run)
    valid_count = len(valid_runs)
    pass_count = len(pass_runs)
    supported_count = len(supported_runs)
    conflicting_count = len(conflicting_runs)
    invalid_count = len(evidence.surrogate_runs) - valid_count
    paired_support_score = supported_count / valid_count if valid_count else 0.0
    if valid_count < max(1, min_valid_surrogate_patches):
        evidence.repair_sufficiency = {
            "status": "UNKNOWN",
            "valid_patch_count": valid_count,
            "positive_pass_count": pass_count,
            "supported_patch_count": supported_count,
            "conflicting_patch_count": conflicting_count,
            "invalid_patch_count": invalid_count,
            "paired_support_score": paired_support_score,
            "score": 0.0,
            "reason": "not enough valid surrogate patches for consensus",
        }
    else:
        ratio = paired_support_score
        if ratio >= surrogate_consensus_threshold:
            status = "SUPPORTED"
            score = ratio
            reason = "majority of valid surrogate patches make the positive test pass"
        elif pass_count > 0:
            status = "WEAK"
            score = ratio
            reason = "a minority of valid surrogate patches make the positive test pass"
        else:
            status = "UNSUPPORTED"
            score = 0.0
            reason = "valid surrogate patches did not make the positive test pass"
        evidence.repair_sufficiency = {
            "status": status,
            "valid_patch_count": valid_count,
            "positive_pass_count": pass_count,
            "supported_patch_count": supported_count,
            "conflicting_patch_count": conflicting_count,
            "invalid_patch_count": invalid_count,
            "paired_support_score": paired_support_score,
            "score": score,
            "reason": reason,
        }
    trigger_status = str(evidence.trigger_necessity.get("status") or "UNKNOWN")
    repair_status = str(evidence.repair_sufficiency.get("status") or "UNKNOWN")
    if trigger_status == "SUPPORTED" and repair_status == "SUPPORTED":
        evidence.oracle_stability = {
            "status": "STABLE",
            "score": 1.0,
            "reason": "positive/negative contrast and surrogate repairs agree",
        }
        evidence.bidirectional_support = {
            "status": "STRONG",
            "reason": "test-side trigger ablation and program-side surrogate repair both support the candidate",
        }
    elif "SUPPORTED" in {trigger_status, repair_status} or "WEAK" in {trigger_status, repair_status}:
        evidence.oracle_stability = {
            "status": "UNKNOWN",
            "score": 0.4,
            "reason": "only partial counterfactual evidence is available",
        }
        evidence.bidirectional_support = {
            "status": "PARTIAL",
            "reason": "one side of the counterfactual evidence supports the candidate",
        }
    elif trigger_status == "UNSUPPORTED" and repair_status == "UNSUPPORTED":
        evidence.oracle_stability = {
            "status": "UNSTABLE",
            "score": 0.0,
            "reason": "negative control and surrogate repair evidence do not support the oracle",
        }
        evidence.bidirectional_support = {
            "status": "NONE",
            "reason": "neither test-side nor program-side counterfactual evidence supports the candidate",
        }
    else:
        evidence.bidirectional_support = {
            "status": "UNKNOWN",
            "reason": "counterfactual evidence is incomplete",
        }
    return evidence


def counterfactual_summary(
    enabled: bool,
    negative_metadata: NegativeControlMetadata | None,
    evidence: CounterfactualEvidence | None,
    selected_candidate_id: str = "",
    ranking_changed: bool = False,
    fallback_used: bool = False,
    shadow_mode: bool = True,
    legacy_selected_candidate_id: str = "",
    counterfactual_would_select_candidate_id: str = "",
) -> dict[str, Any]:
    repair = evidence.repair_sufficiency if evidence else {}
    return {
        "enabled": enabled,
        "shadow_mode": shadow_mode,
        "negative_control_generated": bool(negative_metadata and negative_metadata.status in {"VALID", "INVALID"}),
        "negative_control_valid": bool(negative_metadata and negative_metadata.status == "VALID"),
        "trigger_necessity": (
            (evidence.trigger_necessity or {}).get("status")
            if evidence
            else "UNKNOWN"
        ),
        "valid_surrogate_count": int(
            (repair or {}).get("valid_patch_count") or 0
        ),
        "supported_patch_count": int((repair or {}).get("supported_patch_count") or 0),
        "conflicting_patch_count": int((repair or {}).get("conflicting_patch_count") or 0),
        "invalid_patch_count": int((repair or {}).get("invalid_patch_count") or 0),
        "paired_support_score": float((repair or {}).get("paired_support_score") or 0.0),
        "repair_sufficiency": (
            (repair or {}).get("status")
            if evidence
            else "UNKNOWN"
        ),
        "bidirectional_support": (
            (evidence.bidirectional_support or {}).get("status")
            if evidence
            else "UNKNOWN"
        ),
        "ranking_changed": False if shadow_mode else ranking_changed,
        "ranking_changed_in_shadow": bool(shadow_mode and ranking_changed),
        "selected_candidate_id": selected_candidate_id,
        "legacy_selected_candidate_id": legacy_selected_candidate_id,
        "counterfactual_would_select_candidate_id": counterfactual_would_select_candidate_id,
        "fallback_used": fallback_used,
    }
