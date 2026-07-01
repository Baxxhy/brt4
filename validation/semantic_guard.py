"""Static, repository-agnostic semantic checks for generated BRT files."""

from __future__ import annotations

import ast
import re

from ..core.schema import BehaviorTarget


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _same(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(
        right, include_attributes=False
    )


def _opposites(left: ast.AST, right: ast.AST) -> bool:
    if isinstance(left, ast.UnaryOp) and isinstance(left.op, ast.Not):
        return _same(left.operand, right)
    if isinstance(right, ast.UnaryOp) and isinstance(right.op, ast.Not):
        return _same(right.operand, left)
    if not isinstance(left, ast.Compare) or not isinstance(right, ast.Compare):
        return False
    if len(left.ops) != 1 or len(right.ops) != 1:
        return False
    if not (_same(left.left, right.left) and _same(left.comparators[0], right.comparators[0])):
        return False
    pairs = (
        (ast.Eq, ast.NotEq),
        (ast.Is, ast.IsNot),
        (ast.In, ast.NotIn),
        (ast.Lt, ast.GtE),
        (ast.LtE, ast.Gt),
    )
    return any(
        (isinstance(left.ops[0], a) and isinstance(right.ops[0], b))
        or (isinstance(left.ops[0], b) and isinstance(right.ops[0], a))
        for a, b in pairs
    )


def _expected_text(behavior: BehaviorTarget) -> str:
    return str(behavior.expected_behavior.get("text") or "").lower()


def audit_candidate(
    behavior: BehaviorTarget,
    code: str,
) -> str:
    """Return a repair instruction when the candidate is semantically unsafe."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "候选文件不是合法 Python，必须先修复语法。"

    test_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ]
    if len(test_nodes) != 1:
        return (
            f"完整 BRT 文件必须只有一个可收集测试入口，当前有 {len(test_nodes)} 个。"
            "保留最直接复现 Issue 的一个测试，删除 baseline、对照组和备用测试。"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_name = _name(node.func)
            if call_name in {
                "pytest.skip",
                "unittest.skip",
            } or call_name.endswith(".skip"):
                return (
                    "BRT 不得无条件调用 skip。缺少平台或依赖时应恢复真实"
                    "可执行上下文，不能把未执行的测试当作通过。"
                )
            if call_name in {"pytest.raises", "raises"} and node.args:
                if _name(node.args[0]) in {"Exception", "BaseException"}:
                    return "不得用宽泛 Exception/BaseException 作为 oracle；必须验证 Issue 指定的稳定行为。"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                name = _name(decorator.func) if isinstance(decorator, ast.Call) else _name(decorator)
                if name.lower().endswith(".skip") or name.lower() == "skip":
                    return "BRT 不得使用无条件 skip decorator；测试必须真实执行目标路径。"
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                return "不得使用 assert True 或其他占位 oracle。"
            if isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.Or):
                values = node.test.values
                if any(_opposites(a, b) for i, a in enumerate(values) for b in values[i + 1 :]):
                    return "检测到恒真的 A or not A / 相反比较断言；必须改为 expected_behavior 的可证伪 oracle。"
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                broad = handler.type is None or _name(handler.type) in {"Exception", "BaseException"}
                swallowed = all(isinstance(item, (ast.Pass, ast.Return, ast.Continue)) for item in handler.body)
                if broad and swallowed:
                    return "不得用 broad try/except 吞掉目标路径异常；只捕获 Issue 明确要求观察的异常。"

    expected = _expected_text(behavior)
    no_raise = any(
        marker in expected
        for marker in (
            "不应抛", "不再抛", "不应该抛", "不报错", "正常执行", "正常工作",
            "should not raise", "without raising", "without error", "must not raise",
        )
    )
    if no_raise and re.search(r"(?:pytest\.raises|assertRaises|\braises\s*\()", code):
        return (
            "expected_behavior 要求正常执行/不再抛异常，但候选用 raises 接受了 buggy 异常。"
            "应直接调用目标路径，并对修复后返回值、状态或输出建立正向断言。"
        )

    positive_capability = any(
        marker in expected
        for marker in (
            "应该支持", "应支持", "应该提供", "应提供", "应该存在", "应存在",
            "应该包含", "应包含", "应该显示", "应显示", "应该保留", "应保留",
            "should support", "should provide", "should exist", "should include",
            "should contain", "should display", "should preserve",
        )
    )
    negative_hasattr = re.search(
        r"(?:assert\s+not\s+hasattr\s*\(|assertFalse\s*\(\s*hasattr\s*\()",
        code,
    )
    if positive_capability and negative_hasattr:
        return (
            "expected_behavior 要求能力/属性存在，但候选断言 hasattr 为 False，"
            "这是把 buggy 的缺失行为当成正确结果。必须改成正向存在性和语义断言。"
        )
    return ""
