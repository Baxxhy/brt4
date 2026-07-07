"""Lightweight AST skeletonization for seed tests."""

from __future__ import annotations

import ast
from typing import Any

from ..core.schema import HostContext, IssueGate, TestSkeleton


def _node_id(node: ast.AST) -> str:
    return f"{node.__class__.__name__}:{getattr(node, 'lineno', 0)}:{getattr(node, 'col_offset', 0)}"


def _segment(code: str, node: ast.AST, limit: int = 240) -> str:
    return (ast.get_source_segment(code, node) or node.__class__.__name__)[:limit]


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return ""


def _entry(code: str, node: ast.AST, role: str) -> dict[str, Any]:
    return {
        "id": _node_id(node),
        "role": role,
        "lineno": getattr(node, "lineno", 0),
        "end_lineno": getattr(node, "end_lineno", getattr(node, "lineno", 0)),
        "code": _segment(code, node),
    }


def build_test_skeleton(
    instance_id: str,
    seed_test_code: str,
    issue_gate: IssueGate,
    host: HostContext,
) -> TestSkeleton:
    del host
    try:
        tree = ast.parse(seed_test_code or "")
    except SyntaxError as exc:
        return TestSkeleton(
            instance_id=instance_id,
            parse_ok=False,
            reason=f"AST parse failed: {exc}",
        )
    target_terms = {
        part.lower()
        for api in issue_gate.target_apis
        for part in [api, api.rsplit(".", 1)[-1]]
        if part
    }
    setup_nodes: list[dict[str, Any]] = []
    action_nodes: list[dict[str, Any]] = []
    oracle_nodes: list[dict[str, Any]] = []
    target_call_nodes: list[dict[str, Any]] = []
    protected_nodes: list[dict[str, Any]] = []
    mutable_slice: list[dict[str, Any]] = []
    assignments: dict[str, list[dict[str, Any]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            protected_nodes.append(_entry(seed_test_code, node, "protected"))
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.With)):
            setup_nodes.append(_entry(seed_test_code, node, "setup"))
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(_entry(seed_test_code, node, "definition"))
        if isinstance(node, ast.Call):
            call_name = _call_name(node.func)
            entry = _entry(seed_test_code, node, "action")
            entry["call_name"] = call_name
            action_nodes.append(entry)
            if any(term and term in call_name.lower() for term in target_terms):
                target_call_nodes.append(entry)
                mutable_slice.append(entry)
                for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                    if isinstance(arg, ast.Name):
                        mutable_slice.extend(assignments.get(arg.id, []))
        if isinstance(node, ast.Assert):
            entry = _entry(seed_test_code, node, "oracle")
            oracle_nodes.append(entry)
            mutable_slice.append(entry)
        if isinstance(node, ast.Call) and _call_name(node.func).endswith(("raises", "warns")):
            entry = _entry(seed_test_code, node, "oracle")
            oracle_nodes.append(entry)
            mutable_slice.append(entry)
    if not target_call_nodes and action_nodes:
        mutable_slice.extend(action_nodes[:3])
    dedup_mutable = {item["id"]: item for item in mutable_slice}
    return TestSkeleton(
        instance_id=instance_id,
        setup_nodes=setup_nodes[:30],
        action_nodes=action_nodes[:40],
        oracle_nodes=oracle_nodes[:20],
        target_call_nodes=target_call_nodes[:20],
        def_use_chain=assignments,
        mutable_slice=list(dedup_mutable.values())[:40],
        protected_nodes=protected_nodes[:40],
        parse_ok=True,
        reason="parsed lightweight AST skeleton",
    )
