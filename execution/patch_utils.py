"""Independent surrogate source-patch generation and validation."""

from __future__ import annotations

import difflib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .executor import run_command_in_conda
from ..core.prompts import SURROGATE_PATCH_SYSTEM_PROMPT, SURROGATE_PATCH_USER_PROMPT
from ..core.schema import (
    BehaviorTarget,
    CandidateTest,
    DualVersionResult,
    ExecutionResult,
    RetrievedCode,
    SurrogatePatchCandidate,
)
from ..core.utils import ensure_dir, extract_json_object, safe_json_dump, truncate_text, write_text


def _effective_surrogate_sources(
    behavior: BehaviorTarget,
    retrieved_code: list[RetrievedCode],
    repo_path: str,
) -> list[RetrievedCode]:
    sources = list(retrieved_code)
    seen = {item.path for item in sources if item.path}
    for location in behavior.suspected_bug_locations:
        path = str(location.get("path") or "")
        if not path or path in seen or not (Path(repo_path) / path).is_file():
            continue
        sources.append(
            RetrievedCode(
                instance_id=behavior.instance_id,
                obj_name=str(location.get("object") or ""),
                path=path,
                code_start_line="",
                code_end_line="",
                code_content="",
                raw={"source": "behavior_target.suspected_bug_locations"},
            )
        )
        seen.add(path)
    return sources


def _source_excerpt(repo_path: str, item: RetrievedCode, max_chars: int = 9000) -> str:
    path = Path(repo_path) / item.path
    if not path.is_file():
        return item.code_content
    source = path.read_text(encoding="utf-8", errors="replace")
    if len(source) <= 20000:
        return source
    lines = source.splitlines()
    try:
        start = max(0, int(item.code_start_line) - 121)
        end = min(len(lines), int(item.code_end_line) + 120)
        excerpt = "\n".join(lines[start:end])
    except (TypeError, ValueError):
        excerpt = source
    return truncate_text(excerpt, max_chars)


def format_surrogate_source_context(
    repo_path: str,
    retrieved_code: list[RetrievedCode],
    max_chars: int = 30000,
) -> str:
    chunks = []
    for index, item in enumerate(retrieved_code, 1):
        if not item.path:
            continue
        chunks.append(
            f"【允许修改源码 {index}】\n"
            f"path: {item.path}\n"
            f"object: {item.obj_name}\n"
            f"lines: {item.code_start_line}-{item.code_end_line}\n"
            f"code:\n{_source_excerpt(repo_path, item)}"
        )
    return truncate_text("\n\n".join(chunks), max_chars)


def generate_surrogate_patch(
    instance_id: str,
    behavior: BehaviorTarget,
    candidate: CandidateTest,
    retrieved_code: list[RetrievedCode],
    buggy_execution: ExecutionResult,
    llm_client: Any,
    output_dir: str,
    buggy_repo: str,
    round_id: int,
    previous_attempts: list[dict[str, Any]] | None = None,
) -> SurrogatePatchCandidate:
    ensure_dir(Path(output_dir) / "prompts")
    ensure_dir(Path(output_dir) / "responses")
    source_context = format_surrogate_source_context(buggy_repo, retrieved_code)
    compact_attempts = []
    for attempt in previous_attempts or []:
        execution = attempt.get("execution") if isinstance(attempt, dict) else {}
        compact_attempts.append(
            {
                "round_id": attempt.get("round_id"),
                "status": attempt.get("status"),
                "reason": attempt.get("reason"),
                "patches": attempt.get("patches"),
                "execution_status": (
                    execution.get("status") if isinstance(execution, dict) else ""
                ),
                "execution_tail": truncate_text(
                    (
                        str(execution.get("stdout") or "")
                        + "\n"
                        + str(execution.get("stderr") or "")
                    )
                    if isinstance(execution, dict)
                    else "",
                    3000,
                ),
            }
        )
    user_prompt = SURROGATE_PATCH_USER_PROMPT.format(
        behavior_json=json.dumps(behavior.to_dict(), ensure_ascii=False),
        final_test=candidate.code,
        buggy_execution_log=truncate_text(
            buggy_execution.stdout + "\n" + buggy_execution.stderr, 12000
        ),
        code_context=source_context,
        previous_attempts=json.dumps(compact_attempts, ensure_ascii=False),
    )
    prompt_path = str(
        Path(output_dir) / "prompts" / f"surrogate_patch_prompt_round_{round_id}.txt"
    )
    response_path = str(
        Path(output_dir) / "responses" / f"surrogate_patch_response_round_{round_id}.txt"
    )
    write_text(prompt_path, SURROGATE_PATCH_SYSTEM_PROMPT + "\n\n" + user_prompt)
    response = llm_client.chat(SURROGATE_PATCH_SYSTEM_PROMPT, user_prompt)
    write_text(response_path, response)
    data = extract_json_object(response)
    patches = data.get("patches") if isinstance(data, dict) else []
    if not isinstance(patches, list):
        patches = []
    return SurrogatePatchCandidate(
        instance_id=instance_id,
        round_id=round_id,
        patches=[item for item in patches[:3] if isinstance(item, dict)],
        status="GENERATED",
        reason=str(data.get("analysis") or "") if isinstance(data, dict) else "",
        prompt_path=prompt_path,
        response_path=response_path,
    )


def _validate_patch_items(
    patch_candidate: SurrogatePatchCandidate,
    retrieved_code: list[RetrievedCode],
    test_path: str,
) -> tuple[list[dict[str, str]], str]:
    allowed = {item.path for item in retrieved_code if item.path}
    validated: list[dict[str, str]] = []
    for raw in patch_candidate.patches:
        path = str(raw.get("path") or "").strip().replace("\\", "/")
        search = str(raw.get("search") or "")
        replace = str(raw.get("replace") or "")
        if not path or path.startswith("/") or ".." in Path(path).parts:
            return [], f"unsafe patch path: {path}"
        lowered = path.lower()
        if (
            path == test_path
            or "/tests/" in f"/{lowered}"
            or Path(path).name.startswith("test_")
            or Path(path).name == "conftest.py"
        ):
            return [], f"surrogate patch attempted to modify test/config file: {path}"
        if (
            Path(path).name
            in {
                "pyproject.toml",
                "setup.py",
                "setup.cfg",
                "tox.ini",
                "requirements.txt",
                "requirements-dev.txt",
                "environment.yml",
                "environment.yaml",
            }
            or "/requirements/" in f"/{lowered}"
            or "/requirements-" in f"/{lowered}"
        ):
            return [], f"surrogate patch attempted to modify config/dependency file: {path}"
        if allowed and path not in allowed:
            return [], f"path is not in retrieved source context: {path}"
        if not search or search == replace:
            return [], f"invalid no-op search/replace for {path}"
        validated.append(
            {
                "path": path,
                "search": search,
                "replace": replace,
                "reason": str(raw.get("reason") or ""),
            }
        )
    if not validated:
        return [], "model returned no applicable source patch"
    return validated, ""


def _apply_patch_items(
    repo_copy: str,
    patch_items: list[dict[str, str]],
) -> tuple[list[str], str, str]:
    diffs: list[str] = []
    applied_paths: list[str] = []
    for item in patch_items:
        path = Path(repo_copy) / item["path"]
        if not path.is_file():
            return [], "", f"source file does not exist: {item['path']}"
        original = path.read_text(encoding="utf-8", errors="replace")
        occurrences = original.count(item["search"])
        if occurrences != 1:
            return [], "", (
                f"search text must occur exactly once in {item['path']}, "
                f"found {occurrences}"
            )
        updated = original.replace(item["search"], item["replace"], 1)
        path.write_text(updated, encoding="utf-8")
        applied_paths.append(item["path"])
        diffs.extend(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{item['path']}",
                tofile=f"b/{item['path']}",
            )
        )
    return applied_paths, "".join(diffs), ""


def _copy_repo(source: str, destination: str) -> None:
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__"),
    )


def validate_surrogate_patch_candidate(
    patch_candidate: SurrogatePatchCandidate,
    retrieved_code: list[RetrievedCode],
    candidate: CandidateTest,
    negative_candidate: CandidateTest | None,
    buggy_repo: str,
    conda_env: str,
    timeout: int,
    no_conda: bool,
) -> tuple[SurrogatePatchCandidate, ExecutionResult | None, ExecutionResult | None]:
    patch_items, error = _validate_patch_items(
        patch_candidate, retrieved_code, candidate.candidate_repo_path
    )
    if error:
        patch_candidate.status = "REJECTED"
        patch_candidate.reason = error
        return patch_candidate, None, None
    temp_dir = tempfile.mkdtemp(prefix="brt3_surrogate_")
    try:
        repo_copy = os.path.join(temp_dir, "repo")
        _copy_repo(buggy_repo, repo_copy)
        applied_paths, diff, error = _apply_patch_items(repo_copy, patch_items)
        if error:
            patch_candidate.status = "APPLY_ERROR"
            patch_candidate.reason = error
            return patch_candidate, None, None
        patch_candidate.applied_paths = applied_paths
        patch_candidate.diff = diff
        patch_candidate.patches = patch_items
        patch_candidate.status = "APPLIED"
        execution = run_command_in_conda(
            candidate.command,
            repo_copy,
            conda_env,
            timeout,
            no_conda,
            None,
            candidate.instance_id,
        )
        negative_execution = None
        if negative_candidate is not None:
            negative_execution = run_command_in_conda(
                negative_candidate.command,
                repo_copy,
                conda_env,
                timeout,
                no_conda,
                None,
                negative_candidate.instance_id,
            )
        return patch_candidate, execution, negative_execution
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_surrogate_patch_loop(
    instance_id: str,
    behavior: BehaviorTarget,
    candidate: CandidateTest,
    retrieved_code: list[RetrievedCode],
    buggy_repo: str,
    buggy_execution: ExecutionResult,
    llm_client: Any,
    output_dir: str,
    conda_env: str,
    timeout: int,
    no_conda: bool,
    max_rounds: int = 3,
    negative_candidate: CandidateTest | None = None,
) -> DualVersionResult:
    effective_code = _effective_surrogate_sources(
        behavior, retrieved_code, buggy_repo
    )
    attempts: list[dict[str, Any]] = []
    latest_execution: ExecutionResult | None = None
    latest_patch: SurrogatePatchCandidate | None = None
    seen_patch_signatures: set[str] = set()
    for round_id in range(max_rounds):
        try:
            patch_candidate = generate_surrogate_patch(
                instance_id,
                behavior,
                candidate,
                effective_code,
                buggy_execution,
                llm_client,
                output_dir,
                buggy_repo,
                round_id,
                attempts,
            )
        except Exception as exc:  # noqa: BLE001
            attempt = {
                "round_id": round_id,
                "status": "GENERATION_ERROR",
                "reason": str(exc),
                "execution": {},
                "negative_execution": {},
                "negative_skip_reason": "surrogate patch generation failed",
            }
            attempts.append(attempt)
            safe_json_dump(
                attempt,
                str(Path(output_dir) / f"surrogate_patch_round_{round_id}.json"),
            )
            continue
        signature = json.dumps(
            patch_candidate.patches, ensure_ascii=False, sort_keys=True
        )
        if signature in seen_patch_signatures:
            patch_candidate.status = "DUPLICATE"
            patch_candidate.reason = "model repeated a previous surrogate patch"
            attempt = patch_candidate.to_dict()
            attempt["execution"] = {}
            attempt["negative_execution"] = {}
            attempt["negative_skip_reason"] = "surrogate patch duplicate; no repo execution"
            attempts.append(attempt)
            safe_json_dump(
                attempt,
                str(Path(output_dir) / f"surrogate_patch_round_{round_id}.json"),
            )
            continue
        seen_patch_signatures.add(signature)
        patch_candidate, execution, negative_execution = validate_surrogate_patch_candidate(
            patch_candidate,
            effective_code,
            candidate,
            negative_candidate,
            buggy_repo,
            conda_env,
            timeout,
            no_conda,
        )
        latest_patch = patch_candidate
        latest_execution = execution
        attempt = patch_candidate.to_dict()
        attempt["execution"] = execution.to_dict() if execution else {}
        attempt["negative_execution"] = (
            negative_execution.to_dict() if negative_execution else {}
        )
        if negative_execution is None:
            if negative_candidate is None:
                attempt["negative_skip_reason"] = "negative control unavailable or invalid"
            elif execution is None:
                attempt["negative_skip_reason"] = "surrogate patch was not executable"
            else:
                attempt["negative_skip_reason"] = "negative execution not run"
        attempts.append(attempt)
        safe_json_dump(
            attempt,
            str(Path(output_dir) / f"surrogate_patch_round_{round_id}.json"),
        )
        if patch_candidate.diff:
            write_text(
                str(Path(output_dir) / f"surrogate_patch_round_{round_id}.diff"),
                patch_candidate.diff,
            )
        if execution and buggy_execution.returncode != 0 and execution.returncode == 0:
            result = DualVersionResult(
                instance_id=instance_id,
                mode="surrogate_patch",
                buggy_execution=buggy_execution.to_dict(),
                patched_execution=execution.to_dict(),
                status="SURROGATE_F2P_SUCCESS",
                notes="BRT fails on buggy source and passes after an independently generated surrogate source patch.",
                surrogate_patch=patch_candidate.to_dict(),
                attempts=attempts,
            )
            result.save_json(str(Path(output_dir) / "dual_version_result.json"))
            return result
    result = DualVersionResult(
        instance_id=instance_id,
        mode="surrogate_patch",
        buggy_execution=buggy_execution.to_dict(),
        patched_execution=latest_execution.to_dict() if latest_execution else {},
        status="SURROGATE_PATCH_UNRESOLVED",
        notes="No generated surrogate source patch made the current BRT pass.",
        surrogate_patch=latest_patch.to_dict() if latest_patch else {},
        attempts=attempts,
    )
    result.save_json(str(Path(output_dir) / "dual_version_result.json"))
    return result


def apply_and_validate_surrogate_patch(
    instance_id: str, *args: Any, **kwargs: Any
) -> DualVersionResult:
    """Backward-compatible alias for callers using the old stub name."""

    return run_surrogate_patch_loop(instance_id, *args, **kwargs)
