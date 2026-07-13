"""Observation-driven oracle rebinding that preserves setup and trigger code."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..execution.executor import run_command_in_conda
from ..retrieval.icore_runtime import first_test_selector, icore_test_command
from ..core.prompts import (
    OBSERVATION_ORACLE_PROBE_PROMPT,
    OBSERVATION_ORACLE_REBIND_PROMPT,
    OBSERVATION_ORACLE_SYSTEM_PROMPT,
)
from ..core.schema import BehaviorTarget, CandidateTest, ObservationReport, ProtocolRecovery
from ..validation.semantic_guard import audit_candidate
from ..core.utils import clean_code_block, safe_json_dump, truncate_text, write_text


ORACLE_TYPES = {
    "NO_EXCEPTION", "EXCEPTION_TYPE", "WARNING", "LOGGING", "EXACT_VALUE",
    "TYPE_OR_SHAPE", "STATE_CHANGE", "SQL_VALIDITY", "SERIALIZATION",
    "RENDER_OUTPUT", "ORDERING", "EQUAL", "NOT_EQUAL", "CONTAINS",
    "NOT_CONTAINS", "TYPE_IS", "SHAPE_IS", "STATE_EQUALS",
    "ORDER_BEFORE", "WARNING_TYPE", "LOG_CONTAINS",
    "SERIALIZATION_PROPERTY",
}

MAX_ORACLE_PROMPT_STRING = 4_000
MAX_ORACLE_EXECUTION_LOG = 24_000
MAX_ORACLE_OBSERVATIONS_JSON = 20_000


def _compact_prompt_value(value: Any, depth: int = 0) -> Any:
    """Bound runtime observations before they are sent back to the model."""
    if depth >= 6:
        return truncate_text(str(value), MAX_ORACLE_PROMPT_STRING)
    if isinstance(value, str):
        return truncate_text(value, MAX_ORACLE_PROMPT_STRING)
    if isinstance(value, dict):
        items = list(value.items())
        compact = {
            str(key): _compact_prompt_value(item, depth + 1)
            for key, item in items[:40]
        }
        if len(items) > 40:
            compact["__truncated_keys__"] = len(items) - 40
        return compact
    if isinstance(value, (list, tuple)):
        compact = [_compact_prompt_value(item, depth + 1) for item in value[:10]]
        if len(value) > 10:
            compact.append({"__truncated_items__": len(value) - 10})
        return compact
    return value


def _prompt_report(report: ObservationReport) -> dict[str, Any]:
    """Keep public observations and bounded execution evidence for rebinding."""
    raw = report.to_dict()
    observations = _compact_prompt_value(raw.get("observations") or {})
    observations_json = json.dumps(observations, ensure_ascii=False)
    if len(observations_json) > MAX_ORACLE_OBSERVATIONS_JSON:
        observations = {
            "truncated_json": truncate_text(
                observations_json, MAX_ORACLE_OBSERVATIONS_JSON
            )
        }
    execution = raw.get("execution") if isinstance(raw.get("execution"), dict) else {}
    bounded_execution = {
        key: _compact_prompt_value(execution.get(key))
        for key in (
            "command", "returncode", "duration", "timeout", "status",
            "error_reason", "stdout", "stderr",
        )
        if key in execution
    }
    return {
        "instance_id": raw.get("instance_id"),
        "status": raw.get("status"),
        "observations": observations,
        "execution": bounded_execution,
        "raw_output": truncate_text(
            str(raw.get("raw_output") or ""), MAX_ORACLE_EXECUTION_LOG
        ),
    }


def _extract_observation(text: str) -> dict[str, Any]:
    blocks = re.findall(r"BRT_OBS_START\s*(.*?)\s*BRT_OBS_END", text, re.S)
    for block in reversed(blocks):
        try:
            value = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        return value if isinstance(value, dict) else {"value": value}
    return {}


def _oracle_type(code: str) -> str:
    match = re.search(r"BRT_ORACLE_TYPE:\s*([A-Z_]+)", code)
    return match.group(1) if match and match.group(1) in ORACLE_TYPES else ""


def rebind_observation_oracle(
    behavior: BehaviorTarget,
    protocol: ProtocolRecovery | None,
    candidate: CandidateTest,
    execution_log: str,
    llm_client: Any,
    output_dir: str,
    buggy_repo: str,
    conda_env: str,
    timeout: int,
    no_conda: bool,
    repo: str,
    version: str,
    round_id: int,
    contrastive_context: dict[str, Any] | None = None,
    enable_contrastive_observation: bool = True,
) -> tuple[CandidateTest, ObservationReport, str]:
    protocol_json = json.dumps(protocol.to_dict() if protocol else {}, ensure_ascii=False)
    behavior_json = json.dumps(behavior.to_dict(), ensure_ascii=False)
    probe_prompt = OBSERVATION_ORACLE_PROBE_PROMPT.format(
        behavior_json=behavior_json,
        protocol_json=protocol_json,
        candidate_code=candidate.code,
    )
    write_text(str(Path(output_dir) / "prompts" / f"oracle_probe_round_{round_id}.txt"), OBSERVATION_ORACLE_SYSTEM_PROMPT + "\n\n" + probe_prompt)
    probe_response = llm_client.chat(OBSERVATION_ORACLE_SYSTEM_PROMPT, probe_prompt)
    write_text(str(Path(output_dir) / "responses" / f"oracle_probe_round_{round_id}.txt"), probe_response)
    probe_code = clean_code_block(probe_response).strip() + "\n"
    probe_output_path = Path(output_dir) / f"oracle_round_{round_id}_probe.py"
    write_text(str(probe_output_path), probe_code)
    repo_probe = Path(candidate.candidate_file_path).with_name(Path(candidate.candidate_file_path).stem + f"_oracle_probe_{round_id}.py")
    write_text(str(repo_probe), probe_code)
    rel_probe = str(Path(candidate.candidate_repo_path).with_name(repo_probe.name))
    command = icore_test_command(repo, version, rel_probe, first_test_selector(probe_code))
    if "pytest" in command:
        command += " -s"
    execution = run_command_in_conda(command, buggy_repo, conda_env, timeout, no_conda, behavior, behavior.instance_id)
    observations = _extract_observation(execution.stdout + "\n" + execution.stderr)
    report = ObservationReport(
        instance_id=behavior.instance_id,
        probe_code=probe_code,
        probe_file_path=str(probe_output_path),
        execution=execution.to_dict(),
        observations=observations,
        raw_output=execution.stdout + "\n" + execution.stderr,
        status=execution.status,
    )
    safe_json_dump(report.to_dict(), str(Path(output_dir) / f"oracle_round_{round_id}_observation.json"))
    prompt_report = _prompt_report(report)
    contrastive_observation = contrastive_context or {}
    if not enable_contrastive_observation:
        contrastive_observation = {}
    if contrastive_observation:
        safe_json_dump(
            contrastive_observation,
            str(Path(output_dir) / "contrastive_observation.json"),
        )
    rebind_prompt = OBSERVATION_ORACLE_REBIND_PROMPT.format(
        behavior_json=behavior_json,
        protocol_json=protocol_json,
        observation_json=json.dumps(prompt_report, ensure_ascii=False),
        contrastive_observation_json=json.dumps(
            _compact_prompt_value(contrastive_observation), ensure_ascii=False
        ),
        candidate_code=candidate.code,
        execution_log=truncate_text(execution_log, MAX_ORACLE_EXECUTION_LOG),
    )
    write_text(str(Path(output_dir) / "prompts" / f"oracle_rebind_round_{round_id}.txt"), OBSERVATION_ORACLE_SYSTEM_PROMPT + "\n\n" + rebind_prompt)
    rebuilt_response = llm_client.chat(OBSERVATION_ORACLE_SYSTEM_PROMPT, rebind_prompt)
    write_text(str(Path(output_dir) / "responses" / f"oracle_rebind_round_{round_id}.txt"), rebuilt_response)
    rebuilt = clean_code_block(rebuilt_response).strip() + "\n"
    problem = audit_candidate(behavior, rebuilt)
    if problem:
        retry = rebind_prompt + "\n\n返回代码未通过语义校验：" + problem + "\n请修复后仍只输出完整 Python 文件。"
        rebuilt_response = llm_client.chat(OBSERVATION_ORACLE_SYSTEM_PROMPT, retry)
        rebuilt = clean_code_block(rebuilt_response).strip() + "\n"
        write_text(str(Path(output_dir) / "responses" / f"oracle_rebind_round_{round_id}_retry.txt"), rebuilt_response)
    write_text(str(Path(output_dir) / f"oracle_round_{round_id}_rebuilt_test.py"), rebuilt)
    write_text(candidate.candidate_file_path, rebuilt)
    new_candidate = CandidateTest(
        instance_id=candidate.instance_id,
        round_id=round_id,
        code=rebuilt,
        candidate_file_path=candidate.candidate_file_path,
        candidate_repo_path=candidate.candidate_repo_path,
        pytest_nodeid=candidate.pytest_nodeid,
        command=candidate.command,
        prompt_path=str(Path(output_dir) / "prompts" / f"oracle_rebind_round_{round_id}.txt"),
        response_path=str(Path(output_dir) / "responses" / f"oracle_rebind_round_{round_id}.txt"),
        lineage={
            "origin": (
                "contrastive_observation_oracle"
                if contrastive_observation
                else "buggy_observation_oracle"
            ),
            "parent_candidate_id": str(candidate.round_id),
            "seed_id": str((candidate.lineage or {}).get("seed_id") or ""),
            "generation_round": round_id,
            "repair_round": round_id,
            "observation_context_used": (
                "contrastive" if contrastive_observation else "buggy_only"
            ),
            "counterfactual_evidence_id": str(
                (contrastive_observation.get("counterfactual_evidence") or {}).get("instance_id")
                if isinstance(contrastive_observation, dict)
                else ""
            ),
            "negative_control_id": "",
        },
    )
    return new_candidate, report, _oracle_type(rebuilt)
