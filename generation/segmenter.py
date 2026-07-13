"""AST segmentation for protocol-preserving BRT transformations."""

from __future__ import annotations

import ast
import copy
import hashlib
from typing import Any, Iterable

from ..core.schema import BehaviorTarget, TestSegments


ASSERT_METHOD_MARKERS = (
    "assert",
    "fail",
    "raises",
    "warns",
    "assertraises",
    "assertwarns",
    "assertlogs",
)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return ""


def target_names(behavior: BehaviorTarget) -> set[str]:
    names: set[str] = set()
    raw_targets: list[Any] = list(behavior.target_apis or [])
    contract_targets = (behavior.trigger_contract or {}).get("target_apis") or []
    raw_targets.extend(contract_targets if isinstance(contract_targets, list) else [])
    for item in raw_targets:
        value = item.get("name") if isinstance(item, dict) else item
        text = str(value or "").strip()
        if text:
            names.add(text)
            names.add(text.rsplit(".", 1)[-1])
    for item in behavior.trace_targets or []:
        if not isinstance(item, dict):
            continue
        for key in ("function_name", "class_name"):
            text = str(item.get(key) or "").strip()
            if text:
                names.add(text)
                names.add(text.rsplit(".", 1)[-1])
    for item in (behavior.localization_contract or {}).get("trace_targets") or []:
        if isinstance(item, dict):
            text = str(item.get("function_name") or "").strip()
            if text:
                names.add(text)
                names.add(text.rsplit(".", 1)[-1])
    return names


def _matches_target(call: ast.Call, names: set[str]) -> bool:
    name = _call_name(call.func)
    short = name.rsplit(".", 1)[-1]
    return bool(name and (name in names or short in names))


def _target_calls(node: ast.AST, names: set[str]) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _matches_target(child, names)
    ]


def _is_assertion_call(call: ast.Call) -> bool:
    name = _call_name(call.func).lower().replace("_", "")
    return any(marker in name for marker in ASSERT_METHOD_MARKERS)


def _is_oracle_statement(node: ast.stmt) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            context_name = _call_name(item.context_expr).lower().replace("_", "")
            if any(marker in context_name for marker in ASSERT_METHOD_MARKERS):
                return True
    return any(
        isinstance(child, ast.Call) and _is_assertion_call(child)
        for child in ast.walk(node)
    )


def _node_dump(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _hash_parts(parts: Iterable[str]) -> str:
    material = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _node_record(
    code: str,
    node: ast.AST,
    role: str,
    normalized_node: ast.AST | None = None,
) -> dict[str, Any]:
    return {
        "role": role,
        "node_type": type(node).__name__,
        "lineno": int(getattr(node, "lineno", 0) or 0),
        "end_lineno": int(getattr(node, "end_lineno", 0) or 0),
        "source": ast.get_source_segment(code, node) or "",
        "ast": _node_dump(normalized_node or node),
    }


class _TargetPlaceholder(ast.NodeTransformer):
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if _matches_target(node, self.names):
            return ast.copy_location(
                ast.Name(id="__BRT_TARGET_CALL__", ctx=ast.Load()), node
            )
        return self.generic_visit(node)


def _oracle_components(statement: ast.stmt) -> list[ast.AST]:
    if isinstance(statement, ast.Assert):
        return [statement]
    components: list[ast.AST] = []
    seen: set[int] = set()
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        for item in statement.items:
            if isinstance(item.context_expr, ast.Call) and _is_assertion_call(
                item.context_expr
            ):
                components.append(item.context_expr)
                seen.add(id(item.context_expr))
    for child in ast.walk(statement):
        if (
            isinstance(child, ast.Call)
            and _is_assertion_call(child)
            and id(child) not in seen
        ):
            components.append(child)
            seen.add(id(child))
    return components or [statement]


def _non_assertion_calls(node: ast.AST) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and not _is_assertion_call(child)
    ]


def _test_functions(tree: ast.Module) -> list[tuple[ast.AST | None, ast.AST]]:
    found: list[tuple[ast.AST | None, ast.AST]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            found.append((None, node))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                    found.append((node, child))
    return found


def _assigned_names(statement: ast.stmt) -> list[str]:
    targets: list[ast.AST] = []
    if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        if isinstance(statement, ast.Assign):
            targets.extend(statement.targets)
        else:
            targets.append(statement.target)
    elif isinstance(statement, (ast.With, ast.AsyncWith)):
        targets.extend(
            item.optional_vars for item in statement.items if item.optional_vars is not None
        )
    names: list[str] = []
    for target in targets:
        names.extend(
            node.id for node in ast.walk(target) if isinstance(node, ast.Name)
        )
    return list(dict.fromkeys(names))


def segment_test(
    code: str,
    behavior: BehaviorTarget,
    instance_id: str = "",
) -> TestSegments:
    """Split a candidate into scaffold, trigger and oracle segments.

    The result is intentionally conservative. Ambiguous statements are assigned
    to trigger only after the first target call; statements before that remain
    scaffold so search branches cannot casually rewrite protocol setup.
    """

    result = TestSegments(instance_id=instance_id or behavior.instance_id)
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        result.parse_error = f"{exc.msg}:{exc.lineno}:{exc.offset}"
        return result

    names = target_names(behavior)
    tests = _test_functions(tree)
    result.test_entry_count = len(tests)
    scaffold_nodes: list[dict[str, Any]] = []
    trigger_nodes: list[dict[str, Any]] = []
    oracle_nodes: list[dict[str, Any]] = []

    for top in tree.body:
        if isinstance(top, (ast.Import, ast.ImportFrom)):
            scaffold_nodes.append(_node_record(code, top, "import"))
        elif isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) and not top.name.startswith("test"):
            scaffold_nodes.append(_node_record(code, top, "helper"))
        elif isinstance(top, ast.ClassDef):
            wrapper = ast.ClassDef(
                name=top.name,
                bases=top.bases,
                keywords=top.keywords,
                body=[],
                decorator_list=top.decorator_list,
            )
            scaffold_nodes.append(_node_record(code, wrapper, "class_wrapper"))
        elif not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scaffold_nodes.append(_node_record(code, top, "module_setup"))

    target_locations: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for class_node, test in tests:
        signature = {
            "role": "test_protocol",
            "node_type": type(test).__name__,
            "name": test.name,
            "args": ast.dump(test.args, include_attributes=False),
            "decorators": [ast.dump(item, include_attributes=False) for item in test.decorator_list],
            "class_name": getattr(class_node, "name", ""),
        }
        scaffold_nodes.append(signature)
        statement_info = [
            {
                "statement": statement,
                "target_calls": _target_calls(statement, names),
                "is_oracle": _is_oracle_statement(statement),
                "non_assertion_calls": _non_assertion_calls(statement),
            }
            for statement in test.body
        ]
        direct_indexes = [
            index for index, item in enumerate(statement_info) if item["target_calls"]
        ]
        inferred_trigger_index: int | None = None
        if not direct_indexes:
            first_oracle_index = next(
                (
                    index
                    for index, item in enumerate(statement_info)
                    if item["is_oracle"]
                ),
                len(statement_info),
            )
            inferred_candidates = [
                index
                for index, item in enumerate(statement_info[:first_oracle_index])
                if item["non_assertion_calls"]
            ]
            if inferred_candidates:
                inferred_trigger_index = inferred_candidates[-1]
        first_trigger_index = (
            direct_indexes[0]
            if direct_indexes
            else inferred_trigger_index
        )
        for index, item in enumerate(statement_info):
            statement = item["statement"]
            calls = item["target_calls"]
            is_oracle = bool(item["is_oracle"])
            inferred_calls = (
                item["non_assertion_calls"]
                if inferred_trigger_index == index and not calls
                else []
            )
            observation_calls = calls or inferred_calls
            if calls:
                for call in calls:
                    call_name = _call_name(call.func)
                    target_locations.append(
                        {
                            "name": call_name,
                            "lineno": int(getattr(call, "lineno", 0) or 0),
                            "end_lineno": int(getattr(call, "end_lineno", 0) or 0),
                            "expression": ast.get_source_segment(code, call) or "",
                            "test_name": test.name,
                        }
                    )
            if observation_calls:
                assigned = _assigned_names(statement)
                observations.append(
                    {
                        "test_name": test.name,
                        "statement_lineno": int(getattr(statement, "lineno", 0) or 0),
                        "statement_end_lineno": int(getattr(statement, "end_lineno", 0) or 0),
                        "assigned_names": assigned,
                        "preferred_name": assigned[0] if assigned else "",
                        "target_expressions": [
                            ast.get_source_segment(code, call) or ""
                            for call in observation_calls
                        ],
                        "direct_in_oracle": is_oracle,
                        "inferred_target": not bool(calls),
                    }
                )
            if is_oracle:
                for component in _oracle_components(statement):
                    normalized = copy.deepcopy(component)
                    normalized = _TargetPlaceholder(names).visit(normalized)
                    ast.fix_missing_locations(normalized)
                    oracle_nodes.append(
                        _node_record(
                            code,
                            component,
                            "oracle",
                            normalized_node=normalized,
                        )
                    )
            if calls and is_oracle:
                trigger_nodes.extend(
                    _node_record(code, call, "trigger") for call in calls
                )
            elif not is_oracle and first_trigger_index is not None and index >= first_trigger_index:
                trigger_nodes.append(_node_record(code, statement, "trigger"))
            elif not is_oracle:
                scaffold_nodes.append(_node_record(code, statement, "setup"))

    result.scaffold_nodes = scaffold_nodes
    result.trigger_nodes = trigger_nodes
    result.oracle_nodes = oracle_nodes
    result.target_call_locations = target_locations
    result.observation_candidates = observations
    result.scaffold_hash = _hash_parts(str(item.get("ast") or item) for item in scaffold_nodes)
    result.trigger_hash = _hash_parts(str(item.get("ast") or item) for item in trigger_nodes)
    result.oracle_hash = _hash_parts(str(item.get("ast") or item) for item in oracle_nodes)
    result.segment_confidence = {
        "oracle": 1.0 if oracle_nodes else 0.45,
        "scaffold": 0.95 if len(tests) == 1 else (0.7 if tests else 0.2),
        "trigger": (
            0.95
            if target_locations and names
            else 0.55
            if trigger_nodes
            else 0.2
        ),
    }
    return result


def preservation_report(
    before: TestSegments,
    after: TestSegments,
    mutable_segment: str,
) -> dict[str, Any]:
    hashes = {
        "scaffold": (before.scaffold_hash, after.scaffold_hash),
        "trigger": (before.trigger_hash, after.trigger_hash),
        "oracle": (before.oracle_hash, after.oracle_hash),
    }
    changed = [name for name, values in hashes.items() if values[0] != values[1]]
    frozen_changed = [name for name in changed if name != mutable_segment]
    if after.target_call_locations:
        target_preservation = "true"
    elif not before.target_call_locations and after.trigger_nodes:
        target_preservation = "unknown"
    else:
        target_preservation = "false"
    return {
        "valid": not before.parse_error
        and not after.parse_error
        and before.test_entry_count == after.test_entry_count == 1
        and not frozen_changed,
        "mutable_segment": mutable_segment,
        "changed_segments": changed,
        "frozen_segments_changed": frozen_changed,
        "target_api_preserved": target_preservation != "false",
        "target_api_preservation": target_preservation,
        "before": {
            "scaffold_hash": before.scaffold_hash,
            "trigger_hash": before.trigger_hash,
            "oracle_hash": before.oracle_hash,
        },
        "after": {
            "scaffold_hash": after.scaffold_hash,
            "trigger_hash": after.trigger_hash,
            "oracle_hash": after.oracle_hash,
        },
    }


def _first_test_function(
    tree: ast.Module,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    tests = _test_functions(tree)
    if len(tests) != 1:
        return None
    return tests[0][1]


def _statement_lines(segments: TestSegments, role: str) -> set[int]:
    records = getattr(segments, f"{role}_nodes")
    accepted_roles = {role}
    if role == "scaffold":
        accepted_roles = {"setup"}
    return {
        int(item.get("lineno") or 0)
        for item in records
        if item.get("role") in accepted_roles and int(item.get("lineno") or 0) > 0
    }


def _statements_for_segment(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    segments: TestSegments,
    role: str,
) -> list[ast.stmt]:
    lines = _statement_lines(segments, role)
    return [
        copy.deepcopy(statement)
        for statement in function.body
        if any(
            int(getattr(statement, "lineno", 0) or 0)
            <= line
            <= int(
                getattr(statement, "end_lineno", 0)
                or getattr(statement, "lineno", 0)
                or 0
            )
            for line in lines
        )
    ]


def _top_level_statement_lines(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    segments: TestSegments,
    role: str,
) -> set[int]:
    record_lines = _statement_lines(segments, role)
    return {
        int(getattr(statement, "lineno", 0) or 0)
        for statement in function.body
        if any(
            int(getattr(statement, "lineno", 0) or 0)
            <= line
            <= int(
                getattr(statement, "end_lineno", 0)
                or getattr(statement, "lineno", 0)
                or 0
            )
            for line in record_lines
        )
    }


def _behavior_statements(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    segments: TestSegments,
) -> list[ast.stmt]:
    lines = _top_level_statement_lines(
        function, segments, "trigger"
    ) | _top_level_statement_lines(function, segments, "oracle")
    return [
        copy.deepcopy(statement)
        for statement in function.body
        if int(getattr(statement, "lineno", 0) or 0) in lines
    ]


class _TargetCallReplacer(ast.NodeTransformer):
    def __init__(self, names: set[str], replacements: list[ast.Call]) -> None:
        self.names = names
        self.replacements = replacements
        self.index = 0

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if _matches_target(node, self.names) and self.index < len(self.replacements):
            replacement = copy.deepcopy(self.replacements[self.index])
            self.index += 1
            return ast.copy_location(replacement, node)
        return self.generic_visit(node)


def _replace_target_calls(
    tree: ast.Module,
    names: set[str],
    replacements: list[ast.Call],
) -> tuple[ast.Module, int]:
    replacer = _TargetCallReplacer(names, replacements)
    updated = replacer.visit(tree)
    assert isinstance(updated, ast.Module)
    return updated, replacer.index


def _oracle_marker(code: str) -> str:
    return next(
        (
            line.strip()
            for line in code.splitlines()
            if line.strip().startswith("# BRT_ORACLE_TYPE:")
        ),
        "",
    )


def transplant_typed_segment(
    before_code: str,
    proposed_code: str,
    behavior: BehaviorTarget,
    action: str,
) -> tuple[str | None, dict[str, Any]]:
    """Apply only the segment that a typed-search action is allowed to change.

    LLM repairs return complete files and can incidentally rewrite formatting,
    imports, or assertions. This function treats that output as a proposal: it
    extracts the permitted statement segment, installs it into an AST copied
    from the appropriate source file, then leaves the normal preservation
    verifier to check the result.
    """

    mutable = {
        "protocol_repair": "scaffold",
        "trigger_search": "trigger",
        "minimal_oracle_search": "oracle",
    }.get(action)
    metadata: dict[str, Any] = {
        "status": "ABSTAIN",
        "action": action,
        "mutable_segment": mutable or "",
        "reason": "",
    }
    if mutable is None:
        metadata["reason"] = "unsupported typed-search action"
        return None, metadata

    try:
        before_tree = ast.parse(before_code)
        proposed_tree = ast.parse(proposed_code)
    except SyntaxError as exc:
        metadata["reason"] = f"proposal parse error: {exc.msg}:{exc.lineno}:{exc.offset}"
        return None, metadata

    before_segments = segment_test(before_code, behavior)
    proposed_segments = segment_test(proposed_code, behavior)
    names = target_names(behavior)
    original_target_calls = [
        copy.deepcopy(call) for call in _target_calls(before_tree, names)
    ]
    before_test = _first_test_function(before_tree)
    proposed_test = _first_test_function(proposed_tree)
    if before_test is None or proposed_test is None:
        metadata["reason"] = "typed transplant requires exactly one test entry in both files"
        return None, metadata

    before_parts = {
        role: _statements_for_segment(before_test, before_segments, role)
        for role in ("scaffold", "trigger", "oracle")
    }
    proposed_parts = {
        role: _statements_for_segment(proposed_test, proposed_segments, role)
        for role in ("scaffold", "trigger", "oracle")
    }
    if (
        mutable != "scaffold"
        and not proposed_parts[mutable]
        and not (
            mutable == "trigger" and proposed_segments.target_call_locations
        )
    ):
        metadata["reason"] = f"proposal has no identifiable {mutable} statements"
        return None, metadata
    if mutable != "trigger" and not before_parts["trigger"]:
        metadata["reason"] = "original trigger segment is empty or overlaps the oracle"
        return None, metadata

    # Trigger and oracle search retain the original module, wrapper, imports,
    # fixtures, decorators, and test signature. Protocol repair uses the
    # proposal as its scaffold source while restoring the original behavior.
    before_overlap = _top_level_statement_lines(
        before_test, before_segments, "trigger"
    ) & _top_level_statement_lines(before_test, before_segments, "oracle")
    proposed_overlap = _top_level_statement_lines(
        proposed_test, proposed_segments, "trigger"
    ) & _top_level_statement_lines(proposed_test, proposed_segments, "oracle")
    strategy = "statement_segment"
    if mutable == "trigger" and before_overlap:
        replacements = _target_calls(proposed_tree, names)
        if not replacements:
            metadata["reason"] = "overlapping trigger/oracle proposal has no target call"
            return None, metadata
        output_tree, replaced = _replace_target_calls(
            before_tree, names, replacements
        )
        if replaced == 0:
            metadata["reason"] = "could not replace target call inside overlapping oracle"
            return None, metadata
        strategy = "target_call_only"
    else:
        output_tree = proposed_tree if mutable == "scaffold" else before_tree
    output_test = _first_test_function(output_tree)
    if output_test is None:
        metadata["reason"] = "output scaffold does not contain one test entry"
        return None, metadata

    if strategy == "statement_segment":
        if mutable == "scaffold":
            output_test.body = proposed_parts["scaffold"] + _behavior_statements(
                before_test, before_segments
            )
        else:
            chosen = dict(before_parts)
            chosen[mutable] = proposed_parts[mutable]
            if mutable == "oracle" and (before_overlap or proposed_overlap):
                overlap_lines = _top_level_statement_lines(
                    before_test, before_segments, "oracle"
                )
                preserved_trigger = [
                    statement
                    for statement in before_parts["trigger"]
                    if int(getattr(statement, "lineno", 0) or 0)
                    not in overlap_lines
                ]
                output_test.body = (
                    before_parts["scaffold"]
                    + preserved_trigger
                    + proposed_parts["oracle"]
                )
                if original_target_calls:
                    output_tree, _ = _replace_target_calls(
                        output_tree, names, original_target_calls
                    )
                    output_test = _first_test_function(output_tree)
                    if output_test is None:
                        metadata["reason"] = "oracle transplant lost the test entry"
                        return None, metadata
                strategy = "oracle_with_original_target_calls"
            else:
                output_test.body = (
                    chosen["scaffold"] + chosen["trigger"] + chosen["oracle"]
                )
    if not output_test.body:
        metadata["reason"] = "transplanted test body is empty"
        return None, metadata

    try:
        ast.fix_missing_locations(output_tree)
        transplanted = ast.unparse(output_tree).strip() + "\n"
        marker = _oracle_marker(proposed_code if mutable == "oracle" else before_code)
        if marker:
            transplanted = marker + "\n" + transplanted
        compile(transplanted, "<ats-brt-typed-transplant>", "exec")
    except (SyntaxError, ValueError) as exc:
        metadata["reason"] = f"transplanted code is invalid: {exc}"
        return None, metadata

    transplanted_segments = segment_test(transplanted, behavior)
    metadata.update(
        {
            "status": "CREATED",
            "reason": "only the permitted AST segment was transplanted",
            "strategy": strategy,
            "proposal_changed_segments": preservation_report(
                before_segments, proposed_segments, mutable
            ).get("changed_segments", []),
            "transplanted_segment_hashes": {
                "scaffold": transplanted_segments.scaffold_hash,
                "trigger": transplanted_segments.trigger_hash,
                "oracle": transplanted_segments.oracle_hash,
            },
        }
    )
    return transplanted, metadata
