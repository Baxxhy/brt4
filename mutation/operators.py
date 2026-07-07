"""NS-GEM mutation operator applicability."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.schema import IssueGate, MutationOperator, TestSkeleton
from ..core.utils import safe_json_dump


OPERATOR_DEFS = [
    ("Mut_Boundary", "边界变异", "Construct None, empty, negative, large, dtype/shape boundary values.", {"empty", "none", "zero", "negative", "large", "boundary", "invalid", "missing", "dtype", "shape"}),
    ("Mut_NegatePredicateObject", "谓词对象取反变异", "Negate predicate objects or boolean/comparison expressions.", {"not", "negated", "exclude", "filter", "condition", "boolean", "comparison"}),
    ("Mut_Wrap", "包裹变异", "Wrap objects in nested/container/compound structures.", {"nested", "wrapper", "container", "compound", "polymorphic"}),
    ("Mut_InjectArg", "参数注入变异", "Inject missing keyword/configuration arguments into target calls.", {"keyword", "arg", "option", "mode", "axis", "output_field", "config"}),
    ("Mut_CallChain", "调用链变异", "Extend call chains to reach observable behavior.", {"render", "compile", "save", "load", "query", "values", "chain", "observable"}),
    ("Mut_StateLifecycle", "状态生命周期变异", "Change save/update/cache/config/session lifecycle.", {"state", "cache", "config", "save", "load", "init", "teardown", "session"}),
    ("Mut_Observe", "观测变异", "Insert public observation such as SQL/repr/return/warning/shape.", {"observe", "warning", "repr", "return", "sql", "shape", "public"}),
    ("Mut_OracleCompile", "断言编译变异", "Compile observations into assert/raises/warns.", {"assert", "raises", "warns", "expected", "oracle", "behavior"}),
]


def build_operator_applicability(
    instance_id: str,
    issue_gate: IssueGate,
    skeleton: TestSkeleton,
    output_dir: str,
) -> list[MutationOperator]:
    issue_text = " ".join(
        [
            issue_gate.trigger_condition,
            " ".join(issue_gate.expected_failure_signature),
            " ".join(issue_gate.observable_channels),
            " ".join(issue_gate.state_variables),
        ]
    ).lower()
    target_node = ""
    if skeleton.target_call_nodes:
        target_node = str(skeleton.target_call_nodes[0].get("id") or "")
    elif skeleton.mutable_slice:
        target_node = str(skeleton.mutable_slice[0].get("id") or "")
    operators: list[MutationOperator] = []
    for name, cn_name, description, keywords in OPERATOR_DEFS:
        applicable = bool(set(issue_text.split()) & keywords)
        if name in {"Mut_CallChain", "Mut_Observe", "Mut_OracleCompile"} and skeleton.parse_ok:
            applicable = applicable or bool(skeleton.target_call_nodes)
        operators.append(
            MutationOperator(
                name=name,
                cn_name=cn_name,
                description=description,
                applicable=applicable,
                target_node=target_node,
                reason="keyword/gate match" if applicable else "no matching gate signal",
                risk="low" if applicable and name in {"Mut_InjectArg", "Mut_Observe", "Mut_OracleCompile"} else "medium",
            )
        )
    if not any(item.applicable for item in operators):
        operators.append(
            MutationOperator(
                name="Mut_CallChain",
                cn_name="调用链变异",
                description="Fallback high-risk call-chain exploration.",
                applicable=True,
                target_node=target_node,
                reason="fallback when no operator is applicable",
                risk="high",
            )
        )
    path = Path(output_dir) / "operator_applicability.json"
    safe_json_dump([item.to_dict() for item in operators], str(path))
    return operators


def summarize_applicability(operators: list[MutationOperator]) -> dict[str, Any]:
    return {
        "applicable": [item.name for item in operators if item.applicable],
        "all": [item.to_dict() for item in operators],
    }
