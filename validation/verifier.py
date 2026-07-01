"""Buggy-only verifier."""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.prompts import BUGGY_ONLY_VERIFIER_SYSTEM_PROMPT, BUGGY_ONLY_VERIFIER_USER_PROMPT
from ..core.schema import BehaviorTarget, CandidateTest, ExecutionResult, VerifierDecision
from .semantic_guard import audit_candidate
from ..core.utils import extract_json_object


def _missing_check_target(
    behavior: BehaviorTarget,
    source_context: str,
) -> bool:
    text = " ".join(
        [
            behavior.issue_summary,
            str(behavior.error_symptom.get("text") or ""),
            str(behavior.expected_behavior.get("text") or ""),
        ]
    ).lower()
    mentions_check = any(marker in text for marker in ("check", "检查", "验证"))
    describes_missing = any(
        marker in text
        for marker in ("add check", "missing", "no check", "缺少", "没有", "新增", "添加")
    )
    return (
        mentions_check
        and describes_missing
        and "def check(" in source_context
    )


def _missing_logging_target(behavior: BehaviorTarget) -> bool:
    text = " ".join(
        [
            behavior.issue_summary,
            str(behavior.error_symptom.get("text") or ""),
            str(behavior.expected_behavior.get("text") or ""),
        ]
    ).lower()
    mentions_logging = any(
        marker in text
        for marker in ("logger", "logging", "log message", "日志", "记录异常")
    )
    describes_missing = any(
        marker in text
        for marker in ("missing", "doesn't have", "没有", "缺少", "未记录")
    )
    return mentions_logging and describes_missing


def _normalize_focus(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return default


def _ask_llm(
    issue_text: str,
    behavior: BehaviorTarget,
    candidate: CandidateTest,
    execution: ExecutionResult,
    llm_client: Any,
    default_decision: str,
    host_context: dict[str, Any] | None = None,
    source_context: str = "",
) -> VerifierDecision:
    prompt = BUGGY_ONLY_VERIFIER_USER_PROMPT.format(
        issue_text=issue_text,
        behavior_json=json.dumps(behavior.to_dict(), ensure_ascii=False),
        host_context_json=json.dumps(host_context or {}, ensure_ascii=False),
        source_context=source_context,
        candidate_code=candidate.code,
        execution_json=json.dumps(execution.to_dict(), ensure_ascii=False),
    )
    data = extract_json_object(
        llm_client.chat(BUGGY_ONLY_VERIFIER_SYSTEM_PROMPT, prompt)
    )
    decision = str(data.get("decision") or default_decision)
    if decision not in {
        "accept",
        "repair_setup",
        "repair_trigger",
        "repair_oracle",
        "reject",
    }:
        decision = default_decision
    result = VerifierDecision(
        candidate.instance_id,
        decision,
        str(data.get("reason") or ""),
        _normalize_focus(data.get("focus"), ["oracle"]),
        str(data.get("next_action") or ""),
    )
    if result.decision == "accept":
        reason = result.reason.lower()
        setup_markers = (
            "测试设置问题", "setup error", "setup问题", "环境问题", "环境配置问题",
            "测试自身异常", "未注册", "不存在的 fixture", "collection error",
        )
        trigger_markers = (
            "未触发", "没有触发", "未执行到", "没有执行到", "触发条件不满足",
            "路径无关", "与issue无关", "unrelated failure", "did not trigger",
        )
        oracle_markers = (
            "断言方向错误", "断言对象错误", "断言过于", "oracle错误",
            "期望行为不符", "把buggy行为", "assertion direction", "wrong oracle",
        )
        if any(marker in reason for marker in setup_markers):
            result.decision = "repair_setup"
            result.focus = ["setup"]
            result.next_action = result.next_action or "恢复可执行测试上下文后重新运行。"
        elif any(marker in reason for marker in trigger_markers) or re.search(
            r"未(?:能|能正确|正确)?[^。；，,]{0,12}触发", reason
        ):
            result.decision = "repair_trigger"
            result.focus = ["trigger"]
            result.next_action = result.next_action or "按 Issue 的精确输入和调用链修复触发路径。"
        elif any(marker in reason for marker in oracle_markers):
            result.decision = "repair_oracle"
            result.focus = ["oracle"]
            result.next_action = result.next_action or "用 expected_behavior 重写最小稳定 oracle。"
    return result


def verify_buggy_only(
    issue_text: str,
    behavior: BehaviorTarget,
    candidate: CandidateTest,
    execution: ExecutionResult,
    llm_client: Any | None = None,
    host_context: dict[str, Any] | None = None,
    source_context: str = "",
) -> VerifierDecision:
    status = execution.status
    missing_check_target = _missing_check_target(behavior, source_context)
    if status in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR"}:
        next_action = "修复 import、fixture、class、setup 或语法。"
        if missing_check_target and ".check(" in candidate.code:
            next_action = (
                "保留真实 check() 调用，只修复字段/对象缺少模型绑定、name、app_label "
                "或其他元数据的问题。优先复用 HostContext 模型并通过 _meta.get_field() "
                "取得字段；standalone Field 可调用 set_attributes_from_name()。"
            )
        return VerifierDecision(
            candidate.instance_id,
            "repair_setup",
            status,
            ["setup"],
            next_action,
        )
    semantic_problem = audit_candidate(behavior, candidate.code)
    if semantic_problem:
        oracle_problem = any(
            marker in semantic_problem
            for marker in ("oracle", "断言", "raises", "hasattr", "expected_behavior")
        )
        return VerifierDecision(
            candidate.instance_id,
            "repair_oracle" if oracle_problem else "repair_trigger",
            semantic_problem,
            ["oracle" if oracle_problem else "trigger"],
            semantic_problem,
        )
    if status == "PASS":
        if missing_check_target:
            return VerifierDecision(
                candidate.instance_id,
                "repair_trigger",
                (
                    "这是缺失系统检查类 Issue；buggy 版本通过说明当前测试没有用"
                    " check() 观察修复后应新增的检查证据。"
                ),
                ["trigger", "oracle"],
                (
                    "必须调用源码真实 check() 生命周期，并断言 expected_behavior "
                    "要求新增的稳定检查结果存在；不得改用 clean()、save()、构造器"
                    "异常或普通值长度验证替代系统检查。"
                ),
            )
        if llm_client:
            try:
                decision = _ask_llm(
                    issue_text,
                    behavior,
                    candidate,
                    execution,
                    llm_client,
                    "repair_trigger",
                    host_context,
                    source_context,
                )
                if decision.decision == "accept":
                    decision.decision = "repair_trigger"
                    decision.reason = (
                        "buggy 版本通过，不能接受为 BRT。"
                        + (f" 语义分析：{decision.reason}" if decision.reason else "")
                    )
                    decision.focus = ["trigger"]
                    decision.next_action = (
                        decision.next_action
                        or "找出尚未覆盖的 Issue 输入、状态、调用链或优化分支。"
                    )
                return decision
            except Exception:  # noqa: BLE001
                pass
        return VerifierDecision(candidate.instance_id, "repair_trigger", "buggy 版本通过，说明缺陷路径未触发。", ["trigger"], "加强输入、状态或调用链变异。")
    if status in {"ISSUE_ALIGNED_FAIL", "ASSERTION_FAIL"}:
        if (
            status == "ASSERTION_FAIL"
            and missing_check_target
            and ".check(" in candidate.code
            and "assertRaises" not in candidate.code
            and "pytest.raises" not in candidate.code
        ):
            return VerifierDecision(
                candidate.instance_id,
                "accept",
                (
                    "测试调用了源码中的 check() 生命周期，并因修复后应出现的检查证据"
                    "在 buggy 版本中缺失而断言失败；这与缺少检查类 Issue 对齐。"
                ),
                ["trigger", "oracle"],
                "接受当前 BRT。",
            )
        if (
            status == "ASSERTION_FAIL"
            and _missing_logging_target(behavior)
            and ("assertLogs" in candidate.code or "caplog" in candidate.code)
        ):
            return VerifierDecision(
                candidate.instance_id,
                "accept",
                (
                    "测试调用了目标行为并捕获修复后应出现的日志；buggy 版本因没有日志"
                    "证据而断言失败，与缺失日志类 Issue 对齐。"
                ),
                ["trigger", "oracle"],
                "接受当前 BRT。",
            )
        if llm_client:
            try:
                return _ask_llm(
                    issue_text,
                    behavior,
                    candidate,
                    execution,
                    llm_client,
                    "repair_oracle" if status == "ASSERTION_FAIL" else "repair_trigger",
                    host_context,
                    source_context,
                )
            except Exception:  # noqa: BLE001
                pass
        if status == "ASSERTION_FAIL":
            return VerifierDecision(candidate.instance_id, "repair_oracle", "断言失败但是否对齐不确定。", ["oracle"], "插桩观测后重写 assert。")
        return VerifierDecision(candidate.instance_id, "repair_trigger", "关键词规则认为相关，但未完成语义验证。", ["trigger", "oracle"], "重新核对目标 API、输入和失败语义。")
    if status == "UNRELATED_FAIL" and llm_client:
        try:
            return _ask_llm(
                issue_text,
                behavior,
                candidate,
                execution,
                llm_client,
                "repair_trigger",
                host_context,
                source_context,
            )
        except Exception:  # noqa: BLE001
            pass
    if status == "TIMEOUT":
        return VerifierDecision(candidate.instance_id, "reject", "执行超时。", ["setup"], "放弃或缩小测试。")
    return VerifierDecision(candidate.instance_id, "repair_trigger", f"失败类型 {status}，和 issue 对齐不足。", ["trigger", "oracle"], "重新对齐触发路径。")
