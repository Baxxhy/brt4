"""Candidate generation and repair."""

from __future__ import annotations

import json
import os
import ast
import re
import textwrap
from pathlib import Path
from typing import Any

from ..io.io_utils import format_code_context
from ..core.prompts import (
    MUTATION_GENERATION_SYSTEM_PROMPT,
    MUTATION_GENERATION_USER_PROMPT,
    REPAIR_ORACLE_SYSTEM_PROMPT,
    REPAIR_ORACLE_USER_PROMPT,
    REPAIR_SETUP_SYSTEM_PROMPT,
    REPAIR_SETUP_USER_PROMPT,
    REPAIR_TRIGGER_SYSTEM_PROMPT,
    REPAIR_TRIGGER_USER_PROMPT,
)
from ..core.schema import BehaviorTarget, CandidateTest, ExecutionResult, HostContext, MutationPlan, ProtocolRecovery, RetrievedCode, RetrievedTest
from ..validation.semantic_guard import audit_candidate
from ..core.utils import clean_code_block, ensure_dir, sanitize_instance_id, write_text


def _source_window(path: Path, object_name: str, max_chars: int = 10000) -> tuple[str, int, int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if len(text) <= max_chars:
        return text, 1, len(lines)
    simple_name = object_name.rsplit(".", 1)[-1].strip()
    match_index = -1
    if simple_name:
        pattern = re.compile(
            rf"^\s*(?:class|def|async\s+def)\s+{re.escape(simple_name)}\b"
        )
        for index, line in enumerate(lines):
            if pattern.search(line):
                match_index = index
                break
    if match_index < 0:
        match_index = 0
    start = max(0, match_index - 25)
    end = min(len(lines), match_index + 150)
    excerpt = "\n".join(lines[start:end])
    return excerpt[:max_chars], start + 1, end


def _effective_source_context(
    behavior: BehaviorTarget,
    related_source: list[RetrievedCode],
    buggy_repo: str,
) -> list[RetrievedCode]:
    effective = list(related_source)
    seen = {(item.path, item.obj_name) for item in effective}
    repo_root = Path(buggy_repo).resolve() if buggy_repo else None
    if not repo_root or not repo_root.is_dir():
        return effective
    for location in behavior.suspected_bug_locations:
        relative = str(location.get("path") or "").strip().replace("\\", "/")
        object_name = str(location.get("object") or "").strip()
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            continue
        source_path = (repo_root / relative).resolve()
        try:
            source_path.relative_to(repo_root)
        except ValueError:
            continue
        if not source_path.is_file():
            continue
        try:
            content, start, end = _source_window(source_path, object_name)
        except OSError:
            continue
        matching_retrievals = [
            item
            for item in effective
            if item.path == relative and item.obj_name == object_name
        ]
        # A retrieval hit with the same path/object may contain only a narrow
        # method fragment. Keep the current buggy-repo window unless an
        # existing hit already contains that whole window, otherwise lifecycle
        # methods such as check()/deconstruct() can disappear from constraints.
        if content.strip() and any(
            content.strip() in item.code_content for item in matching_retrievals
        ):
            continue
        effective.append(
            RetrievedCode(
                instance_id=behavior.instance_id,
                obj_name=object_name,
                node_type="inferred_source_location",
                path=relative,
                code_start_line=start,
                code_end_line=end,
                code_content=content,
                raw={"source": "behavior_target.suspected_bug_locations"},
            )
        )
        seen.add((relative, object_name))
    return effective


def format_effective_source_context(
    behavior: BehaviorTarget,
    related_source: list[RetrievedCode],
    buggy_repo: str,
) -> str:
    return format_code_context(
        _effective_source_context(behavior, related_source, buggy_repo)
    )


def _persistent_setup_failure(code: str, execution_log: str) -> str:
    if (
        'near "[]": syntax error' in execution_log
        and "ArrayField" in code
        and not re.search(r"\bmanaged\s*=\s*False\b", code)
    ):
        return (
            "SQLite 在测试数据库建表阶段尝试创建 PostgreSQL ArrayField，尚未进入"
            "目标测试。若 Issue 只需要表单/field/formset 行为，所有测试内声明且含"
            "ArrayField 的临时 Model 必须设置 Meta.managed = False，并避免依赖这些"
            "表的数据库读写；或复用 HostContext 中已验证的 PostgreSQL 测试环境。"
            "skipUnlessDBFeature 无法阻止测试数据库在测试方法执行前建表，因此不是修复。"
        )
    if (
        "fetch_command" in execution_log
        and "KeyError:" in execution_log
        and (
            "execute_from_command_line" in code
            or re.search(r"\.\s*run_manage\s*\(", code)
        )
    ):
        return (
            "management command 未注册，执行日志在 fetch_command 中抛出 KeyError，"
            "但返回代码仍通过 execute_from_command_line()/run_manage() 查找该临时命令。"
            "对于 parser/help 行为，应直接实例化测试内 Command，调用 create_parser()、"
            "format_help()/print_help() 或 run_from_argv()；不要依赖运行中进程刷新"
            "临时 app 的命令注册表。"
        )
    if "<locals>" in execution_log:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            tree = None
        nested_definitions: list[str] = []
        if tree is not None:
            def visit(node: ast.AST, inside_function: bool = False) -> None:
                is_function = isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                )
                if inside_function and isinstance(
                    node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    nested_definitions.append(node.name)
                for child in ast.iter_child_nodes(node):
                    visit(child, inside_function or is_function)

            visit(tree)
        if nested_definitions:
            names = ", ".join(sorted(set(nested_definitions)))
            return (
                "执行日志中的序列化路径包含 '<locals>'，而返回代码仍在测试函数"
                f"内部定义待序列化的类或函数（{names}）。必须把这些定义真正移动到"
                "模块顶层，使其 __module__ 和 __qualname__ 可导入；只在注释中声称"
                "“module-level”或仅重命名无效。MigrationWriter、deconstruct、"
                "pickle 和 serializer 使用的自定义类型都适用此规则。"
            )
    missing_import = re.search(
        r"ImportError:\s+cannot import name\s+['\"]?([A-Za-z_][A-Za-z0-9_]*)",
        execution_log,
        re.IGNORECASE,
    )
    if missing_import:
        symbol = missing_import.group(1)
        if re.search(
            rf"(?m)^\s*from\s+\S+\s+import\s+[^\n]*\b{re.escape(symbol)}\b",
            code,
        ):
            return (
                f"返回代码仍然直接导入执行日志明确不存在的符号 {symbol!r}。"
                "必须删除这条 direct import，并采用 HostContext 中已验证的导入方式；"
                "若目标是带数字前缀的迁移模块，可使用 importlib.import_module。"
            )
    missing_module = re.search(
        r"ModuleNotFoundError:\s+No module named\s+['\"]([^'\"]+)",
        execution_log,
        re.IGNORECASE,
    )
    if missing_module:
        module = missing_module.group(1)
        if re.search(re.escape(module), code):
            return (
                f"返回代码仍然引用环境中不存在的模块 {module!r}，"
                "可能位于 import、INSTALLED_APPS、app_label 或配置字符串中。"
                "必须删除该虚构模块，并复用 HostContext 中已存在的项目模型、应用和 import。"
            )
    return ""


def _semantic_path_problem(
    behavior: BehaviorTarget,
    source_context: str,
    code: str,
) -> str:
    generic_problem = audit_candidate(behavior, code)
    if generic_problem:
        return generic_problem
    behavior_text = " ".join(
        [
            behavior.issue_summary,
            str(behavior.error_symptom.get("text") or ""),
            str(behavior.expected_behavior.get("text") or ""),
        ]
    ).lower()
    mentions_check = any(
        marker in behavior_text
        for marker in ("check", "检查", "验证")
    )
    describes_missing_check = any(
        marker in behavior_text
        for marker in (
            "add check",
            "missing",
            "no check",
            "缺少",
            "没有",
            "新增",
            "添加",
        )
    )
    if (
        mentions_check
        and describes_missing_check
        and re.search(r"\bdef\s+check\s*\(", source_context)
    ):
        if not re.search(r"\.\s*check\s*\(", code):
            return (
                "BehaviorTarget 描述的是缺失检查，且相关源码明确暴露 check() 生命周期，"
                "但返回测试没有调用 check()。必须保留正确 setup，并调用真实 check()，"
                "断言修复后应出现的检查结果。"
            )
        expects_exception = any(
            marker in str(behavior.expected_behavior.get("text") or "").lower()
            for marker in ("抛出", "异常", "raise", "exception")
        )
        if not expects_exception and re.search(
            r"(?:assertRaises|pytest\.raises)", code
        ):
            return (
                "Expected behavior 没有要求抛异常；不得用 raises 包裹 check()。"
                "应断言 check() 返回的稳定错误/警告证据存在。"
            )
    mentions_logging = any(
        marker in behavior_text
        for marker in ("logger", "logging", "log message", "日志", "记录异常")
    )
    describes_missing_logging = any(
        marker in behavior_text
        for marker in ("missing", "doesn't have", "没有", "缺少", "未记录")
    )
    if mentions_logging and describes_missing_logging:
        if re.search(r"(?:patch|patch\.object)\s*\([^)]*logger", code):
            return (
                "Issue 描述的是生产代码缺少日志；不得 patch 一个 buggy 源码中尚不存在的"
                " logger 属性。应使用 assertLogs/caplog 捕获目标模块的日志命名空间，"
                "让 buggy 因没有日志证据而失败。"
            )
        if "assertLogs" not in code and "caplog" not in code:
            return (
                "缺失日志类 BRT 必须通过 assertLogs 或 caplog 观察修复后应出现的日志，"
                "不能仅检查异常返回值。"
            )
    return ""


def _wrap_if_needed(code: str, host: HostContext, safe_id: str) -> str:
    code = textwrap.dedent(clean_code_block(code)).strip() + "\n"
    if not code.strip():
        return f"def test_brt_{safe_id}():\n    assert True\n"
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            if node.args.args and node.args.args[0].arg == "self":
                class_name = "TestBRT" + "".join(part.capitalize() for part in safe_id.split("_") if part)
                return "import unittest\n\n\nclass " + class_name + "(unittest.TestCase):\n" + textwrap.indent(code, "    ")
            break
    return code


def _candidate_paths(instance_id: str, buggy_repo: str, host: HostContext) -> tuple[str, str]:
    safe_id = sanitize_instance_id(instance_id)
    host_dir = os.path.dirname(host.host_file) if host.host_file else "tests"
    rel = os.path.join(host_dir, f"test_brt_{safe_id}.py")
    return rel, str(Path(buggy_repo) / rel)


def write_candidate_to_repo(candidate: CandidateTest, buggy_repo: str) -> CandidateTest:
    ensure_dir(Path(candidate.candidate_file_path).parent)
    write_text(candidate.candidate_file_path, candidate.code)
    candidate.pytest_nodeid = candidate.candidate_repo_path
    candidate.command = f"python -m pytest {candidate.candidate_repo_path} -q"
    return candidate


def generate_candidate(
    instance_id: str,
    behavior: BehaviorTarget,
    host: HostContext,
    related_test: RetrievedTest | None,
    related_source: list[RetrievedCode],
    llm_client: Any,
    output_dir: str,
    buggy_repo: str,
    round_id: int = 0,
    feedback: str = "",
    write_to_repo: bool = True,
    protocol: ProtocolRecovery | None = None,
    mutation_plan: MutationPlan | None = None,
) -> CandidateTest:
    ensure_dir(Path(output_dir) / "prompts")
    ensure_dir(Path(output_dir) / "responses")
    safe_id = sanitize_instance_id(instance_id)
    user_prompt = MUTATION_GENERATION_USER_PROMPT.format(
        instance_id=instance_id,
        safe_instance_id=safe_id,
        insert_strategy=host.insert_strategy,
        behavior_json=json.dumps(behavior.to_dict(), ensure_ascii=False),
        host_context_json=json.dumps(host.to_dict(), ensure_ascii=False),
        code_context=format_effective_source_context(
            behavior, related_source, buggy_repo
        ),
        seed_test_code=(related_test.code_content if related_test else host.seed_test_code),
        feedback=feedback or "无",
    )
    if protocol is not None:
        user_prompt += "\n\n【必须保留的测试协议】\n" + json.dumps(protocol.to_dict(), ensure_ascii=False)
    if mutation_plan is not None:
        user_prompt += (
            "\n\n【已经校验的小变异计划】\n"
            + json.dumps(mutation_plan.to_dict(), ensure_ascii=False)
            + "\n必须严格按 mutation plan 生成一个完整 Python 文件；不得自由扩大变异或重写无关 setup。"
        )
    prompt_path = str(Path(output_dir) / "prompts" / f"generation_round_{round_id}.txt")
    response_path = str(Path(output_dir) / "responses" / f"generation_round_{round_id}.txt")
    write_text(prompt_path, MUTATION_GENERATION_SYSTEM_PROMPT + "\n\n" + user_prompt)
    response = llm_client.chat(MUTATION_GENERATION_SYSTEM_PROMPT, user_prompt)
    write_text(response_path, response)
    code = _wrap_if_needed(response, host, safe_id)
    rel_path, full_path = _candidate_paths(instance_id, buggy_repo, host)
    candidate = CandidateTest(
        instance_id=instance_id,
        round_id=round_id,
        code=code,
        candidate_file_path=full_path,
        candidate_repo_path=rel_path,
        prompt_path=prompt_path,
        response_path=response_path,
        lineage={
            "origin": "generation",
            "parent_candidate_id": "",
            "seed_id": host.seed_test_name or host.host_file or "",
            "generation_round": round_id,
            "repair_round": 0,
            "round": round_id,
            "search_action": "initial_generation",
            "duplicate_redirected_from": "",
            "segment_hashes": {},
            "observation_id": "",
            "observation_context_used": "none",
            "counterfactual_evidence_id": "",
            "negative_control_id": "",
        },
    )
    if write_to_repo:
        write_candidate_to_repo(candidate, buggy_repo)
    else:
        candidate.pytest_nodeid = candidate.candidate_repo_path
        candidate.command = f"python -m pytest {candidate.candidate_repo_path} -q"
    write_text(str(Path(output_dir) / f"candidate_round_{round_id}.py"), code)
    return candidate


def repair_candidate(
    instance_id: str,
    behavior: BehaviorTarget,
    host: HostContext,
    candidate: CandidateTest,
    execution: ExecutionResult,
    llm_client: Any,
    output_dir: str,
    round_id: int,
    focus: str,
    related_source: list[RetrievedCode] | None = None,
    observation_json: str = "{}",
    verifier_feedback: dict[str, Any] | None = None,
    buggy_repo: str = "",
    protocol: ProtocolRecovery | None = None,
    mutation_plan: MutationPlan | None = None,
    search_context: dict[str, Any] | None = None,
    origin_override: str = "",
) -> CandidateTest:
    feedback_json = json.dumps(verifier_feedback or {}, ensure_ascii=False)
    source_context = format_effective_source_context(
        behavior, related_source or [], buggy_repo
    )
    if focus == "setup":
        system = REPAIR_SETUP_SYSTEM_PROMPT
        template = REPAIR_SETUP_USER_PROMPT
        kwargs = {
            "behavior_json": json.dumps(behavior.to_dict(), ensure_ascii=False),
            "host_context_json": json.dumps(host.to_dict(), ensure_ascii=False),
            "candidate_code": candidate.code,
            "execution_log": execution.stdout + "\n" + execution.stderr,
            "verifier_feedback": feedback_json,
        }
    elif focus == "oracle":
        system = REPAIR_ORACLE_SYSTEM_PROMPT
        template = REPAIR_ORACLE_USER_PROMPT
        kwargs = {
            "behavior_json": json.dumps(behavior.to_dict(), ensure_ascii=False),
            "candidate_code": candidate.code,
            "execution_log": execution.stdout + "\n" + execution.stderr,
            "observation_json": observation_json,
            "verifier_feedback": feedback_json,
        }
    else:
        system = REPAIR_TRIGGER_SYSTEM_PROMPT
        template = REPAIR_TRIGGER_USER_PROMPT
        kwargs = {
            "behavior_json": json.dumps(behavior.to_dict(), ensure_ascii=False),
            "host_context_json": json.dumps(host.to_dict(), ensure_ascii=False),
            "seed_test_code": host.seed_test_code,
            "code_context": source_context,
            "candidate_code": candidate.code,
            "execution_log": execution.stdout + "\n" + execution.stderr,
            "verifier_feedback": feedback_json,
        }
    user_prompt = template.format(**kwargs)
    if protocol is not None:
        user_prompt += "\n\nProtocolRecovery：" + json.dumps(protocol.to_dict(), ensure_ascii=False)
    if mutation_plan is not None:
        user_prompt += "\n\n本轮校验后的 mutation plan：" + json.dumps(mutation_plan.to_dict(), ensure_ascii=False)
        user_prompt += "\n只执行 plan 中的小变异，不能修改 oracle。"
    if search_context:
        user_prompt += (
            "\n\n【ATS-BRT 类型化搜索约束】\n"
            + json.dumps(search_context, ensure_ascii=False)
            + "\n必须保持 frozen segment 的 AST 语义不变；不得借本轮动作重写其他段。"
        )
    prompt_path = str(Path(output_dir) / "prompts" / f"repair_prompt_round_{round_id}.txt")
    response_path = str(Path(output_dir) / "responses" / f"repair_response_round_{round_id}.txt")
    write_text(prompt_path, system + "\n\n" + user_prompt)
    response = llm_client.chat(system, user_prompt)
    write_text(response_path, response)
    code = _wrap_if_needed(response, host, sanitize_instance_id(instance_id))
    if code.strip() == candidate.code.strip():
        retry_prompt = (
            user_prompt
            + "\n\n上一次返回与当前测试完全相同，属于无效修复。"
            + "这次必须根据 Verifier 反馈修改导致失败的具体代码位置；"
            + "如果无法修复，也不能原样返回。仍然只输出完整 Python 文件。"
        )
        retry_response_path = str(
            Path(output_dir)
            / "responses"
            / f"repair_response_round_{round_id}_unchanged_retry.txt"
        )
        response = llm_client.chat(system, retry_prompt)
        write_text(retry_response_path, response)
        code = _wrap_if_needed(response, host, sanitize_instance_id(instance_id))
    if focus == "setup":
        setup_log = execution.stdout + "\n" + execution.stderr
        for validation_attempt in range(2):
            setup_problem = _persistent_setup_failure(code, setup_log)
            if not setup_problem:
                break
            retry_prompt = (
                user_prompt
                + "\n\n返回代码执行前校验失败："
                + setup_problem
                + "\n当前仍然无效的返回代码如下：\n"
                + code
                + "\n必须返回已消除该失败模式的完整 Python 文件。"
            )
            retry_response_path = str(
                Path(output_dir)
                / "responses"
                / (
                    f"repair_response_round_{round_id}_setup_validation_retry_"
                    f"{validation_attempt + 1}.txt"
                )
            )
            response = llm_client.chat(system, retry_prompt)
            write_text(retry_response_path, response)
            code = _wrap_if_needed(
                response, host, sanitize_instance_id(instance_id)
            )
    for validation_attempt in range(2):
        semantic_problem = _semantic_path_problem(
            behavior, source_context, code
        )
        if not semantic_problem:
            break
        retry_prompt = (
            user_prompt
            + "\n\n返回代码执行前的源码路径校验失败："
            + semantic_problem
            + "\n当前仍然无效的返回代码如下：\n"
            + code
            + "\n必须返回通过该路径校验的完整 Python 文件。"
        )
        retry_response_path = str(
            Path(output_dir)
            / "responses"
            / (
                f"repair_response_round_{round_id}_semantic_validation_retry_"
                f"{validation_attempt + 1}.txt"
            )
        )
        response = llm_client.chat(system, retry_prompt)
        write_text(retry_response_path, response)
        code = _wrap_if_needed(
            response, host, sanitize_instance_id(instance_id)
        )
    new_candidate = CandidateTest(
        instance_id=instance_id,
        round_id=round_id,
        code=code,
        candidate_file_path=candidate.candidate_file_path,
        candidate_repo_path=candidate.candidate_repo_path,
        prompt_path=prompt_path,
        response_path=response_path,
        lineage={
            "origin": origin_override or (
                "repair_setup"
                if focus == "setup"
                else "repair_oracle"
                if focus == "oracle"
                else "repair_trigger"
            ),
            "parent_candidate_id": str(
                (candidate.lineage or {}).get("candidate_id") or candidate.round_id
            ),
            "seed_id": str((candidate.lineage or {}).get("seed_id") or ""),
            "generation_round": round_id,
            "repair_round": round_id,
            "round": round_id,
            "search_action": str((search_context or {}).get("variant") or ""),
            "duplicate_redirected_from": str(
                (search_context or {}).get("duplicate_redirected_from") or ""
            ),
            "segment_hashes": dict((search_context or {}).get("segment_hashes") or {}),
            "observation_id": str((search_context or {}).get("observation_id") or ""),
            "observation_context_used": str(
                (candidate.lineage or {}).get("observation_context_used") or "none"
            ),
            "counterfactual_evidence_id": "",
            "negative_control_id": "",
        },
    )
    write_text(new_candidate.candidate_file_path, code)
    new_candidate.pytest_nodeid = new_candidate.candidate_repo_path
    new_candidate.command = candidate.command
    write_text(str(Path(output_dir) / f"candidate_round_{round_id}.py"), code)
    return new_candidate
