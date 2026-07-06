"""Soft oracle and surrogate risk checks for candidate selection."""

from __future__ import annotations

import ast
import re
from typing import Any

from ..core.schema import BehaviorTarget, DualVersionResult


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _level(reasons: list[str]) -> str:
    if any(reason.startswith("high:") for reason in reasons):
        return "high"
    if reasons:
        return "medium"
    return "low"


def assess_oracle_risk(
    candidate_code: str,
    behavior: BehaviorTarget,
    issue_text: str = "",
    execution_log: str = "",
    observation_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del observation_json
    reasons: list[str] = []
    signals: dict[str, Any] = {}
    expected = str(behavior.expected_behavior.get("text") or "").lower()
    symptom = str(behavior.error_symptom.get("text") or "").lower()
    issue = f"{issue_text}\n{expected}\n{symptom}".lower()
    try:
        tree = ast.parse(candidate_code)
    except SyntaxError:
        return {
            "level": "medium",
            "reasons": ["medium: candidate is not parseable for oracle risk checks"],
            "signals": {"syntax_error": True},
        }
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            expression = ast.get_source_segment(candidate_code, node.test) or ""
            low_expr = expression.lower()
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                reasons.append("high: assert True placeholder oracle")
            if re.search(r"\._[A-Za-z_]", expression):
                reasons.append("high: assertion reads a private attribute")
            if any(token in low_expr for token in ("_cache", "cache", "internal", "_state")):
                reasons.append("medium: assertion appears to inspect cache/internal state")
            if ("repr(" in low_expr or "str(" in low_expr or "query" in low_expr) and len(expression) > 120:
                reasons.append("high: assertion compares long repr/string/SQL-like expression")
            if re.search(r"['\"][^'\"]{120,}['\"]", expression):
                reasons.append("medium: assertion contains a very long exact literal")
        if isinstance(node, ast.Call):
            call_name = _name(node.func)
            if call_name in {"pytest.raises", "raises"} and node.args:
                raised = _name(node.args[0])
                if raised in {"Exception", "BaseException"}:
                    reasons.append("high: pytest.raises catches broad Exception/BaseException")
                if len(ast.get_source_segment(candidate_code, node) or "") > 180:
                    reasons.append("medium: exception assertion may depend on long exact message")
            if call_name.endswith(("assert_called_once", "assert_called_once_with")):
                reasons.append("medium: oracle checks mock/internal call count")
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                broad = handler.type is None or _name(handler.type) in {"Exception", "BaseException"}
                swallowed = all(isinstance(item, (ast.Pass, ast.Return, ast.Continue)) for item in handler.body)
                if broad and swallowed:
                    reasons.append("high: broad try/except swallows errors")
    if re.search(r"pytest\.warns\([^)]*,\s*match=", candidate_code) and not any(
        marker in issue for marker in ("exact warning", "warning message", "具体警告", "警告文本")
    ):
        reasons.append("medium: exact warning message asserted without explicit issue demand")
    not_crash = any(
        marker in issue
        for marker in (
            "not crash", "should not crash", "without crashing", "without error",
            "should not raise", "must not raise", "不崩溃", "不报错", "不应抛",
        )
    )
    exact_value = bool(re.search(r"assert\s+.+==\s+(['\"].{40,}['\"]|\[[^\]]{40,}\]|\{[^}]{40,}\})", candidate_code, re.S))
    if not_crash and exact_value:
        reasons.append("high: issue mainly asks for no crash but oracle requires exact value/string")
    log_tail = execution_log[-4000:]
    signals.update(
        {
            "assert_count": len(re.findall(r"\bassert\b", candidate_code)),
            "uses_pytest_raises": "pytest.raises" in candidate_code,
            "log_tail_chars": len(log_tail),
        }
    )
    deduped = list(dict.fromkeys(reasons))
    return {"level": _level(deduped), "reasons": deduped, "signals": signals}


def assess_surrogate_risk(
    dual: DualVersionResult | None,
    oracle_risk: dict[str, Any],
    retrieved_paths: set[str] | None = None,
) -> dict[str, Any]:
    if dual is None or not dual.surrogate_patch:
        return {"level": "low", "reasons": [], "signals": {}}
    reasons: list[str] = []
    retrieved_paths = retrieved_paths or set()
    patch = dual.surrogate_patch or {}
    applied_paths = [str(path) for path in patch.get("applied_paths") or []]
    if retrieved_paths and any(path not in retrieved_paths for path in applied_paths):
        reasons.append("high: surrogate patch applied outside retrieved/effective source context")
    if oracle_risk.get("level") == "high" and dual.status in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"}:
        reasons.append("high: surrogate pass is paired with high oracle risk")
    patch_reasons = " ".join(str(item.get("reason") or "") for item in patch.get("patches") or [] if isinstance(item, dict)).lower()
    if any(token in patch_reasons for token in ("test", "assert", "mock", "exact")):
        reasons.append("medium: surrogate rationale appears tailored to test internals")
    deduped = list(dict.fromkeys(reasons))
    return {
        "level": _level(deduped),
        "reasons": deduped,
        "signals": {"applied_paths": applied_paths, "status": dual.status},
    }
