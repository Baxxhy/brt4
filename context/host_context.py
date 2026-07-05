"""Recover executable context from the top related test."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from ..execution.executor import run_command_in_conda
from ..retrieval.icore_runtime import icore_test_command
from ..io.io_utils import read_repo_file
from ..core.schema import BehaviorTarget, HostContext, RetrievedCode, RetrievedTest
from ..core.utils import truncate_text


def select_related_test(
    related_tests: list[RetrievedTest],
    behavior: BehaviorTarget,
) -> RetrievedTest | None:
    """Select one coherent seed instead of merging incompatible contexts."""
    if not related_tests:
        return None
    preferred_files = {
        str(item.get("test_file") or ""): index
        for index, item in enumerate(behavior.related_test_seeds)
        if item.get("test_file")
    }
    preferred_names = {
        str(item.get("test_name") or ""): index
        for index, item in enumerate(behavior.related_test_seeds)
        if item.get("test_name")
    }
    api_names = {
        str(item.get("name") or "").rsplit(".", 1)[-1]
        for item in behavior.target_apis
        if item.get("name")
    }

    def score(index_and_test: tuple[int, RetrievedTest]) -> tuple[int, int]:
        index, test = index_and_test
        value = 0
        if test.file in preferred_files:
            value += 100 - min(preferred_files[test.file], 20)
        if test.name in preferred_names:
            value += 80 - min(preferred_names[test.name], 20)
        value += 8 * sum(
            1 for name in api_names if name and name in test.code_content
        )
        if test.file and test.code_content:
            value += 2
        return value, -index

    return max(enumerate(related_tests), key=score)[1]


def rank_related_tests(
    related_tests: list[RetrievedTest], behavior: BehaviorTarget
) -> list[RetrievedTest]:
    """Return seeds in the same deterministic score order used by selection."""
    if not related_tests:
        return []
    preferred_files = {
        str(item.get("test_file") or ""): index
        for index, item in enumerate(behavior.related_test_seeds)
        if item.get("test_file")
    }
    preferred_names = {
        str(item.get("test_name") or ""): index
        for index, item in enumerate(behavior.related_test_seeds)
        if item.get("test_name")
    }
    api_names = {
        str(item.get("name") or "").rsplit(".", 1)[-1]
        for item in behavior.target_apis if item.get("name")
    }
    def value(pair: tuple[int, RetrievedTest]) -> tuple[int, int]:
        index, test = pair
        total = 0
        if test.file in preferred_files:
            total += 100 - min(preferred_files[test.file], 20)
        if test.name in preferred_names:
            total += 80 - min(preferred_names[test.name], 20)
        total += 8 * sum(1 for name in api_names if name and name in test.code_content)
        if test.file and test.code_content:
            total += 2
        return total, -index
    return [test for _, test in sorted(enumerate(related_tests), key=value, reverse=True)]


def _node_source(source: str, node: ast.AST) -> str:
    try:
        return ast.get_source_segment(source, node) or ""
    except Exception:  # noqa: BLE001
        return ""


def _decorators(source: str, node: ast.AST) -> list[str]:
    return [_node_source(source, d).strip() for d in getattr(node, "decorator_list", []) if _node_source(source, d).strip()]


def _extract_imports(source: str) -> str:
    lines = []
    try:
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                lines.append(_node_source(source, node))
            elif isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "pytestmark" for t in node.targets):
                lines.append(_node_source(source, node))
    except SyntaxError:
        for line in source.splitlines():
            if line.startswith(("import ", "from ")) or line.startswith("pytestmark"):
                lines.append(line)
    return "\n".join(x for x in lines if x)


def _imported_local_class_context(
    source: str,
    rel_file: str,
    buggy_repo: str,
    max_chars: int = 12000,
) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    host_dir = Path(rel_file).parent
    chunks: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level < 1:
            continue
        module = node.module or ""
        if not (module == "models" or module.endswith(".models")):
            continue
        base_dir = host_dir
        for _ in range(max(0, node.level - 1)):
            base_dir = base_dir.parent
        module_path = Path(*module.split(".")) if module else Path()
        source_path = Path(buggy_repo) / base_dir / module_path
        if source_path.suffix != ".py":
            source_path = source_path.with_suffix(".py")
        if not source_path.is_file():
            continue
        model_source = source_path.read_text(encoding="utf-8", errors="replace")
        try:
            model_tree = ast.parse(model_source)
        except SyntaxError:
            continue
        wanted = {alias.name for alias in node.names if alias.name != "*"}
        for model_node in model_tree.body:
            if isinstance(model_node, ast.ClassDef) and (
                not wanted or model_node.name in wanted
            ):
                class_source = _node_source(model_source, model_node)
                if class_source:
                    chunks.append(
                        f"# {source_path.relative_to(Path(buggy_repo))}\n"
                        f"{class_source}"
                    )
    return truncate_text("\n\n".join(chunks), max_chars)


def _find_test(source: str, test_name: str) -> tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef | None, ast.ClassDef | None]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "", "", None, None
    wanted_class = ""
    wanted_name = test_name
    if "." in test_name:
        wanted_class, wanted_name = test_name.rsplit(".", 1)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if wanted_class and node.name != wanted_class:
                continue
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == wanted_name:
                    return _node_source(source, child), node.name, child, node
        if not wanted_class and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == wanted_name:
            return _node_source(source, node), "", node, None
    return "", "", None, None


def _class_setup(source: str, cls: ast.ClassDef | None) -> str:
    if not cls:
        return ""
    pieces = []
    bases = ", ".join(_node_source(source, b) for b in cls.bases)
    pieces.append(f"class {cls.name}({bases}):")
    for child in cls.body:
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            assignment = _node_source(source, child)
            if assignment:
                pieces.append(assignment)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name in {"setUp", "tearDown", "setup_method", "teardown_method", "setUpClass", "tearDownClass"}:
            pieces.append(_node_source(source, child))
    return "\n\n".join(pieces)


def _adjacent_tests(source: str, target_name: str, limit: int = 2) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    funcs: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            funcs.append(node)
    funcs.sort(key=lambda n: getattr(n, "lineno", 0))
    names = [getattr(n, "name", "") for n in funcs]
    if target_name not in names:
        return []
    idx = names.index(target_name)
    selected = funcs[max(0, idx - limit) : idx] + funcs[idx + 1 : idx + 1 + limit]
    return [_node_source(source, n) for n in selected if _node_source(source, n)]


def build_host_context(
    instance_id: str,
    related_test: RetrievedTest | None,
    buggy_repo: str,
    behavior: BehaviorTarget,
    related_source: list[RetrievedCode] | None = None,
    conda_env: str = "",
    timeout: int = 120,
    no_conda: bool = False,
    skip_execution: bool = False,
    repo: str = "",
    version: str = "",
) -> HostContext:
    warnings: list[str] = []
    if not related_test:
        return HostContext(instance_id=instance_id, warnings=["no related test available"])
    rel_file = related_test.file
    full_source = read_repo_file(buggy_repo, rel_file)
    if not full_source:
        full_source = related_test.code_content
        warnings.append("full related test file not found; using retrieved code_content")
    raw_seed_name = related_test.name or _guess_test_name(related_test.code_content)
    seed_name = raw_seed_name.rsplit(".", 1)[-1] if "." in raw_seed_name else raw_seed_name
    seed_code, host_class, func_node, cls_node = _find_test(full_source, raw_seed_name)
    if not seed_code:
        seed_code = related_test.code_content
        warnings.append("seed test function not found in full file")
    fixtures = []
    if func_node:
        fixtures = [arg.arg for arg in func_node.args.args if arg.arg != "self"]
    decorators = _decorators(full_source, func_node) if func_node else []
    imports = _extract_imports(full_source)
    setup = _class_setup(full_source, cls_node)
    model_context = _imported_local_class_context(
        full_source, rel_file, buggy_repo
    )
    if host_class and seed_name:
        selector = f"{host_class}::{seed_name}"
    elif seed_name:
        selector = seed_name
    else:
        selector = ""
    command = (
        icore_test_command(repo, version, rel_file, selector)
        if repo
        else f"python -m pytest {rel_file} -q"
    )
    strategy = "same_dir_new_file"
    if skip_execution:
        execution_dict = {"status": "SKIPPED", "command": command}
        seed_status = "SKIPPED"
    else:
        execution = run_command_in_conda(command, buggy_repo, conda_env, timeout, no_conda, behavior, instance_id)
        execution_dict = execution.to_dict()
        seed_status = execution.status
        if execution.status not in {"PASS", "ISSUE_ALIGNED_FAIL", "ASSERTION_FAIL"}:
            warnings.append("seed test did not execute cleanly; still placing BRT as a new same-directory file")
    return HostContext(
        instance_id=instance_id,
        host_file=rel_file,
        host_class=host_class,
        seed_test_name=seed_name,
        seed_test_code=seed_code,
        imports=imports,
        setup_context=truncate_text(setup, 8000),
        model_context=model_context,
        fixtures=fixtures,
        decorators=decorators,
        pytestmark="\n".join(x for x in imports.splitlines() if x.startswith("pytestmark")),
        test_command=command,
        seed_execution_status=seed_status,
        seed_execution=execution_dict,
        insert_strategy=strategy,
        insert_location_hint="always create a new test_brt_<instance_id>.py file in the same directory as the primary related test; do not insert into the seed file",
        adjacent_tests=_adjacent_tests(full_source, seed_name),
        full_test_file_path=str(Path(buggy_repo) / rel_file),
        warnings=warnings,
    )


def _guess_test_name(code: str) -> str:
    m = re.search(r"def\s+(test_[A-Za-z0-9_]+)\s*\(", code or "")
    return m.group(1) if m else ""
