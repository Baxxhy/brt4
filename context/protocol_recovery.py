"""Recover one coherent test protocol from one retrieved seed test."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from ..runtime.icore_runtime import icore_test_command
from ..core.prompts import PROTOCOL_RECOVERY_SYSTEM_PROMPT, PROTOCOL_RECOVERY_USER_PROMPT
from ..core.schema import BehaviorTarget, ProtocolRecovery, RetrievedCode, RetrievedTest
from ..core.utils import extract_json_object, truncate_text, write_text


_SETUP_NAMES = {"setUp", "setUpClass", "setup_method", "setup_class"}
_TEARDOWN_NAMES = {"tearDown", "tearDownClass", "teardown_method", "teardown_class"}
_LOCAL_FILES = ("models.py", "helpers.py", "utils.py")


def _source(node: ast.AST, text: str) -> str:
    return ast.get_source_segment(text, node) or ""


def _target_test(tree: ast.Module, name: str) -> tuple[ast.AST | None, ast.ClassDef | None]:
    class_name, _, function_name = name.rpartition(".")
    if not function_name:
        function_name = class_name
        class_name = ""
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if class_name and node.name != class_name:
                continue
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == function_name:
                    return child, node
        if not class_name and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return node, None
    return None, None


def _framework(source: str, cls: ast.ClassDef | None) -> str:
    bases = " ".join(_source(base, source) for base in cls.bases) if cls else ""
    if "django.test" in source or re.search(r"\b(?:TestCase|TransactionTestCase|SimpleTestCase)\b", bases):
        return "django"
    if "unittest" in source or "TestCase" in bases:
        return "unittest"
    if "pytest" in source or re.search(r"\bpytestmark\b", source):
        return "pytest"
    return "pytest" if "def test" in source else "unknown"


def _fixture_definitions(conftest: Path, wanted: set[str]) -> list[dict[str, str]]:
    text = conftest.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    found: list[dict[str, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = [_source(item, text) for item in node.decorator_list]
        fixture = any("fixture" in value for value in decorators)
        if fixture and (not wanted or node.name in wanted):
            found.append({"name": node.name, "file": str(conftest), "code": truncate_text(_source(node, text), 5000)})
    return found


def _local_symbols(directory: Path, referenced: set[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    helpers: list[dict[str, str]] = []
    models: list[dict[str, str]] = []
    for filename in _LOCAL_FILES:
        path = directory / filename
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in referenced:
                continue
            item = {"name": node.name, "file": str(path), "code": truncate_text(_source(node, text), 6000)}
            (models if filename == "models.py" and isinstance(node, ast.ClassDef) else helpers).append(item)
    return helpers, models


def recover_test_protocol(
    instance_id: str,
    related_test: RetrievedTest,
    buggy_worktree: str,
    behavior: BehaviorTarget,
    related_source: list[RetrievedCode],
    repo: str,
    version: str,
) -> ProtocolRecovery:
    del behavior, related_source  # Kept in the interface for future evidence-aware extraction.
    test_path = Path(buggy_worktree) / related_test.file
    risks: list[str] = []
    source = test_path.read_text(encoding="utf-8", errors="replace") if test_path.is_file() else related_test.code_content
    if not test_path.is_file():
        risks.append("完整测试文件不存在，协议恢复仅使用检索片段。")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = ast.Module(body=[], type_ignores=[])
        risks.append("相关测试源码无法通过 AST 解析。")
    target, cls = _target_test(tree, related_test.name)
    if target is None:
        risks.append("无法在完整文件中定位目标测试入口。")
    imports = [_source(node, source) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    marks = [
        _source(node, source)
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and "pytestmark" in _source(node, source)
    ]
    decorators = [_source(node, source) for node in getattr(target, "decorator_list", [])]
    fixtures = [arg.arg for arg in getattr(getattr(target, "args", None), "args", []) if arg.arg not in {"self", "cls"}]
    setup_methods: list[str] = []
    teardown_methods: list[str] = []
    class_context = ""
    if cls:
        bases = ", ".join(_source(base, source) for base in cls.bases)
        class_context = f"class {cls.name}({bases}):"
        for child in cls.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name in _SETUP_NAMES:
                setup_methods.append(_source(child, source))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name in _TEARDOWN_NAMES:
                teardown_methods.append(_source(child, source))
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                class_context += "\n" + _source(child, source)
    referenced = {node.id for node in ast.walk(target) if isinstance(node, ast.Name)} if target else set()
    directory = test_path.parent
    helpers, models = _local_symbols(directory, referenced)
    conftests: list[dict[str, object]] = []
    cursor = directory
    root = Path(buggy_worktree).resolve()
    while cursor.resolve().is_relative_to(root):
        conftest = cursor / "conftest.py"
        if conftest.is_file():
            definitions = _fixture_definitions(conftest, set(fixtures))
            if definitions:
                conftests.append({"file": str(conftest.relative_to(root)), "fixtures": definitions})
        if cursor.resolve() == root:
            break
        cursor = cursor.parent
    selector = related_test.name.replace(".", "::")
    command = icore_test_command(repo, version, related_test.file, selector)
    framework = _framework(source, cls)
    if fixtures and not conftests and framework == "pytest":
        risks.append("测试使用 fixture，但父目录 conftest 中未定位到对应定义；fixture 可能来自插件。")
    return ProtocolRecovery(
        instance_id=instance_id,
        test_file=related_test.file,
        test_framework=framework,
        test_command=command,
        imports=[item for item in imports if item],
        fixtures=fixtures,
        pytest_marks=[item for item in marks if item],
        decorators=[item for item in decorators if item],
        class_context=truncate_text(class_context, 6000),
        setup_methods=[truncate_text(item, 6000) for item in setup_methods],
        teardown_methods=[truncate_text(item, 6000) for item in teardown_methods],
        local_helpers=helpers,
        local_models=models,
        conftest_context=conftests,
        runner_hints=[f"使用项目原生命令：{command}", "新 BRT 放在 seed test 同目录并只执行自身 nodeid。"],
        protocol_risks=risks,
        selected_seed_name=related_test.name,
        placement_dir=str(Path(related_test.file).parent),
    )


def audit_recovered_protocol(
    protocol: ProtocolRecovery,
    behavior: BehaviorTarget,
    related_test: RetrievedTest,
    llm_client: object,
    output_dir: str,
) -> ProtocolRecovery:
    """Let the model identify risks without replacing factual AST extraction."""
    prompt = PROTOCOL_RECOVERY_USER_PROMPT.format(
        behavior_json=json.dumps(behavior.to_dict(), ensure_ascii=False),
        seed_test=related_test.code_content,
        protocol_json=json.dumps(protocol.to_dict(), ensure_ascii=False),
    )
    prompt_path = Path(output_dir) / "prompts" / "protocol_recovery_prompt.txt"
    response_path = Path(output_dir) / "responses" / "protocol_recovery_response.txt"
    write_text(str(prompt_path), PROTOCOL_RECOVERY_SYSTEM_PROMPT + "\n\n" + prompt)
    response = llm_client.chat(PROTOCOL_RECOVERY_SYSTEM_PROMPT, prompt)  # type: ignore[attr-defined]
    write_text(str(response_path), response)
    data = extract_json_object(response)
    framework = str(data.get("test_framework") or protocol.test_framework)
    if framework in {"pytest", "unittest", "django", "unknown"}:
        protocol.test_framework = framework
    for field_name in ("runner_hints", "protocol_risks"):
        values = data.get(field_name)
        if isinstance(values, list):
            current = getattr(protocol, field_name)
            setattr(protocol, field_name, list(dict.fromkeys(current + [str(item) for item in values])))
    return protocol
