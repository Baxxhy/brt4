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
from ..core.prompts import NEGATIVE_CONTROL_SYSTEM_PROMPT, NEGATIVE_CONTROL_USER_PROMPT
from ..core.utils import clean_code_block, ensure_dir, safe_json_dump, truncate_text, write_text
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
            source_anchor=dict(
                (selected_rule.get("source_anchor") or (selected_factor or {}).get("source_anchor") or {})
            ),
            positive_ast_pattern=str(
                selected_rule.get("positive_ast_pattern")
                or (selected_factor or {}).get("positive_ast_pattern")
                or ""
            ),
            negative_ast_pattern=str(
                selected_rule.get("negative_ast_pattern")
                or (selected_factor or {}).get("negative_ast_pattern")
                or ""
            ),
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


def _normalized_ast_dump(code: str) -> str:
    try:
        return ast.dump(ast.parse(code), include_attributes=False)
    except SyntaxError:
        return ""


def _semantic_edit_type(operation: str, positive: str = "", negative: str = "") -> str:
    normalized = str(operation or "").upper()
    mapping = {
        "OPERATOR_FLIP": "OPERATOR_REPLACE",
        "ARG_VALUE_REPLACE": "ARGUMENT_REPLACE",
        "CALL_REMOVAL": "CALL_SEQUENCE_ABLATION",
        "BOUNDARY_NORMALIZE": "BOUNDARY_NORMALIZE",
        "STATE_RESET": "STATE_RESET",
        "CONFIG_RESET": "CONFIG_RESET",
    }
    if normalized in mapping:
        return mapping[normalized]
    if normalized in {
        "OPERATOR_REMOVE",
        "OPERATOR_REPLACE",
        "ARGUMENT_REPLACE",
        "ARGUMENT_REMOVE",
        "ARGUMENT_INSERT",
        "BOOLEAN_FLIP",
        "CALL_SEQUENCE_ABLATION",
        "LIFECYCLE_ABLATION",
        "INPUT_SHAPE_NORMALIZE",
        "OTHER_TRIGGER_ABLATION",
    }:
        return normalized
    if positive.startswith("~") and negative == positive[1:]:
        return "OPERATOR_REMOVE"
    if positive in {"True", "False"} and negative in {"True", "False"}:
        return "BOOLEAN_FLIP"
    return "OTHER_TRIGGER_ABLATION"


def _semantic_edits_from_transform(
    plan: CounterfactualPlan,
    positive: str,
    negative: str,
    changed_nodes: list[str],
    generation_method: str = "",
) -> list[dict[str, Any]]:
    if not changed_nodes:
        return []
    return [
        {
            "edit_type": _semantic_edit_type(
                plan.negative_control_operation, positive, negative
            ),
            "before": truncate_text(positive, 400),
            "after": truncate_text(negative, 400),
            "anchor": json.dumps(plan.source_anchor or {}, ensure_ascii=False),
            "count": 1,
            "generation_method": generation_method,
        }
    ]


def _body_without_assertions_fingerprint(tree: ast.AST) -> str:
    clone = ast.parse(ast.unparse(tree) if hasattr(ast, "unparse") else "")
    for node in ast.walk(clone):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            new_body: list[ast.stmt] = []
            for child in node.body:
                if isinstance(child, ast.Assert):
                    continue
                if isinstance(child, ast.Expr) and isinstance(child.value, ast.Call):
                    func_dump = ast.dump(child.value.func, include_attributes=False).lower()
                    if "assert" in func_dump or "raises" in func_dump:
                        continue
                new_body.append(child)
            node.body = new_body or [ast.Pass()]
    return ast.dump(clone, include_attributes=False)


def _oracle_preserved(positive_tree: ast.AST, negative_tree: ast.AST) -> bool:
    return _assert_fingerprint(positive_tree) == _assert_fingerprint(negative_tree)


def _protocol_preserved(positive_tree: ast.AST, negative_tree: ast.AST) -> bool:
    return (
        _import_fingerprint(positive_tree) == _import_fingerprint(negative_tree)
        and _decorator_fingerprint(positive_tree) == _decorator_fingerprint(negative_tree)
        and _signature_fingerprint(positive_tree) == _signature_fingerprint(negative_tree)
    )


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


def _regex_unique_replace(code: str, pattern: str, replacement: str) -> tuple[str, str]:
    matches = list(re.finditer(pattern, code))
    if len(matches) != 1:
        return code, ""
    match = matches[0]
    start, end = match.span()
    try:
        replacement_text = match.expand(replacement)
    except re.error:
        replacement_text = replacement
    return code[:start] + replacement_text + code[end:], "AST_ANCHORED_REGEX_REPLACE"


def _apply_ast_anchor_transform(
    code: str,
    plan: CounterfactualPlan,
    positive: str,
    negative: str,
) -> tuple[str, str]:
    anchor = plan.source_anchor or {}
    operation = _semantic_edit_type(plan.negative_control_operation, positive, negative)
    if positive and negative and positive in code and code.count(positive) == 1:
        return code.replace(positive, negative, 1), "AST_ANCHOR_EXACT_REPLACE"
    callee = str(anchor.get("callee") or "").strip()
    argument_name = str(anchor.get("argument_name") or "").strip()
    literal = str(anchor.get("literal_value") or "").strip()
    if operation == "OPERATOR_REMOVE" and positive.startswith("~"):
        return _regex_unique_replace(
            code,
            rf"~\s*({re.escape(positive[1:].strip())})",
            negative or positive[1:].strip(),
        )
    if operation == "BOOLEAN_FLIP" and argument_name:
        return _regex_unique_replace(
            code,
            rf"({re.escape(argument_name)}\s*=\s*)(True|False)",
            rf"\g<1>{'False' if literal == 'True' or positive.endswith('True') else 'True'}",
        )
    if operation in {"ARGUMENT_REPLACE", "CONFIG_RESET", "STATE_RESET", "BOUNDARY_NORMALIZE"} and argument_name and negative:
        negative_value = negative.split("=", 1)[-1].strip() if "=" in negative else negative
        return _regex_unique_replace(
            code,
            rf"({re.escape(argument_name)}\s*=\s*)[^,\)\n]+",
            rf"\g<1>{negative_value}",
        )
    if operation == "ARGUMENT_REMOVE" and argument_name:
        return _regex_unique_replace(
            code,
            rf",?\s*{re.escape(argument_name)}\s*=\s*[^,\)\n]+",
            "",
        )
    if operation in {"CALL_SEQUENCE_ABLATION", "LIFECYCLE_ABLATION"} and callee:
        return _regex_unique_replace(
            code,
            rf"(?m)^\s*.*\b{re.escape(callee)}\s*\([^\\n]*\)\s*$\n?",
            "",
        )
    return code, ""


def _apply_pattern_transform(
    code: str,
    plan: CounterfactualPlan,
    positive: str,
    negative: str,
) -> tuple[str, str]:
    positive_pattern = plan.positive_ast_pattern.strip()
    negative_pattern = plan.negative_ast_pattern.strip()
    if positive_pattern and negative_pattern:
        normalized = _normalized_ast_dump(positive_pattern)
        if normalized and _normalized_ast_dump(positive) == normalized:
            return _regex_unique_replace(code, re.escape(positive), negative)
    if positive and negative:
        escaped = re.escape(positive)
        return _regex_unique_replace(code, escaped, negative)
    return code, ""


def _deterministic_negative_transform(
    code: str,
    plan: CounterfactualPlan,
    positive: str,
    negative: str,
) -> tuple[str, str, int]:
    for transform in (
        lambda: _apply_ast_anchor_transform(code, plan, positive, negative),
        lambda: _apply_pattern_transform(code, plan, positive, negative),
    ):
        transformed, method = transform()
        if method and transformed != code:
            return transformed, method, 1
    if positive and negative:
        occurrences = code.count(positive)
        if occurrences == 1:
            return code.replace(positive, negative, 1), "EXACT_TEXT_REPLACE", occurrences
        token_code, token_method = _token_aware_replace(code, positive, negative)
        if token_method and token_code != code:
            return token_code, token_method, 1
        return "", "", occurrences
    return "", "", 0


def _llm_negative_control_fallback(
    behavior: BehaviorTarget,
    plan: CounterfactualPlan,
    candidate: CandidateTest,
    output_path: Path,
    llm_client: Any | None,
    protocol: ProtocolRecovery | None,
    execution_log: str,
    max_attempts: int,
) -> tuple[str, str]:
    if llm_client is None or max_attempts <= 0:
        return "", ""
    behavior_json = json.dumps(behavior.to_dict(), ensure_ascii=False)
    protocol_json = json.dumps(protocol.to_dict() if protocol else {}, ensure_ascii=False)
    plan_json = json.dumps(plan.to_dict(), ensure_ascii=False)
    prompt = NEGATIVE_CONTROL_USER_PROMPT.format(
        behavior_json=behavior_json,
        protocol_json=protocol_json,
        counterfactual_plan_json=plan_json,
        candidate_code=candidate.code,
    )
    prompt += (
        "\n\n当前 buggy execution / failure signature：\n"
        + truncate_text(execution_log, 12000)
        + "\n\n必须只输出完整 negative-control Python 文件。"
    )
    write_text(str(output_path / "negative_control_llm_prompt.txt"), NEGATIVE_CONTROL_SYSTEM_PROMPT + "\n\n" + prompt)
    for attempt in range(max_attempts):
        response = llm_client.chat(NEGATIVE_CONTROL_SYSTEM_PROMPT, prompt)
        response_path = output_path / f"negative_control_llm_response_{attempt}.txt"
        write_text(str(response_path), response)
        code = clean_code_block(response).strip()
        if code:
            return code + "\n", "LLM_FALLBACK"
    return "", ""


def validate_negative_control_structure(
    behavior: BehaviorTarget,
    positive_code: str,
    negative_code: str,
    plan: CounterfactualPlan,
) -> NegativeControlMetadata:
    selected_factor_id = plan.selected_ablation_factor
    changed_nodes = _changed_ast_nodes(positive_code, negative_code)
    raw_changed_node_count = len(changed_nodes)
    metadata = NegativeControlMetadata(
        instance_id=behavior.instance_id,
        selected_factor_id=selected_factor_id,
        changed_ast_nodes=changed_nodes,
        ast_edit_count=raw_changed_node_count,
        raw_changed_node_count=raw_changed_node_count,
        max_ast_edits=plan.max_ast_edits,
    )
    if not negative_code.strip():
        metadata.status = "INVALID"
        metadata.validation_reasons.append("negative control code is empty")
        return metadata
    if positive_code.strip() == negative_code.strip():
        metadata.status = "INVALID"
        metadata.validation_reasons.append("negative control did not change the trigger slice")
        return metadata
    try:
        positive_tree = ast.parse(positive_code)
        negative_tree = ast.parse(negative_code)
    except SyntaxError as exc:
        metadata.status = "INVALID"
        metadata.validation_reasons.append(f"negative control syntax error: {exc}")
        return metadata
    target_names = _target_names(behavior)
    positive_form = ""
    negative_form = ""
    for item in list(behavior.trigger_ablation_rules) + list(behavior.essential_trigger_factors):
        if isinstance(item, dict) and _factor_id(item) == selected_factor_id:
            positive_form = str(item.get("positive_form") or "").strip()
            negative_form = str(item.get("negative_control_form") or "").strip()
            break
    semantic_edits = _semantic_edits_from_transform(
        plan, positive_form, negative_form, changed_nodes
    )
    metadata.semantic_edits = semantic_edits
    metadata.semantic_edit_count = sum(int(item.get("count") or 0) for item in semantic_edits)
    metadata.test_entry_preserved = (
        _test_entry_fingerprint(positive_tree) == _test_entry_fingerprint(negative_tree)
    )
    metadata.target_api_preserved = (
        not target_names
        or _count_target_occurrences(negative_code, target_names)
        >= max(1, _count_target_occurrences(positive_code, target_names))
    )
    metadata.oracle_preserved = _oracle_preserved(positive_tree, negative_tree)
    metadata.imports_preserved = _import_fingerprint(positive_tree) == _import_fingerprint(negative_tree)
    metadata.decorators_preserved = _decorator_fingerprint(positive_tree) == _decorator_fingerprint(negative_tree)
    metadata.fixtures_preserved = True
    metadata.setup_preserved = _protocol_preserved(positive_tree, negative_tree)
    metadata.protocol_preserved = metadata.setup_preserved
    metadata.trigger_only_changed = (
        metadata.oracle_preserved
        and metadata.setup_preserved
        and metadata.test_entry_preserved
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
    if metadata.semantic_edit_count > max(1, plan.max_ast_edits):
        metadata.validation_reasons.append(
            f"semantic edit count {metadata.semantic_edit_count} exceeds max_ast_edits={plan.max_ast_edits}"
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
    *,
    llm_client: Any | None = None,
    protocol: ProtocolRecovery | None = None,
    execution_log: str = "",
    enable_llm_fallback: bool = True,
    max_llm_attempts: int = 1,
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
    negative_code, generation_method, occurrences = _deterministic_negative_transform(
        candidate.code, plan, positive, negative
    )
    used_llm = False
    if not positive or not negative or occurrences != 1 or not negative_code:
        negative_code, generation_method = _llm_negative_control_fallback(
            behavior,
            plan,
            candidate,
            output_path,
            llm_client if enable_llm_fallback else None,
            protocol,
            execution_log,
            max_llm_attempts,
        )
        used_llm = bool(negative_code)
        if not negative_code:
            reason = (
                "ablation forms are missing"
                if not positive or not negative
                else (
                    "positive_form did not have a safe AST anchor, pattern, token-aware, "
                    f"or exact match; found {occurrences}; LLM fallback failed or unavailable"
                )
            )
            metadata = NegativeControlMetadata(
                instance_id=behavior.instance_id,
                status="ABSTAIN",
                selected_factor_id=plan.selected_ablation_factor,
                validation_reasons=[reason],
                max_ast_edits=plan.max_ast_edits,
                cache_key=cache_key,
                generation_method="ABSTAIN_LLM_FALLBACK_FAILED",
                retry_count=max(0, max_llm_attempts if enable_llm_fallback else 0),
            )
            metadata.save_json(str(output_path / "negative_control_metadata.json"))
            return None, metadata
    write_text(str(output_path / "negative_control_test.py"), negative_code)
    metadata = validate_negative_control_structure(
        behavior, candidate.code, negative_code, plan
    )
    metadata.cache_key = cache_key
    metadata.generation_method = generation_method
    metadata.retry_count = 1 if used_llm else 0
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


def _execution_was_run(execution: dict[str, Any]) -> bool:
    return bool(
        isinstance(execution, dict)
        and execution
        and (
            "command" in execution
            or "returncode" in execution
            or "return_code" in execution
            or "stdout" in execution
            or "stderr" in execution
            or "status" in execution
        )
    )


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
        positive_executed = _execution_was_run(execution if isinstance(execution, dict) else {})
        negative_executed = _execution_was_run(
            negative_execution if isinstance(negative_execution, dict) else {}
        )
        runs.append(
            {
                "patch_id": str(attempt.get("round_id") if isinstance(attempt, dict) else ""),
                "patch_valid": patch_valid,
                "positive_result": execution if isinstance(execution, dict) else {},
                "negative_result": negative_execution if isinstance(negative_execution, dict) else {},
                "positive_executed": bool(attempt.get("positive_executed", positive_executed)),
                "negative_executed": bool(attempt.get("negative_executed", negative_executed)),
                "negative_skip_reason": str(attempt.get("negative_skip_reason") or ""),
                "paired_execution_complete": bool(
                    attempt.get(
                        "paired_execution_complete",
                        positive_executed and negative_executed,
                    )
                ),
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
    paired_runs = [
        run
        for run in valid_runs
        if run.get("positive_executed")
        and run.get("negative_executed")
        and _execution_was_run(run.get("positive_result") or {})
        and _execution_was_run(run.get("negative_result") or {})
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
    paired_positive_pass = [
        run for run in paired_runs if _execution_dict_pass(run.get("positive_result") or {})
    ]
    paired_negative_stable: list[dict[str, Any]] = []
    paired_conflicts: list[dict[str, Any]] = []
    for run in paired_runs:
        positive_result = run.get("positive_result") or {}
        negative_result = run.get("negative_result") or {}
        negative_status = str(negative_result.get("status") or negative_result.get("outcome") or "")
        if _is_non_executable_status(negative_status):
            paired_conflicts.append(run)
            continue
        if _execution_dict_pass(negative_result):
            paired_negative_stable.append(run)
            continue
        comparison = compare_failure_signatures(
            _execution_dict_signature(positive_result),
            _execution_dict_signature(negative_result),
        )
        if comparison == "SAME" and not _execution_dict_pass(positive_result):
            paired_conflicts.append(run)
        else:
            paired_negative_stable.append(run)
    if len(paired_runs) < max(1, min_valid_surrogate_patches):
        evidence.oracle_stability = {
            "status": "UNKNOWN",
            "score": 0.0,
            "valid_paired_patch_ids": [str(run.get("patch_id") or "") for run in paired_runs],
            "positive_pass_count": len(paired_positive_pass),
            "negative_stable_count": len(paired_negative_stable),
            "conflicting_patch_ids": [str(run.get("patch_id") or "") for run in paired_conflicts],
            "reason": "not enough valid paired surrogate executions for oracle stability",
        }
    else:
        positive_ratio = len(paired_positive_pass) / len(paired_runs)
        negative_ratio = len(paired_negative_stable) / len(paired_runs)
        if (
            positive_ratio >= surrogate_consensus_threshold
            and negative_ratio >= surrogate_consensus_threshold
            and not paired_conflicts
        ):
            evidence.oracle_stability = {
                "status": "STABLE",
                "score": min(positive_ratio, negative_ratio),
                "valid_paired_patch_ids": [str(run.get("patch_id") or "") for run in paired_runs],
                "positive_pass_count": len(paired_positive_pass),
                "negative_stable_count": len(paired_negative_stable),
                "conflicting_patch_ids": [],
                "reason": "positive and negative outcomes are stable across paired surrogate repairs",
            }
        else:
            evidence.oracle_stability = {
                "status": "UNSTABLE",
                "score": min(positive_ratio, negative_ratio),
                "valid_paired_patch_ids": [str(run.get("patch_id") or "") for run in paired_runs],
                "positive_pass_count": len(paired_positive_pass),
                "negative_stable_count": len(paired_negative_stable),
                "conflicting_patch_ids": [str(run.get("patch_id") or "") for run in paired_conflicts],
                "reason": "paired surrogate repairs give conflicting positive/negative oracle evidence",
            }
    oracle_status = str(evidence.oracle_stability.get("status") or "UNKNOWN")
    if trigger_status == "SUPPORTED" and repair_status == "SUPPORTED":
        evidence.bidirectional_support = {
            "status": "STRONG",
            "reason": "test-side trigger ablation and program-side surrogate repair both support the candidate",
        }
    elif "SUPPORTED" in {trigger_status, repair_status} or "WEAK" in {trigger_status, repair_status}:
        evidence.bidirectional_support = {
            "status": "PARTIAL",
            "reason": "one side of the counterfactual evidence supports the candidate",
        }
    elif trigger_status == "UNSUPPORTED" and repair_status == "UNSUPPORTED":
        if oracle_status == "UNKNOWN":
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
    surrogate_runs = evidence.surrogate_runs if evidence else []
    paired_runs = [
        run for run in surrogate_runs
        if isinstance(run, dict) and run.get("paired_execution_complete")
    ]
    oracle_stability = evidence.oracle_stability if evidence else {}
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
        "oracle_stability": (
            (oracle_stability or {}).get("status")
            if evidence
            else "UNKNOWN"
        ),
        "positive_surrogate_executed_count": sum(
            1 for run in surrogate_runs if isinstance(run, dict) and run.get("positive_executed")
        ),
        "negative_surrogate_executed_count": sum(
            1 for run in surrogate_runs if isinstance(run, dict) and run.get("negative_executed")
        ),
        "paired_surrogate_execution_count": len(paired_runs),
        "complete_2x2": bool(
            evidence
            and evidence.positive_buggy
            and evidence.negative_buggy
            and evidence.negative_buggy.get("outcome") != "UNKNOWN"
            and paired_runs
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
