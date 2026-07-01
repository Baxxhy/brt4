"""Observation probe and assert synthesis."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..runtime.executor import run_command_in_conda
from ..runtime.icore_runtime import first_test_selector, icore_test_command
from ..core.prompts import ASSERT_SYNTHESIS_SYSTEM_PROMPT, ASSERT_SYNTHESIS_USER_PROMPT, OBSERVATION_PROBE_SYSTEM_PROMPT, OBSERVATION_PROBE_USER_PROMPT
from ..core.schema import BehaviorTarget, CandidateTest, ObservationReport
from ..core.utils import clean_code_block, safe_json_dump, truncate_text, write_text


MAX_ORACLE_EXECUTION_LOG = 24_000
MAX_ORACLE_OBSERVATION_JSON = 48_000


def _extract_observation(stdout: str) -> dict[str, Any]:
    matches = re.findall(
        r"BRT_OBS_START\s*(.*?)\s*BRT_OBS_END", stdout or "", re.S
    )
    merged: dict[str, Any] = {}
    raw_values: list[str] = []
    for value in matches:
        value = value.strip()
        try:
            parsed = json.loads(value)
        except Exception:  # noqa: BLE001
            raw_values.append(value)
            continue
        if isinstance(parsed, dict):
            merged.update(parsed)
        else:
            merged["value"] = parsed
    if raw_values:
        merged["raw"] = raw_values[-1]
    return merged


def run_observation_probe(
    behavior: BehaviorTarget,
    candidate: CandidateTest,
    llm_client: Any,
    output_dir: str,
    buggy_repo: str,
    conda_env: str = "",
    timeout: int = 120,
    no_conda: bool = False,
    repo: str = "",
    version: str = "",
) -> ObservationReport:
    user_prompt = OBSERVATION_PROBE_USER_PROMPT.format(
        behavior_json=json.dumps(behavior.to_dict(), ensure_ascii=False),
        candidate_code=candidate.code,
    )
    write_text(str(Path(output_dir) / "prompts" / "observation_probe.txt"), OBSERVATION_PROBE_SYSTEM_PROMPT + "\n\n" + user_prompt)
    response = llm_client.chat(OBSERVATION_PROBE_SYSTEM_PROMPT, user_prompt)
    write_text(str(Path(output_dir) / "responses" / "observation_probe.txt"), response)
    probe_code = clean_code_block(response)
    probe_path = str(Path(candidate.candidate_file_path).with_name(Path(candidate.candidate_file_path).stem + "_probe.py"))
    write_text(probe_path, probe_code)
    rel_probe = str(Path(candidate.candidate_repo_path).with_name(Path(candidate.candidate_repo_path).stem + "_probe.py"))
    if repo:
        command = icore_test_command(
            repo, version, rel_probe, first_test_selector(probe_code)
        )
        if "pytest" in command:
            command += " -s"
    else:
        command = f"python -m pytest {rel_probe} -q -s"
    execution = run_command_in_conda(command, buggy_repo, conda_env, timeout, no_conda, behavior, behavior.instance_id)
    observations = _extract_observation(execution.stdout)
    report = ObservationReport(
        instance_id=behavior.instance_id,
        probe_code=probe_code,
        probe_file_path=probe_path,
        execution=execution.to_dict(),
        observations=observations,
        raw_output=execution.stdout,
        status=execution.status,
    )
    safe_json_dump(report.to_dict(), str(Path(output_dir) / "observation.json"))
    return report


def synthesize_oracle(
    behavior: BehaviorTarget,
    candidate: CandidateTest,
    observation: ObservationReport | None,
    execution_log: str,
    llm_client: Any,
    output_dir: str,
) -> str:
    user_prompt = ASSERT_SYNTHESIS_USER_PROMPT.format(
        behavior_json=json.dumps(behavior.to_dict(), ensure_ascii=False),
        candidate_code=candidate.code,
        observation_json=truncate_text(
            json.dumps(observation.to_dict() if observation else {}, ensure_ascii=False),
            MAX_ORACLE_OBSERVATION_JSON,
        ),
        execution_log=truncate_text(execution_log, MAX_ORACLE_EXECUTION_LOG),
    )
    write_text(str(Path(output_dir) / "prompts" / "assert_synthesis.txt"), ASSERT_SYNTHESIS_SYSTEM_PROMPT + "\n\n" + user_prompt)
    response = llm_client.chat(ASSERT_SYNTHESIS_SYSTEM_PROMPT, user_prompt)
    write_text(str(Path(output_dir) / "responses" / "assert_synthesis.txt"), response)
    final_code = clean_code_block(response)
    write_text(str(Path(output_dir) / "final_test.py"), final_code)
    write_text(candidate.candidate_file_path, final_code)
    return final_code
