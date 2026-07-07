"""Build an issue trigger gate from existing non-golden inputs."""

from __future__ import annotations

import re
from typing import Any

from ..core.schema import BehaviorTarget, IssueGate, RetrievedCode, RetrievedTest


FORBIDDEN_FAILURES = [
    "ImportError",
    "ModuleNotFoundError",
    "fixture not found",
    "collect error",
    "syntax error",
    "unrelated assertion",
]


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value)
    return str(value or "")


def _api_name(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("qualified_name") or item.get("name") or item.get("api") or "").strip()
    return str(item or "").strip()


def _tokens(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[A-Za-z_][A-Za-z0-9_\.]{2,}", text or "")))[:12]


def build_issue_gate(
    behavior: BehaviorTarget,
    retrieved_code: list[RetrievedCode],
    retrieved_tests: list[RetrievedTest],
) -> IssueGate:
    target_apis = [name for name in (_api_name(item) for item in behavior.target_apis) if name]
    trigger = _text(behavior.trigger_condition)
    expected = _text(behavior.expected_behavior)
    symptom = _text(behavior.error_symptom)
    observable_channels = [
        _text(item)
        for item in [*behavior.observation_points, *behavior.assertion_hints]
        if _text(item)
    ][:12]
    if not observable_channels:
        observable_channels = ["return value", "exception", "warning", "public output"]
    state_text = " ".join([trigger, expected, symptom, behavior.issue_summary])
    state_variables = [
        token for token in _tokens(state_text)
        if token.lower() not in {"the", "and", "for", "with", "without", "should"}
    ][:10]
    code_evidence = [
        f"{item.path}:{item.obj_name}".strip(":")
        for item in retrieved_code[:8]
        if item.path or item.obj_name
    ]
    seed_evidence = [
        f"{item.file}:{item.name}".strip(":")
        for item in retrieved_tests[:8]
        if item.file or item.name
    ]
    uncertainties = list(behavior.uncertainties)
    if not target_apis:
        uncertainties.append("no explicit target API in BehaviorTarget")
    return IssueGate(
        instance_id=behavior.instance_id,
        target_apis=target_apis,
        trigger_condition=trigger,
        state_variables=state_variables,
        observable_channels=list(dict.fromkeys(observable_channels)),
        expected_failure_signature=[item for item in [symptom, expected] if item],
        forbidden_failures=FORBIDDEN_FAILURES,
        source_evidence={
            "from_behavior_target": [
                behavior.issue_summary,
                trigger,
                expected,
                symptom,
            ],
            "from_retrieved_code": code_evidence,
            "from_seed": seed_evidence,
        },
        uncertainties=list(dict.fromkeys(uncertainties)),
    )
