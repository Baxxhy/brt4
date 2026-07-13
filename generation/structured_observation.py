"""Fixed-schema public observation probes for ATS-BRT oracle search."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..core.schema import BehaviorTarget, CandidateTest, StructuredObservation
from ..core.utils import safe_json_dump, write_text
from ..execution.executor import run_command_in_conda
from ..retrieval.icore_runtime import first_test_selector, icore_test_command
from .segmenter import segment_test


START_MARKER = "BRT_STRUCTURED_OBS_START"
END_MARKER = "BRT_STRUCTURED_OBS_END"

PROBE_HELPERS = r'''
import json as _brt_json
import re as _brt_re

def _brt_normalize_text(value):
    text = str(value)
    text = _brt_re.sub(r"0x[0-9a-fA-F]+", "<ADDR>", text)
    text = _brt_re.sub(r"/tmp/[^\s'\"]+", "<TMP>", text)
    text = _brt_re.sub(r"/root/[^\s'\"]+/tmp/[^\s'\"]+", "<TMP>", text)
    text = _brt_re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b", "<ID>", text)
    text = _brt_re.sub(r"\b\d{4}-\d\d-\d\d[T ][0-9:.+-]+", "<TIME>", text)
    return text[:500]

def _brt_tokens(value, limit=20):
    return list(dict.fromkeys(_brt_re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", _brt_normalize_text(value))))[:limit]

def _brt_collect(value):
    observation = {
        "exception_type": None,
        "warning_types": [],
        "return_type": type(value).__name__,
        "return_repr_short": _brt_normalize_text(repr(value)),
        "length": None,
        "shape": None,
        "dtype": None,
        "public_attrs": {},
        "serialization_tokens": [],
        "render_tokens": [],
        "sql_tokens": [],
        "ordering": [],
        "log_tokens": [],
    }
    try:
        observation["length"] = len(value)
    except Exception:
        pass
    try:
        shape = getattr(value, "shape", None)
        if shape is not None:
            observation["shape"] = list(shape)
    except Exception:
        pass
    try:
        dtype = getattr(value, "dtype", None)
        if dtype is not None:
            observation["dtype"] = _brt_normalize_text(dtype)
    except Exception:
        pass
    try:
        attrs = vars(value)
        for key, item in list(attrs.items())[:30]:
            if key.startswith("_"):
                continue
            if item is None or isinstance(item, (bool, int, float, str)):
                observation["public_attrs"][key] = item if not isinstance(item, str) else _brt_normalize_text(item)
    except Exception:
        pass
    try:
        if isinstance(value, (list, tuple)):
            observation["ordering"] = [_brt_normalize_text(item) for item in value[:20]]
    except Exception:
        pass
    rendered = observation["return_repr_short"]
    observation["render_tokens"] = _brt_tokens(rendered)
    sql_words = {"SELECT", "FROM", "WHERE", "JOIN", "GROUP", "ORDER", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"}
    observation["sql_tokens"] = [item for item in _brt_tokens(rendered.upper()) if item.upper() in sql_words]
    for method_name in ("to_json", "serialize", "as_dict"):
        try:
            method = getattr(value, method_name, None)
            if callable(method):
                observation["serialization_tokens"] = _brt_tokens(method())
                break
        except Exception:
            pass
    return observation

def _brt_emit_observation(value):
    print("BRT_STRUCTURED_OBS_START")
    print(_brt_json.dumps(_brt_collect(value), sort_keys=True, default=str))
    print("BRT_STRUCTURED_OBS_END")
'''


def _inject_after_line(tree: ast.Module, line: int, variable_name: str) -> bool:
    emitted = ast.Expr(
        value=ast.Call(
            func=ast.Name(id="_brt_emit_observation", ctx=ast.Load()),
            args=[ast.Name(id=variable_name, ctx=ast.Load())],
            keywords=[],
        )
    )

    def rewrite_body(body: list[ast.stmt]) -> tuple[list[ast.stmt], bool]:
        result: list[ast.stmt] = []
        inserted = False
        for statement in body:
            for attr in ("body", "orelse", "finalbody"):
                nested = getattr(statement, attr, None)
                if isinstance(nested, list) and nested:
                    replacement, nested_inserted = rewrite_body(nested)
                    setattr(statement, attr, replacement)
                    inserted = inserted or nested_inserted
            result.append(statement)
            if int(getattr(statement, "lineno", 0) or 0) == line:
                result.append(emitted)
                inserted = True
        return result, inserted

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body, inserted = rewrite_body(node.body)
            if inserted:
                return True
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    child.body, inserted = rewrite_body(child.body)
                    if inserted:
                        return True
    return False


def build_structured_probe(
    candidate: CandidateTest,
    behavior: BehaviorTarget,
) -> tuple[str, dict[str, Any]]:
    segments = segment_test(candidate.code, behavior, candidate.instance_id)
    usable = [
        item
        for item in segments.observation_candidates
        if item.get("preferred_name") and not item.get("direct_in_oracle")
    ]
    if not usable:
        usable = [
            item for item in segments.observation_candidates if item.get("preferred_name")
        ]
    if not usable:
        return "", {
            "status": "AST_FALLBACK_REQUIRED",
            "reason": "target call has no stable assigned observation variable",
            "segments": segments.to_dict(),
        }
    selected = usable[-1]
    try:
        tree = ast.parse(candidate.code)
    except SyntaxError as exc:
        return "", {"status": "INVALID", "reason": str(exc), "segments": segments.to_dict()}
    helper_nodes = ast.parse(PROBE_HELPERS).body
    insert_at = 0
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        insert_at = 1
    while insert_at < len(tree.body):
        node = tree.body[insert_at]
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            insert_at += 1
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            insert_at += 1
            continue
        break
    tree.body[insert_at:insert_at] = helper_nodes
    injected = _inject_after_line(
        tree,
        int(selected.get("statement_lineno") or 0),
        str(selected.get("preferred_name") or ""),
    )
    if not injected:
        return "", {
            "status": "AST_FALLBACK_REQUIRED",
            "reason": "could not insert observation after target statement",
            "segments": segments.to_dict(),
        }
    ast.fix_missing_locations(tree)
    probe_code = ast.unparse(tree).strip() + "\n"
    return probe_code, {
        "status": "READY",
        "target_expression": (selected.get("target_expressions") or [""])[0],
        "observation_variable": selected.get("preferred_name"),
        "statement_lineno": selected.get("statement_lineno"),
        "segments": segments.to_dict(),
    }


def _extract_payload(text: str) -> dict[str, Any]:
    matches = re.findall(
        rf"{START_MARKER}\s*(.*?)\s*{END_MARKER}", text, flags=re.S
    )
    for block in reversed(matches):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        for line in reversed(lines):
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                return value
    return {}


def _bounded_tokens(text: str, limit: int = 30) -> list[str]:
    cleaned = re.sub(r"0x[0-9a-fA-F]+", "<ADDR>", text)
    cleaned = re.sub(r"/(?:tmp|root)/\S+", "<PATH>", cleaned)
    return list(dict.fromkeys(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", cleaned)))[:limit]


def normalize_observation(
    instance_id: str,
    payload: dict[str, Any],
    execution: dict[str, Any],
    target_expression: str,
    source: str,
    fallback_reason: str = "",
) -> StructuredObservation:
    fields = StructuredObservation.__dataclass_fields__
    data = {key: payload.get(key) for key in fields if key in payload}
    data.update(
        {
            "instance_id": instance_id,
            "source": source,
            "target_expression": target_expression,
            "fallback_reason": fallback_reason,
            "status": "COLLECTED" if payload else "EMPTY",
        }
    )
    if not data.get("exception_type"):
        data["exception_type"] = execution.get("exception_type") or None
    if not data.get("log_tokens"):
        data["log_tokens"] = _bounded_tokens(
            str(execution.get("stdout") or "") + "\n" + str(execution.get("stderr") or "")
        )
    material = json.dumps(data, sort_keys=True, ensure_ascii=True, default=str)
    data["observation_id"] = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return StructuredObservation(**{key: value for key, value in data.items() if key in fields})


def collect_structured_observation(
    behavior: BehaviorTarget,
    candidate: CandidateTest,
    output_dir: str,
    buggy_repo: str,
    conda_env: str,
    timeout: int,
    no_conda: bool,
    repo: str,
    version: str,
) -> StructuredObservation:
    probe_code, metadata = build_structured_probe(candidate, behavior)
    if not probe_code:
        observation = normalize_observation(
            behavior.instance_id,
            {},
            {},
            "",
            "ast",
            str(metadata.get("reason") or "AST probe unavailable"),
        )
        safe_json_dump(observation.to_dict(), str(Path(output_dir) / "structured_observations.json"))
        return observation
    probe_path = Path(output_dir) / "structured_observation_probe.py"
    write_text(str(probe_path), probe_code)
    repo_probe = Path(candidate.candidate_file_path).with_name(
        Path(candidate.candidate_file_path).stem + "_structured_probe.py"
    )
    write_text(str(repo_probe), probe_code)
    relative_probe = str(Path(candidate.candidate_repo_path).with_name(repo_probe.name))
    command = icore_test_command(
        repo, version, relative_probe, first_test_selector(probe_code)
    )
    if "pytest" in command:
        command += " -s"
    execution = run_command_in_conda(
        command,
        buggy_repo,
        conda_env,
        timeout,
        no_conda,
        behavior,
        behavior.instance_id,
    )
    execution_data = execution.to_dict()
    payload = _extract_payload(execution.stdout + "\n" + execution.stderr)
    observation = normalize_observation(
        behavior.instance_id,
        payload,
        execution_data,
        str(metadata.get("target_expression") or ""),
        "ast_instrumentation",
    )
    artifact = observation.to_dict()
    artifact["probe_metadata"] = metadata
    artifact["execution"] = execution_data
    safe_json_dump(artifact, str(Path(output_dir) / "structured_observations.json"))
    return observation
