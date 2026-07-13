"""Stage 1: rewrite raw issues into structured behavior targets."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from ..io.io_utils import format_code_context, format_test_context
from ..core.prompts import ISSUE_REWRITE_SYSTEM_PROMPT, ISSUE_REWRITE_USER_PROMPT
from ..core.schema import BehaviorTarget, InstanceContext
from ..core.utils import ensure_dir, extract_json_object, now_timestamp, safe_json_dump, write_text


REQUIRED_FIELDS = [
    "issue_summary",
    "trigger_condition",
    "error_symptom",
    "expected_behavior",
    "target_apis",
    "suspected_bug_locations",
    "related_test_seeds",
    "mutation_hints",
    "observation_points",
    "assertion_hints",
    "setup_hints",
    "essential_trigger_factors",
    "trigger_ablation_rules",
    "trace_targets",
    "public_observation_schema",
    "trigger_contract",
    "failure_contract",
    "expected_contract",
    "localization_contract",
    "uncertainties",
]


def behavior_from_dict(instance_id: str, data: dict[str, Any]) -> BehaviorTarget:
    normalized = {k: data.get(k) for k in REQUIRED_FIELDS}
    normalized.setdefault("issue_summary", "")
    for key in [
        "target_apis",
        "suspected_bug_locations",
        "related_test_seeds",
        "mutation_hints",
        "observation_points",
        "assertion_hints",
        "setup_hints",
        "essential_trigger_factors",
        "trigger_ablation_rules",
        "trace_targets",
        "uncertainties",
    ]:
        if not isinstance(normalized.get(key), list):
            normalized[key] = []
    if not isinstance(normalized.get("public_observation_schema"), list):
        normalized["public_observation_schema"] = []
    for key in [
        "trigger_condition",
        "error_symptom",
        "expected_behavior",
        "trigger_contract",
        "failure_contract",
        "expected_contract",
        "localization_contract",
    ]:
        if not isinstance(normalized.get(key), dict):
            normalized[key] = {}
    normalized = _induce_contracts(normalized)
    return BehaviorTarget(instance_id=instance_id, raw=data, **normalized)


def _induce_contracts(data: dict[str, Any]) -> dict[str, Any]:
    """Map legacy BehaviorTarget evidence into executable contracts.

    The mapping only copies existing evidence. It deliberately leaves unsupported
    contract slots empty so old behavior caches remain usable without inventing
    new issue facts.
    """

    trigger = data.get("trigger_contract") or {}
    if not trigger:
        trigger_condition = data.get("trigger_condition") or {}
        factors = data.get("essential_trigger_factors") or []
        trigger = {
            "required_conditions": [trigger_condition]
            if trigger_condition.get("text")
            else [],
            "target_apis": list(data.get("target_apis") or []),
            "call_sequence": [
                item for item in factors if item.get("factor_type") == "call_sequence"
            ],
            "state_constraints": [
                item
                for item in factors
                if item.get("factor_type") in {"state", "configuration", "lifecycle"}
            ],
            "boundary_conditions": [
                item
                for item in factors
                if item.get("factor_type") in {"boundary", "input_shape"}
            ],
        }
    failure = data.get("failure_contract") or {}
    if not failure:
        symptom = data.get("error_symptom") or {}
        symptom_type = str(symptom.get("symptom_type") or "").strip()
        failure = {
            "buggy_symptom": symptom,
            "allowed_failure_types": [symptom_type] if symptom_type else [],
            "forbidden_side_failures": [
                "setup_error",
                "collect_error",
                "syntax_error",
                "environment_error",
            ],
        }
    expected = data.get("expected_contract") or {}
    if not expected:
        expected = {
            "expected_behavior": data.get("expected_behavior") or {},
            "public_observation_targets": list(
                data.get("public_observation_schema") or []
            ),
            "preferred_oracle_families": [
                str(item.get("preferred_assertion_style") or "")
                for item in data.get("assertion_hints") or []
                if item.get("preferred_assertion_style")
            ],
        }
    localization = data.get("localization_contract") or {}
    if not localization:
        locations = data.get("suspected_bug_locations") or []
        localization = {
            "suspected_files": list(
                dict.fromkeys(
                    str(item.get("path") or "") for item in locations if item.get("path")
                )
            ),
            "suspected_functions": list(
                dict.fromkeys(
                    str(item.get("object") or "")
                    for item in locations
                    if item.get("object")
                )
            ),
            "trace_targets": list(data.get("trace_targets") or []),
        }
    data["trigger_contract"] = trigger
    data["failure_contract"] = failure
    data["expected_contract"] = expected
    data["localization_contract"] = localization
    return data


def save_enhanced_issue_copy(behavior: BehaviorTarget, output_dir: str) -> None:
    enhanced = behavior.to_dict()
    safe_json_dump(enhanced, str(Path(output_dir) / "enhanced_issue.json"))
    lines = [
        f"instance_id: {behavior.instance_id}",
        "",
        f"issue_summary: {behavior.issue_summary}",
        "",
        "trigger_condition:",
        json_dumps_for_text(behavior.trigger_condition),
        "",
        "error_symptom:",
        json_dumps_for_text(behavior.error_symptom),
        "",
        "expected_behavior:",
        json_dumps_for_text(behavior.expected_behavior),
        "",
        "target_apis:",
        json_dumps_for_text(behavior.target_apis),
        "",
        "mutation_hints:",
        json_dumps_for_text(behavior.mutation_hints),
        "",
        "observation_points:",
        json_dumps_for_text(behavior.observation_points),
        "",
        "assertion_hints:",
        json_dumps_for_text(behavior.assertion_hints),
        "",
        "setup_hints:",
        json_dumps_for_text(behavior.setup_hints),
        "",
        "essential_trigger_factors:",
        json_dumps_for_text(behavior.essential_trigger_factors),
        "",
        "trigger_ablation_rules:",
        json_dumps_for_text(behavior.trigger_ablation_rules),
        "",
        "trace_targets:",
        json_dumps_for_text(behavior.trace_targets),
        "",
        "public_observation_schema:",
        json_dumps_for_text(behavior.public_observation_schema),
        "",
        "trigger_contract:",
        json_dumps_for_text(behavior.trigger_contract),
        "",
        "failure_contract:",
        json_dumps_for_text(behavior.failure_contract),
        "",
        "expected_contract:",
        json_dumps_for_text(behavior.expected_contract),
        "",
        "localization_contract:",
        json_dumps_for_text(behavior.localization_contract),
        "",
        "uncertainties:",
        json_dumps_for_text(behavior.uncertainties),
        "",
    ]
    write_text(str(Path(output_dir) / "enhanced_issue.txt"), "\n".join(lines))


def json_dumps_for_text(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2)


def rewrite_issue(
    context: InstanceContext,
    llm_client: Any,
    output_dir: str,
    code_max_chars: int = 18000,
    test_max_chars: int = 18000,
) -> BehaviorTarget:
    ensure_dir(output_dir)
    code_context = format_code_context(context.retrieved_code, code_max_chars)
    test_context = format_test_context(context.retrieved_tests, test_max_chars)
    user_prompt = ISSUE_REWRITE_USER_PROMPT.format(
        issue_text=context.issue_text,
        code_context=code_context,
        test_context=test_context,
    )
    prompt_path = str(Path(output_dir) / "prompt.txt")
    response_path = str(Path(output_dir) / "response.txt")
    write_text(prompt_path, ISSUE_REWRITE_SYSTEM_PROMPT + "\n\n" + user_prompt)
    meta = {"instance_id": context.instance_id, "started_at": now_timestamp(), "status": "RUNNING"}
    try:
        response = llm_client.chat(ISSUE_REWRITE_SYSTEM_PROMPT, user_prompt)
        write_text(response_path, response)
        try:
            data = extract_json_object(response)
        except ValueError as first_error:
            retry_prompt = (
                user_prompt
                + "\n\n上一次响应无法解析为完整 JSON："
                + str(first_error)
                + "。请重新输出单个完整合法 JSON 对象；不要省略字段，不要截断，"
                + "不要输出 Markdown 或解释。"
            )
            response = llm_client.chat(ISSUE_REWRITE_SYSTEM_PROMPT, retry_prompt)
            write_text(str(Path(output_dir) / "response_json_retry.txt"), response)
            data = extract_json_object(response)
        behavior = behavior_from_dict(context.instance_id, data)
        behavior.save_json(str(Path(output_dir) / "behavior_target.json"))
        save_enhanced_issue_copy(behavior, output_dir)
        meta.update({"status": "OK", "finished_at": now_timestamp()})
        safe_json_dump(meta, str(Path(output_dir) / "meta.json"))
        return behavior
    except Exception as exc:  # noqa: BLE001
        meta.update({"status": "ERROR", "finished_at": now_timestamp(), "error": str(exc), "traceback": traceback.format_exc()})
        safe_json_dump(meta, str(Path(output_dir) / "meta.json"))
        raise
