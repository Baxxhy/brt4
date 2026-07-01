"""Input loading and output helpers for BRT3."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..core.config import DEFAULT_TOP_CODE, DEFAULT_TOP_TESTS
from ..core.schema import InstanceContext, RetrievedCode, RetrievedTest
from ..core.utils import safe_json_load, truncate_text


def _load_json_or_jsonl(path: str) -> Any:
    if path.endswith(".jsonl"):
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    return safe_json_load(path)


def _issue_text(row: dict[str, Any]) -> str:
    if row.get("problem_statement"):
        return str(row["problem_statement"])
    if row.get("issue_text"):
        return str(row["issue_text"])
    if row.get("issue"):
        return str(row["issue"])
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")
    return (title + "\n\n" + desc).strip()


def load_issue_data(path: str) -> dict[str, dict[str, Any]]:
    data = _load_json_or_jsonl(path)
    out: dict[str, dict[str, Any]] = {}
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = []
        for key, value in data.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("instance_id", key)
                rows.append(row)
    else:
        raise ValueError(f"unsupported issue data type: {type(data).__name__}")
    for row in rows:
        if not isinstance(row, dict):
            continue
        instance_id = row.get("instance_id")
        if not instance_id:
            continue
        normalized = dict(row)
        normalized["issue_text"] = _issue_text(row)
        out[str(instance_id)] = normalized
    return out


def _items_for_instance(raw: Any, instance_id: str) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        value = raw.get(instance_id, [])
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            return [
                dict(v, name=k)
                for k, v in value.items()
                if isinstance(v, dict)
            ]
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict) and x.get("instance_id") == instance_id]
    return []


def load_retrieved_code(path: str, instance_id: str, top_k: int = DEFAULT_TOP_CODE) -> list[RetrievedCode]:
    raw = _load_json_or_jsonl(path)
    seen: set[tuple[Any, ...]] = set()
    out: list[RetrievedCode] = []
    for item in _items_for_instance(raw, instance_id):
        key = (item.get("path", ""), item.get("obj_name") or item.get("name", ""), item.get("code_start_line", ""), item.get("code_end_line", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            RetrievedCode(
                instance_id=instance_id,
                obj_name=str(item.get("obj_name") or item.get("name") or ""),
                node_type=str(item.get("node_type") or ""),
                path=str(item.get("path") or ""),
                code_start_line=item.get("code_start_line", ""),
                code_end_line=item.get("code_end_line", ""),
                code_content=str(item.get("code_content") or item.get("content") or item.get("code") or ""),
                parent=str(item.get("parent") or ""),
                raw=item,
            )
        )
        if len(out) >= top_k:
            break
    return out


def load_retrieved_tests(path: str, instance_id: str, top_k: int = DEFAULT_TOP_TESTS) -> list[RetrievedTest]:
    raw = _load_json_or_jsonl(path)
    seen: set[tuple[str, str]] = set()
    out: list[RetrievedTest] = []
    for item in _items_for_instance(raw, instance_id):
        key = (str(item.get("file") or item.get("path") or ""), str(item.get("name") or item.get("test_name") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            RetrievedTest(
                instance_id=instance_id,
                name=key[1],
                file=key[0],
                code_content=str(item.get("code_content") or item.get("content") or item.get("code") or ""),
                raw=item,
            )
        )
        if len(out) >= top_k:
            break
    return out


def format_code_context(items: list[RetrievedCode], max_chars: int = 18000) -> str:
    chunks = []
    for i, item in enumerate(items, 1):
        chunks.append(
            f"【源码片段 {i}】\n"
            f"文件路径：{item.path}\n对象名称：{item.obj_name}\n节点类型：{item.node_type}\n"
            f"父节点：{item.parent}\n行号范围：{item.code_start_line}-{item.code_end_line}\n代码：\n{item.code_content}\n"
        )
    return truncate_text("\n".join(chunks), max_chars)


def format_test_context(items: list[RetrievedTest], max_chars: int = 18000) -> str:
    chunks = []
    for i, item in enumerate(items, 1):
        chunks.append(f"【相关测试 {i}】\n测试文件：{item.file}\n测试名称：{item.name}\n测试代码：\n{item.code_content}\n")
    return truncate_text("\n".join(chunks), max_chars)


def infer_repo_path(repo_root_base: str, issue_row: dict[str, Any], instance_id: str) -> str:
    repo = str(issue_row.get("repo") or "")
    candidates: list[str] = []
    if repo:
        candidates += [repo.split("/")[-1], repo.replace("/", "__")]
    prefix = instance_id.split("__", 1)[0]
    aliases = {
        "pytest-dev": "pytest",
        "sphinx-doc": "sphinx",
        "pylint-dev": "pylint",
        "pallets": "flask",
        "psf": "requests",
        "pydata": "xarray",
        "mwaskom": "seaborn",
    }
    candidates += [aliases.get(prefix, prefix)]
    for name in candidates:
        p = os.path.join(repo_root_base, name)
        if os.path.isdir(p):
            return p
    return os.path.join(repo_root_base, candidates[0] if candidates else prefix)


def build_instance_context(
    instance_id: str,
    issue_row: dict[str, Any],
    code_path: str,
    test_path: str,
    repo_root_base: str = "",
    top_code: int = DEFAULT_TOP_CODE,
    top_tests: int = DEFAULT_TOP_TESTS,
) -> InstanceContext:
    buggy_repo = infer_repo_path(repo_root_base, issue_row, instance_id) if repo_root_base else ""
    forbidden_generation_fields = {
        "patch",
        "test_patch",
        "gold_patch",
        "golden_patch",
        "golden_test",
        "model_patch",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
    }
    return InstanceContext(
        instance_id=instance_id,
        issue_text=str(issue_row.get("issue_text") or _issue_text(issue_row)),
        repo=str(issue_row.get("repo") or ""),
        base_commit=str(issue_row.get("base_commit") or ""),
        buggy_repo_path=buggy_repo,
        retrieved_code=load_retrieved_code(code_path, instance_id, top_code),
        retrieved_tests=load_retrieved_tests(test_path, instance_id, top_tests),
        metadata={
            k: v
            for k, v in issue_row.items()
            if k
            not in {
                "issue_text",
                "problem_statement",
                "issue",
                "description",
                *forbidden_generation_fields,
            }
        },
    )


def read_repo_file(repo_path: str, rel_path: str) -> str:
    full = Path(repo_path) / rel_path
    if not full.exists():
        return ""
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        return f.read()
