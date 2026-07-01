"""Strict semantic acceptance gate for buggy-only BRT execution."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from ..core.prompts import STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT, STRICT_SEMANTIC_VERIFIER_USER_PROMPT
from ..core.schema import (
    BehaviorTarget, CandidateTest, ExecutionResult, ProtocolRecovery,
    StrictVerifierResult, VerifierDecision,
)
from .semantic_guard import audit_candidate
from ..core.utils import extract_json_object, safe_json_dump, truncate_text, write_text


_DECISIONS = {"accept", "repair_setup", "repair_trigger", "repair_oracle", "reject"}
_FAILURE_CLASSES = {
    "setup", "syntax", "collect", "timeout", "buggy_pass", "target_not_hit",
    "side_path", "oracle_wrong", "oracle_too_strong", "issue_aligned",
}


def _private_or_brittle_oracle(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "syntax"
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        expression = ast.get_source_segment(code, node.test) or ""
        if re.search(r"\._[A-Za-z_]", expression):
            return "断言依赖私有属性或内部缓存。"
        if ("repr(" in expression or "str(" in expression or "query" in expression.lower()) and "==" in expression and len(expression) > 120:
            return "断言对完整 repr/SQL/内部字符串做了过强精确比较。"
    return ""


def _forced_result(instance_id: str, execution: ExecutionResult, reason: str) -> StrictVerifierResult | None:
    mapping = {
        "SETUP_ERROR": ("repair_setup", "setup"),
        "SYNTAX_ERROR": ("repair_setup", "syntax"),
        "COLLECT_ERROR": ("repair_setup", "collect"),
        "TIMEOUT": ("reject", "timeout"),
        "PASS": ("repair_trigger", "buggy_pass"),
    }
    if execution.status not in mapping:
        return None
    decision, failure = mapping[execution.status]
    return StrictVerifierResult(instance_id, decision, failure, False, False, False, reason or execution.status, decision)


def verify_strict_semantics(
    issue_text: str,
    behavior: BehaviorTarget,
    protocol: ProtocolRecovery | None,
    candidate: CandidateTest,
    execution: ExecutionResult,
    source_context: str,
    llm_client: Any,
    output_dir: str,
    round_id: int,
) -> tuple[VerifierDecision, StrictVerifierResult]:
    forced = _forced_result(candidate.instance_id, execution, execution.status)
    semantic_problem = audit_candidate(behavior, candidate.code)
    brittle_problem = _private_or_brittle_oracle(candidate.code)
    if forced is not None:
        result = forced
    elif semantic_problem or brittle_problem:
        problem = semantic_problem or brittle_problem
        oracle_problem = any(token in problem for token in ("断言", "oracle", "raises", "expected_behavior", "私有", "SQL", "repr"))
        decision = "repair_oracle" if oracle_problem else "repair_trigger"
        result = StrictVerifierResult(
            candidate.instance_id, decision,
            "oracle_too_strong" if brittle_problem else ("oracle_wrong" if oracle_problem else "side_path"),
            False, False, False, problem, decision,
        )
    else:
        prompt = STRICT_SEMANTIC_VERIFIER_USER_PROMPT.format(
            issue_text=issue_text,
            behavior_json=json.dumps(behavior.to_dict(), ensure_ascii=False),
            protocol_json=json.dumps(protocol.to_dict() if protocol else {}, ensure_ascii=False),
            candidate_code=candidate.code,
            command=execution.command,
            execution_status=execution.status,
            execution_log=truncate_text(execution.stdout + "\n" + execution.stderr, 16000),
            source_context=truncate_text(source_context, 18000),
        )
        write_text(str(Path(output_dir) / "prompts" / f"strict_verifier_round_{round_id}.txt"), STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT + "\n\n" + prompt)
        response = llm_client.chat(STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT, prompt)
        write_text(str(Path(output_dir) / "responses" / f"strict_verifier_round_{round_id}.txt"), response)
        data = extract_json_object(response)
        decision = str(data.get("decision") or "repair_trigger")
        failure = str(data.get("failure_class") or "side_path")
        result = StrictVerifierResult(
            candidate.instance_id,
            decision if decision in _DECISIONS else "repair_trigger",
            failure if failure in _FAILURE_CLASSES else "side_path",
            bool(data.get("target_hit")),
            bool(data.get("oracle_grounded_in_issue")),
            bool(data.get("uses_public_behavior")),
            str(data.get("reason") or ""),
            str(data.get("next_action") or decision),
        )
        if result.decision == "accept" and not (
            execution.returncode != 0
            and execution.status not in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT"}
            and result.failure_class == "issue_aligned"
            and result.target_hit
            and result.oracle_grounded_in_issue
            and result.uses_public_behavior
        ):
            if not result.target_hit:
                result.decision, result.failure_class = "repair_trigger", "target_not_hit"
            else:
                result.decision = "repair_oracle"
                result.failure_class = "oracle_wrong" if not result.oracle_grounded_in_issue else "oracle_too_strong"
            result.next_action = result.decision
            result.reason = "严格 accept 条件未全部满足。" + result.reason
    safe_json_dump(result.to_dict(), str(Path(output_dir) / f"strict_verifier_round_{round_id}.json"))
    decision = VerifierDecision(
        instance_id=candidate.instance_id,
        decision=result.decision,
        reason=result.reason,
        focus=[result.next_action.replace("repair_", "")],
        next_action=result.next_action,
    )
    return decision, result
