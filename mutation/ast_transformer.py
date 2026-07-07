"""Safe local AST transformations for NS-GEM MVP."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ..core.schema import NSGEMOperatorPlan, TestSkeleton
from ..core.utils import safe_json_dump, write_text


class _Transformer(ast.NodeTransformer):
    def __init__(self, plan: NSGEMOperatorPlan, skeleton: TestSkeleton) -> None:
        self.plan = plan
        self.skeleton = skeleton
        self.applied = False
        self.reason = ""

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if self.applied:
            return node
        op = self.plan.selected_operator
        params = self.plan.operator_parameters or {}
        if op == "Mut_InjectArg":
            name = str(params.get("new_argument") or params.get("keyword") or "")
            value = str(params.get("replacement_expr") or params.get("value") or "True")
            if name and all(kw.arg != name for kw in node.keywords):
                try:
                    value_node = ast.parse(value, mode="eval").body
                except SyntaxError:
                    value_node = ast.Constant(value=value)
                node.keywords.append(ast.keyword(arg=name, value=value_node))
                self.applied = True
                self.reason = f"injected keyword {name}"
        elif op == "Mut_NegatePredicateObject":
            call_name = ast.unparse(node.func) if hasattr(ast, "unparse") else ""
            if call_name.endswith("Q") or call_name.endswith(".Q"):
                self.applied = True
                self.reason = "wrapped predicate call with unary invert"
                return ast.UnaryOp(op=ast.Invert(), operand=node)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if self.applied or self.plan.selected_operator != "Mut_Boundary":
            return node
        replacement = str((self.plan.operator_parameters or {}).get("replacement_expr") or "")
        if not replacement:
            return node
        if isinstance(node.value, (str, int, float, bool, type(None))):
            try:
                new_node = ast.parse(replacement, mode="eval").body
            except SyntaxError:
                return node
            self.applied = True
            self.reason = f"replaced constant with {replacement}"
            return new_node
        return node


def apply_ns_gem_ast_transform(
    candidate_code: str,
    plan: NSGEMOperatorPlan,
    skeleton: TestSkeleton,
    output_dir: str,
    enabled: bool = False,
) -> tuple[str, dict[str, Any]]:
    result = {
        "operator": plan.selected_operator,
        "applied": False,
        "fallback_reason": "",
        "reason": "",
        "ast_transform_default": "disabled",
        "ast_transform_rescue_used": bool(enabled),
        "ast_transform_allowed": False,
        "ast_transform_reject_reason": "",
        "ast_transform_rollback": False,
    }
    if not enabled:
        result["fallback_reason"] = "AST transform disabled by P2-safe default"
        result["ast_transform_reject_reason"] = result["fallback_reason"]
        safe_json_dump(result, str(Path(output_dir) / "ast_transform_result.json"))
        return candidate_code, result
    if not skeleton.parse_ok:
        result["fallback_reason"] = "seed skeleton parse failed"
        result["ast_transform_reject_reason"] = result["fallback_reason"]
        safe_json_dump(result, str(Path(output_dir) / "ast_transform_result.json"))
        return candidate_code, result
    if plan.selected_operator not in {"Mut_NegatePredicateObject", "Mut_InjectArg", "Mut_Boundary"}:
        result["fallback_reason"] = "operator is prompt-hint only in MVP"
        result["ast_transform_reject_reason"] = result["fallback_reason"]
        safe_json_dump(result, str(Path(output_dir) / "ast_transform_result.json"))
        return candidate_code, result
    if plan.selected_operator not in {"Mut_OracleCompile", "Mut_Observe", "Mut_NegatePredicateObject"}:
        result["fallback_reason"] = f"operator {plan.selected_operator} is disabled for P2-safe rescue"
        result["ast_transform_reject_reason"] = result["fallback_reason"]
        safe_json_dump(result, str(Path(output_dir) / "ast_transform_result.json"))
        return candidate_code, result
    result["ast_transform_allowed"] = True
    try:
        tree = ast.parse(candidate_code)
        transformer = _Transformer(plan, skeleton)
        new_tree = transformer.visit(tree)
        ast.fix_missing_locations(new_tree)
        new_code = ast.unparse(new_tree) + "\n"
        ast.parse(new_code)
    except Exception as exc:  # noqa: BLE001
        result["fallback_reason"] = f"AST transform failed: {exc}"
        safe_json_dump(result, str(Path(output_dir) / "ast_transform_result.json"))
        return candidate_code, result
    if not transformer.applied:
        result["fallback_reason"] = "no safe mutable node matched"
        safe_json_dump(result, str(Path(output_dir) / "ast_transform_result.json"))
        return candidate_code, result
    result.update({"applied": True, "reason": transformer.reason})
    write_text(str(Path(output_dir) / "transformed_candidate.py"), new_code)
    safe_json_dump(result, str(Path(output_dir) / "ast_transform_result.json"))
    return new_code, result
