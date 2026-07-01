"""Create and validate a small, issue-guided mutation plan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.prompts import SEED_MUTATION_PLAN_SYSTEM_PROMPT, SEED_MUTATION_PLAN_USER_PROMPT
from ..core.schema import BehaviorTarget, HostContext, MutationPlan, ProtocolRecovery
from ..core.utils import extract_json_object, safe_json_dump, write_text


ALLOWED_MUTATION_OPS = {
    "ARG_VALUE_REPLACE", "ARG_BOUNDARY_EXPAND", "OPERATOR_FLIP",
    "CALL_CHAIN_EXTEND", "STATE_MUTATION", "FIXTURE_DATA_MUTATION",
    "CONFIG_MUTATION", "MOCK_BEHAVIOR_MUTATION", "LIFECYCLE_TRIGGER",
    "SERIALIZATION_TRIGGER", "WARNING_LOG_TRIGGER",
}


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _normalize_plan(instance_id: str, round_id: int, data: dict[str, Any], behavior: BehaviorTarget) -> MutationPlan:
    ops = [item for item in _strings(data.get("mutation_ops")) if item in ALLOWED_MUTATION_OPS][:3]
    if not ops:
        ops = ["CALL_CHAIN_EXTEND"]
    risk = str(data.get("risk") or "medium").lower()
    if risk not in {"low", "medium", "high"}:
        risk = "medium"
    expected = str(behavior.expected_behavior.get("text") or "")
    return MutationPlan(
        instance_id=instance_id,
        round_id=round_id,
        mutation_goal=str(data.get("mutation_goal") or ""),
        preserve_from_seed=_strings(data.get("preserve_from_seed")),
        target_api=_strings(data.get("target_api")),
        target_path=_strings(data.get("target_path")),
        mutation_ops=ops,
        expected_behavior=expected,
        oracle_strategy=str(data.get("oracle_strategy") or "验证 Issue 明确描述的公开行为"),
        why_this_should_trigger=str(data.get("why_this_should_trigger") or ""),
        risk=risk,
    )


def build_mutation_plan(
    instance_id: str,
    round_id: int,
    behavior: BehaviorTarget,
    host: HostContext,
    protocol: ProtocolRecovery | None,
    llm_client: Any,
    output_dir: str,
    execution_feedback: str = "",
    verifier_feedback: dict[str, Any] | None = None,
) -> MutationPlan:
    prompt = SEED_MUTATION_PLAN_USER_PROMPT.format(
        behavior_json=json.dumps(behavior.to_dict(), ensure_ascii=False),
        host_context_json=json.dumps(host.to_dict(), ensure_ascii=False),
        protocol_json=json.dumps(protocol.to_dict() if protocol else {}, ensure_ascii=False),
        execution_feedback=execution_feedback or "无",
        verifier_feedback=json.dumps(verifier_feedback or {}, ensure_ascii=False),
    )
    prompt_path = Path(output_dir) / "prompts" / f"mutation_plan_round_{round_id}.txt"
    response_path = Path(output_dir) / "responses" / f"mutation_plan_round_{round_id}.txt"
    write_text(str(prompt_path), SEED_MUTATION_PLAN_SYSTEM_PROMPT + "\n\n" + prompt)
    response = llm_client.chat(SEED_MUTATION_PLAN_SYSTEM_PROMPT, prompt)
    write_text(str(response_path), response)
    try:
        data = extract_json_object(response)
    except ValueError as first_error:
        retry_prompt = (
            prompt
            + "\n\n上一次 mutation plan 无法解析："
            + str(first_error)
            + "。请重新输出一个完整合法 JSON 对象，不要 Markdown、注释或解释。"
        )
        response = llm_client.chat(SEED_MUTATION_PLAN_SYSTEM_PROMPT, retry_prompt)
        write_text(str(Path(output_dir) / "responses" / f"mutation_plan_round_{round_id}_json_retry.txt"), response)
        data = extract_json_object(response)
    plan = _normalize_plan(instance_id, round_id, data, behavior)
    safe_json_dump(plan.to_dict(), str(Path(output_dir) / f"mutation_round_{round_id}_plan.json"))
    return plan
