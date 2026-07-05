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
    "uncertainties",
]


def behavior_from_dict(instance_id: str, data: dict[str, Any]) -> BehaviorTarget:
    normalized = {k: data.get(k) for k in REQUIRED_FIELDS}
    normalized.setdefault("issue_summary", "")
    for key in ["target_apis", "suspected_bug_locations", "related_test_seeds", "mutation_hints", "observation_points", "assertion_hints", "setup_hints", "uncertainties"]:
        if not isinstance(normalized.get(key), list):
            normalized[key] = []
    for key in ["trigger_condition", "error_symptom", "expected_behavior"]:
        if not isinstance(normalized.get(key), dict):
            normalized[key] = {}
    return BehaviorTarget(instance_id=instance_id, raw=data, **normalized)


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
