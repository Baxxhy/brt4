"""LLM parameter solver for NS-GEM operators."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.schema import IssueGate, MutationOperator, NSGEMOperatorPlan, TestSkeleton
from ..core.utils import extract_json_object, safe_json_dump, write_text
from ..prompts.loader import load_prompt


def _fallback_plan(instance_id: str, operators: list[MutationOperator], reason: str) -> NSGEMOperatorPlan:
    applicable = [item for item in operators if item.applicable]
    selected = applicable[0] if applicable else MutationOperator("Mut_CallChain", applicable=True, risk="high")
    return NSGEMOperatorPlan(
        instance_id=instance_id,
        selected_operator=selected.name,
        target_node_id=selected.target_node,
        operator_parameters={},
        preserve_nodes=[],
        expected_effect=selected.description,
        risk=selected.risk,
        reason=selected.reason,
        valid=False,
        fallback_reason=reason,
    )


def build_ns_gem_operator_plan(
    instance_id: str,
    issue_gate: IssueGate,
    skeleton: TestSkeleton,
    operators: list[MutationOperator],
    llm_client: Any,
    output_dir: str,
    execution_feedback: str = "",
    current_candidate_code: str = "",
) -> NSGEMOperatorPlan:
    applicable = [item.name for item in operators if item.applicable]
    if not applicable:
        plan = _fallback_plan(instance_id, operators, "no applicable operator")
        safe_json_dump(plan.to_dict(), str(Path(output_dir) / "ns_gem_operator_plan.json"))
        return plan
    system = load_prompt("ns_gem_mutation_plan", "system")
    user_template = load_prompt("ns_gem_mutation_plan", "user")
    user = user_template.format(
        issue_gate_json=json.dumps(issue_gate.to_dict(), ensure_ascii=False),
        test_skeleton_json=json.dumps(skeleton.to_dict(), ensure_ascii=False),
        operator_applicability_json=json.dumps([item.to_dict() for item in operators], ensure_ascii=False),
        execution_feedback=execution_feedback or "none",
        current_candidate_code=current_candidate_code[-6000:] if current_candidate_code else "",
    )
    write_text(str(Path(output_dir) / "prompts" / "ns_gem_operator_plan.txt"), system + "\n\n" + user)
    try:
        response = llm_client.chat(system, user)
        write_text(str(Path(output_dir) / "responses" / "ns_gem_operator_plan.txt"), response)
        data = extract_json_object(response)
        selected = str(data.get("selected_operator") or "")
        if selected not in applicable:
            raise ValueError(f"selected_operator {selected!r} is not applicable")
        risk = str(data.get("risk") or "medium").lower()
        if risk not in {"low", "medium", "high"}:
            risk = "medium"
        plan = NSGEMOperatorPlan(
            instance_id=instance_id,
            selected_operator=selected,
            target_node_id=str(data.get("target_node_id") or ""),
            operator_parameters=data.get("operator_parameters") if isinstance(data.get("operator_parameters"), dict) else {},
            preserve_nodes=[str(item) for item in data.get("preserve_nodes") or []],
            expected_effect=str(data.get("expected_effect") or ""),
            risk=risk,
            reason=str(data.get("reason") or ""),
            valid=True,
            fallback_reason="",
        )
    except Exception as exc:  # noqa: BLE001
        plan = _fallback_plan(instance_id, operators, f"planner fallback: {exc}")
    safe_json_dump(plan.to_dict(), str(Path(output_dir) / "ns_gem_operator_plan.json"))
    return plan
