"""Main feedback loop for BRT3."""

from __future__ import annotations

import copy
import json
import fcntl
import os
import shlex
import shutil
import subprocess
import traceback
import re
from pathlib import Path
from typing import Any

from ..execution.executor import run_command_in_conda
from ..generation.generator import (
    format_effective_source_context,
    generate_candidate,
    repair_candidate,
)
from ..context.host_context import build_host_context, rank_related_tests, select_related_test
from ..context.protocol_recovery import audit_recovered_protocol, recover_test_protocol
from ..mutation.seed_mutator import build_mutation_plan
from ..generation.observation_oracle import rebind_observation_oracle
from ..validation.strict_semantic_verifier import verify_strict_semantics
from ..retrieval.icore_runtime import (
    dump_spec,
    ensure_icore_environment,
    env_name_for,
    env_lock_path,
    icore_setup_command,
    icore_test_command,
    first_test_selector,
    make_instance_spec,
)
from ..generation.oracle import run_observation_probe, synthesize_oracle
from ..execution.patch_utils import run_surrogate_patch_loop
from ..core.schema import (
    CandidateCheckpoint,
    DualVersionResult,
    ExecutionResult,
    FinalResult,
    InstanceContext,
    VerifierDecision,
)
from ..core.utils import ensure_dir, safe_json_dump, write_text
from ..validation.verifier import verify_buggy_only
from ..validation.oracle_risk import assess_oracle_risk, assess_surrogate_risk


DEFAULT_BEHAVIOR_CACHE_DIRS = [
    Path(__file__).resolve().parents[1]
    / "results"
    / "runs"
    / "run_brt4_full276_20260705_141938"
    / "generation",
    Path(__file__).resolve().parents[1]
    / "results"
    / "archive"
    / "delete_pending_20260706_001747"
    / "outputs_brt3_flow_276_20260619_151437",
]


def _load_cached_behavior(context: InstanceContext, output_dir: str) -> Any:
    from ..issue.issue_rewriter import behavior_from_dict
    from ..core.utils import safe_json_load

    local_path = Path(output_dir) / "behavior_target.json"
    raw_roots = os.environ.get("BRT4_BEHAVIOR_CACHE_DIR", "")
    cache_roots = [
        Path(item)
        for item in raw_roots.split(os.pathsep)
        if item.strip()
    ] or DEFAULT_BEHAVIOR_CACHE_DIRS
    candidates = [local_path]
    candidates.extend(
        root / context.instance_id / "behavior_target.json"
        for root in cache_roots
    )
    for path in candidates:
        if path.is_file():
            behavior = behavior_from_dict(context.instance_id, safe_json_load(path))
            if path != local_path:
                behavior.save_json(str(local_path))
            return behavior
    raise FileNotFoundError(
        "missing cached behavior_target.json; generation is configured not to "
        f"rerun issue rewrite for {context.instance_id}. Checked: "
        + ", ".join(str(path) for path in candidates)
    )


def _run_local(command: str, cwd: str, timeout: int = 300) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "command": command,
            "cwd": cwd,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "cwd": cwd,
            "returncode": 124,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace"),
            "stderr": exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace"),
            "timeout": True,
        }


def _refresh_candidate_command(context: InstanceContext, candidate: Any) -> None:
    candidate.command = icore_test_command(
        context.repo,
        str(context.metadata.get("version") or ""),
        candidate.candidate_repo_path,
        first_test_selector(candidate.code),
    )


def _checkpoint_score(
    execution: ExecutionResult,
    decision: VerifierDecision,
    dual: DualVersionResult | None,
) -> tuple[int, str]:
    if dual and dual.status in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"}:
        return 300, "buggy fail and independently generated surrogate patch pass"
    if decision.decision == "accept" and execution.returncode != 0:
        return 200, "buggy fail accepted by semantic verifier"
    if execution.returncode != 0 and execution.status not in {
        "SETUP_ERROR",
        "SYNTAX_ERROR",
        "COLLECT_ERROR",
        "TIMEOUT",
    }:
        return 100, "executable buggy failure not accepted as issue-aligned"
    if execution.returncode == 0:
        return 10, "test passes on buggy source"
    return 0, f"non-executable candidate: {execution.status}"


def _risk_adjusted_score(
    base_score: int,
    oracle_risk: dict[str, Any],
    surrogate_risk: dict[str, Any],
) -> tuple[int, list[str]]:
    penalty = 0
    reasons: list[str] = []
    if oracle_risk.get("level") == "high":
        penalty += 70 if base_score >= 300 else 40
        reasons.extend(str(item) for item in oracle_risk.get("reasons") or [])
    elif oracle_risk.get("level") == "medium":
        penalty += 15
        reasons.extend(str(item) for item in oracle_risk.get("reasons") or [])
    if surrogate_risk.get("level") == "high":
        penalty += 30
        reasons.extend(str(item) for item in surrogate_risk.get("reasons") or [])
    elif surrogate_risk.get("level") == "medium":
        penalty += 10
        reasons.extend(str(item) for item in surrogate_risk.get("reasons") or [])
    return max(0, base_score - penalty), list(dict.fromkeys(reasons))


def _save_checkpoint(
    output_dir: str,
    attempt_id: int,
    candidate: Any,
    execution: ExecutionResult,
    decision: VerifierDecision,
    dual: DualVersionResult | None,
    behavior: Any | None = None,
    issue_text: str = "",
    retrieved_paths: set[str] | None = None,
) -> CandidateCheckpoint:
    checkpoint_dir = ensure_dir(Path(output_dir) / "checkpoints")
    code_path = str(Path(checkpoint_dir) / f"candidate_attempt_{attempt_id}.py")
    write_text(code_path, candidate.code)
    score, reason = _checkpoint_score(execution, decision, dual)
    oracle_risk = (
        assess_oracle_risk(
            candidate.code,
            behavior,
            issue_text,
            execution.stdout + "\n" + execution.stderr,
        )
        if behavior is not None
        else {"level": "low", "reasons": [], "signals": {}}
    )
    surrogate_risk = assess_surrogate_risk(dual, oracle_risk, retrieved_paths)
    adjusted_score, penalty_reasons = _risk_adjusted_score(
        score, oracle_risk, surrogate_risk
    )
    checkpoint = CandidateCheckpoint(
        instance_id=candidate.instance_id,
        round_id=attempt_id,
        code_path=code_path,
        score=adjusted_score,
        reason=reason,
        oracle_risk=oracle_risk,
        surrogate_risk=surrogate_risk,
        selector_score_before_risk=score,
        selector_score_after_risk=adjusted_score,
        selector_penalty_reasons=penalty_reasons,
        execution=execution.to_dict(),
        verifier=decision.to_dict(),
        surrogate=dual.to_dict() if dual else {},
    )
    checkpoint.save_json(
        str(Path(checkpoint_dir) / f"candidate_attempt_{attempt_id}.json")
    )
    return checkpoint


def _copy_if_exists(source_dir: Path, target_dir: Path, name: str) -> None:
    source = source_dir / name
    if source.exists():
        target = target_dir / name
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if source.is_dir():
            try:
                os.symlink(source, target, target_is_directory=True)
            except OSError:
                shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)


def _best_checkpoint_from_summary(seed_dir: Path) -> dict[str, Any]:
    ranking_path = seed_dir / "candidate_ranking.json"
    if not ranking_path.is_file():
        return {}
    try:
        ranking = json.loads(ranking_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    checkpoints = ranking.get("checkpoints")
    selected = ranking.get("selected_attempt")
    if not isinstance(checkpoints, list):
        return {}
    for item in checkpoints:
        if isinstance(item, dict) and item.get("round_id") == selected:
            return item
    return checkpoints[-1] if checkpoints else {}


def _seed_result_score(summary: dict[str, Any], checkpoint: dict[str, Any]) -> int:
    status = str(summary.get("status") or "")
    dual = summary.get("dual_version_result") if isinstance(summary.get("dual_version_result"), dict) else {}
    if status in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"} or dual.get("status") in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"}:
        return 300
    if status == "ISSUE_ALIGNED_FAIL" or summary.get("strict_failure_class") == "issue_aligned":
        return 200
    if checkpoint:
        return int(checkpoint.get("score") or 0)
    buggy = summary.get("buggy_execution") if isinstance(summary.get("buggy_execution"), dict) else {}
    if buggy.get("returncode") not in {None, 0} and buggy.get("status") not in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT"}:
        return 100
    if buggy.get("returncode") == 0 or status in {"PASS", "BUGGY_PASS"}:
        return 10
    return 0


def _should_try_next_seed(
    summary: dict[str, Any],
    checkpoint: dict[str, Any],
    has_next: bool,
) -> tuple[bool, str]:
    if not has_next:
        return False, "no next seed"
    status = str(summary.get("status") or "")
    dual = summary.get("dual_version_result") if isinstance(summary.get("dual_version_result"), dict) else {}
    buggy = summary.get("buggy_execution") if isinstance(summary.get("buggy_execution"), dict) else {}
    verifier = checkpoint.get("verifier") if isinstance(checkpoint.get("verifier"), dict) else {}
    oracle_rebound = bool(summary.get("oracle_rebound"))
    if status in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"} or dual.get("status") in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"}:
        return False, "surrogate success"
    if status == "ISSUE_ALIGNED_FAIL" or summary.get("strict_failure_class") == "issue_aligned":
        return False, "verifier accepted issue-aligned buggy failure"
    if oracle_rebound and buggy.get("returncode") not in {None, 0}:
        return False, "observation oracle rebound and buggy fails"
    if status in {"PASS", "BUGGY_PASS"} or dual.get("status") == "BUGGY_PASS":
        return True, "buggy source passed; trigger likely missed"
    if status == "UNRELATED_FAIL" or buggy.get("status") == "UNRELATED_FAIL":
        return True, "buggy failure is unrelated"
    if status in {"ENV_UNRESOLVED", "SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR"}:
        return True, f"seed scaffold remained {status}"
    if verifier.get("decision") == "repair_trigger":
        return True, "trigger repair budget exhausted"
    strict = summary.get("strict_verifier_decision")
    if strict == "repair_trigger" and summary.get("strict_failure_class") == "target_not_hit":
        return True, "strict verifier target_hit=false"
    if int(checkpoint.get("score") or 0) < 100:
        return True, "best candidate score below executable buggy fail"
    return False, "current seed is competitive"


def _missing_dependency_hint(log: str) -> str:
    patterns = [
        r"No module named ['\"]([^'\"]+)['\"]",
        r"requires the ([A-Za-z0-9_.-]+) python package",
    ]
    for pattern in patterns:
        match = re.search(pattern, log, flags=re.IGNORECASE)
        if match:
            return match.group(1).split(".", 1)[0]
    return ""


def _find_declared_requirement(repo_path: str, module_hint: str) -> str:
    if not module_hint:
        return ""
    token = re.sub(r"\d+$", "", module_hint.lower().replace("_", "-"))
    root = Path(repo_path)
    candidates = sorted(root.glob("requirements*.txt"))
    candidates += sorted(root.glob("requirements/*.txt"))
    candidates += sorted(root.glob("*/requirements*.txt"))
    candidates += sorted(root.glob("*/*/requirements*.txt"))
    for path in candidates:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(("#", "-", "git+", "http://", "https://")):
                continue
            normalized = line.lower().replace("_", "-")
            if token and token in normalized:
                return line
    return ""


def _recover_declared_dependency(
    context: InstanceContext,
    execution: ExecutionResult,
    conda_env: str,
    timeout: int,
    no_conda: bool,
    output_dir: str,
    round_id: int,
) -> bool:
    log = execution.stdout + "\n" + execution.stderr
    hint = _missing_dependency_hint(log)
    requirement = _find_declared_requirement(context.buggy_repo_path, hint)
    if not requirement:
        return False
    install_result = run_command_in_conda(
        f"python -m pip install {shlex.quote(requirement)}",
        context.buggy_repo_path,
        conda_env,
        timeout,
        no_conda,
        None,
        context.instance_id,
    )
    safe_json_dump(
        {
            "module_hint": hint,
            "requirement": requirement,
            "execution": install_result.to_dict(),
        },
        str(Path(output_dir) / f"dependency_recovery_round_{round_id}.json"),
    )
    return install_result.returncode == 0


def prepare_instance_worktree(
    context: InstanceContext,
    output_dir: str,
    conda_env: str,
    timeout: int,
    no_conda: bool,
) -> tuple[str, dict[str, Any]]:
    source_repo = context.buggy_repo_path
    base_commit = context.base_commit
    if not source_repo or not base_commit:
        return source_repo, {"status": "SKIPPED", "reason": "missing source repo or base_commit", "repo_path": source_repo}
    worktree = Path(output_dir) / "worktree"
    if worktree.exists():
        _run_local(
            f"git worktree remove --force {shlex.quote(str(worktree))}",
            source_repo,
            timeout=300,
        )
        if worktree.exists():
            shutil.rmtree(worktree)
    ensure_dir(worktree.parent)
    add_cmd = f"git worktree add --force --detach {shlex.quote(str(worktree))} {shlex.quote(base_commit)}"
    add_result = _run_local(add_cmd, source_repo, timeout=300)
    if add_result["returncode"] != 0:
        clone_cmd = f"git clone --shared {shlex.quote(source_repo)} {shlex.quote(str(worktree))}"
        clone_result = _run_local(clone_cmd, str(Path(output_dir)), timeout=600)
        checkout_result = _run_local(f"git checkout --force {shlex.quote(base_commit)}", str(worktree), timeout=300) if clone_result["returncode"] == 0 else {}
        add_result = {"worktree_add": add_result, "clone": clone_result, "checkout": checkout_result}
        if clone_result["returncode"] != 0 or checkout_result.get("returncode") != 0:
            return str(worktree), {"status": "WORKTREE_ERROR", "details": add_result, "repo_path": str(worktree)}
    submodule_result = _run_local(
        "git submodule update --init --recursive",
        str(worktree),
        timeout=600,
    )
    cached_astropy_helpers = Path(source_repo) / "astropy_helpers"
    worktree_astropy_helpers = worktree / "astropy_helpers"
    if (
        context.repo == "astropy/astropy"
        and cached_astropy_helpers.is_dir()
        and not worktree_astropy_helpers.exists()
    ):
        shutil.copytree(
            cached_astropy_helpers,
            worktree_astropy_helpers,
            symlinks=True,
        )
    version = str(context.metadata.get("version") or "")
    environment_setup_commit = str(
        context.metadata.get("environment_setup_commit") or base_commit
    )
    spec = make_instance_spec(
        context.instance_id,
        context.repo,
        version,
        base_commit,
        environment_setup_commit,
    )
    dump_spec(spec, str(Path(output_dir) / "icore_exec_spec.json"))
    resolved_env = conda_env or env_name_for(context.repo, version)
    env_result = ensure_icore_environment(
        spec, resolved_env, str(worktree), timeout
    )
    if env_result.get("returncode") != 0:
        return str(worktree), {
            "status": "ENV_CREATE_ERROR",
            "source_repo": source_repo,
            "repo_path": str(worktree),
            "base_commit": base_commit,
            "env_name": resolved_env,
            "environment": env_result,
            "worktree": add_result,
        }
    setup = icore_setup_command(spec, str(worktree))
    setup_lock = env_lock_path(resolved_env, "project_setup")
    setup_lock.parent.mkdir(parents=True, exist_ok=True)
    with open(setup_lock, "w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        setup_result = run_command_in_conda(
            setup,
            str(worktree),
            resolved_env,
            timeout,
            no_conda,
            None,
            context.instance_id,
        )
        setup_log = setup_result.stdout + "\n" + setup_result.stderr
        if (
            setup_result.returncode != 0
            and "missing the 'build_editable' hook" in setup_log
            and " -e ." in setup
        ):
            fallback_setup = setup.replace(" -e .", " .")
            fallback_result = run_command_in_conda(
                fallback_setup,
                str(worktree),
                resolved_env,
                timeout,
                no_conda,
                None,
                context.instance_id,
            )
            if fallback_result.returncode == 0:
                setup = fallback_setup
                setup_result = fallback_result
        setup_log = setup_result.stdout + "\n" + setup_result.stderr
        if (
            setup_result.returncode != 0
            and (
                "uninstall-no-record-file" in setup_log
                or (
                    "egg-link" in setup_log.lower()
                    and "does not match installed location" in setup_log.lower()
                )
            )
            and "python -m pip install" in setup
            and " -e ." in setup
        ):
            fallback_setup = re.sub(
                r"python -m pip install(?![^&]*--ignore-installed)([^&]*\s-e\s+\.)",
                r"python -m pip install --ignore-installed --no-deps\1",
                setup,
                count=1,
            )
            fallback_result = run_command_in_conda(
                fallback_setup,
                str(worktree),
                resolved_env,
                timeout,
                no_conda,
                None,
                context.instance_id,
            )
            if fallback_result.returncode == 0:
                setup = fallback_setup
                setup_result = fallback_result
    status = "PASS" if setup_result.returncode == 0 else "SETUP_ERROR"
    return str(worktree), {
        "status": status,
        "source_repo": source_repo,
        "repo_path": str(worktree),
        "base_commit": base_commit,
        "environment_setup_commit": environment_setup_commit,
        "env_name": resolved_env,
        "environment": env_result,
        "worktree": add_result,
        "submodule": submodule_result,
        "setup_command": setup,
        "setup_execution": setup_result.to_dict(),
    }


def run_instance_pipeline(
    context: InstanceContext,
    llm_client: Any,
    output_dir: str,
    conda_env: str = "",
    timeout: int = 120,
    no_conda: bool = False,
    max_feedback_rounds: int = 3,
    max_env_rounds: int | None = None,
    max_brt_rounds: int | None = None,
    max_patch_rounds: int = 3,
    validation_mode: str = "buggy_only",
    patched_repo_base: str = "",
    patch_file: str = "",
    generate_only: bool = False,
    enable_protocol_recovery: bool = True,
    enable_seed_mutation: bool = True,
    enable_observation_oracle: bool = True,
    enable_strict_semantic_verifier: bool = True,
    _adaptive_disabled: bool = False,
    _forced_seed_index: int | None = None,
    _prepared_repo_path: str = "",
    _prepare_meta: dict[str, Any] | None = None,
) -> FinalResult:
    ensure_dir(output_dir)
    ensure_dir(Path(output_dir) / "prompts")
    ensure_dir(Path(output_dir) / "responses")
    ensure_dir(Path(output_dir) / "logs")
    try:
        if (
            not _adaptive_disabled
            and not generate_only
            and enable_protocol_recovery
            and _forced_seed_index is None
        ):
            behavior = _load_cached_behavior(context, output_dir)
            ranked_tests = rank_related_tests(context.retrieved_tests, behavior)
            if not ranked_tests:
                ranked_tests = [select_related_test(context.retrieved_tests, behavior)]
            ranked_tests = [seed for seed in ranked_tests if seed is not None]
            seeds_to_try = ranked_tests[:3] or []
            if not seeds_to_try:
                return run_instance_pipeline(
                    context,
                    llm_client,
                    output_dir,
                    conda_env,
                    timeout,
                    no_conda,
                    max_feedback_rounds,
                    max_env_rounds,
                    max_brt_rounds,
                    max_patch_rounds,
                    validation_mode,
                    patched_repo_base,
                    patch_file,
                    generate_only,
                    enable_protocol_recovery,
                    enable_seed_mutation,
                    enable_observation_oracle,
                    enable_strict_semantic_verifier,
                    _adaptive_disabled=True,
                    _forced_seed_index=None,
                )
            prepared_repo_path = ""
            prepared_meta: dict[str, Any] | None = None
            if not generate_only:
                prepared_repo_path, prepared_meta = prepare_instance_worktree(
                    context, output_dir, conda_env, timeout, no_conda
                )
                context.buggy_repo_path = prepared_repo_path
                safe_json_dump(
                    prepared_meta,
                    str(Path(output_dir) / "repo_prepare.json"),
                )
                if prepared_meta.get("status") in {
                    "WORKTREE_ERROR",
                    "ENV_CREATE_ERROR",
                    "SETUP_ERROR",
                }:
                    result = FinalResult(
                        instance_id=context.instance_id,
                        status="SETUP_ERROR",
                        final_test_path="",
                        rounds_used=0,
                        buggy_execution=prepared_meta.get("setup_execution", {}),
                        dual_version_result={"mode": validation_mode, "status": "SKIPPED"},
                        behavior_target=behavior.to_dict(),
                        host_context={},
                        observation_report={},
                        notes="repository worktree/setup failed before BRT generation",
                        protocol_recovery_enabled=enable_protocol_recovery,
                        seed_mutation_enabled=enable_seed_mutation,
                        observation_oracle_enabled=enable_observation_oracle,
                        strict_verifier_enabled=enable_strict_semantic_verifier,
                        final_reason="repository worktree/setup failed before BRT generation",
                        seed_mode="adaptive_top3",
                    )
                    result.save_json(str(Path(output_dir) / "summary.json"))
                    return result
            seed_root = Path(output_dir) / "seed_candidates"
            ensure_dir(seed_root)
            attempts: list[dict[str, Any]] = []
            switch_reasons: list[str] = []
            best: tuple[int, int, int, Path, dict[str, Any], dict[str, Any]] | None = None
            for seed_index, seed in enumerate(seeds_to_try):
                seed_dir = seed_root / f"seed_{seed_index}"
                ensure_dir(seed_dir)
                behavior.save_json(str(seed_dir / "behavior_target.json"))
                seed_context = copy.deepcopy(context)
                result = run_instance_pipeline(
                    seed_context,
                    llm_client,
                    str(seed_dir),
                    conda_env,
                    timeout,
                    no_conda,
                    max_feedback_rounds,
                    max_env_rounds,
                    max_brt_rounds,
                    max_patch_rounds,
                    validation_mode,
                    patched_repo_base,
                    patch_file,
                    generate_only,
                    enable_protocol_recovery,
                    enable_seed_mutation,
                    enable_observation_oracle,
                    enable_strict_semantic_verifier,
                    _adaptive_disabled=True,
                    _forced_seed_index=seed_index,
                    _prepared_repo_path=prepared_repo_path,
                    _prepare_meta=prepared_meta,
                )
                summary_path = seed_dir / "summary.json"
                try:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    summary = result.to_dict()
                checkpoint = _best_checkpoint_from_summary(seed_dir)
                score = _seed_result_score(summary, checkpoint)
                attempt = {
                    "seed_index": seed_index,
                    "seed_file": seed.file,
                    "seed_name": seed.name,
                    "status": summary.get("status"),
                    "score": score,
                    "checkpoint": checkpoint,
                    "summary_path": str(summary_path),
                    "final_test_path": str(seed_dir / "final_test.py"),
                    "oracle_risk": summary.get("final_oracle_risk") or checkpoint.get("oracle_risk") or {},
                    "surrogate_status": (summary.get("dual_version_result") or {}).get("status")
                    if isinstance(summary.get("dual_version_result"), dict)
                    else "",
                }
                attempts.append(attempt)
                order_key = (score, -seed_index, -int(checkpoint.get("round_id") or 0))
                if best is None or order_key > (best[0], best[1], best[2]):
                    best = (order_key[0], order_key[1], order_key[2], seed_dir, summary, checkpoint)
                try_next, reason = _should_try_next_seed(
                    summary, checkpoint, seed_index < len(seeds_to_try) - 1
                )
                attempts[-1]["switch_decision"] = "try_next_seed" if try_next else "stop"
                attempts[-1]["switch_reason"] = reason
                if try_next:
                    switch_reasons.append(f"seed_{seed_index}: {reason}")
                    continue
                break
            assert best is not None
            _, _, _, selected_dir, selected_summary, selected_checkpoint = best
            selected_seed_index = int(selected_dir.name.rsplit("_", 1)[-1])
            for name in (
                "final_test.py",
                "summary.json",
                "host_context.json",
                "protocol_recovery.json",
                "candidate_ranking.json",
                "dual_version_result.json",
                "repo_prepare.json",
                "icore_exec_spec.json",
                "worktree",
            ):
                _copy_if_exists(selected_dir, Path(output_dir), name)
            top_final = Path(output_dir) / "final_test.py"
            selected_summary.update(
                {
                    "final_test_path": str(top_final),
                    "seed_mode": "adaptive_top3",
                    "selected_seed_index": selected_seed_index,
                    "seed_attempts_count": len(attempts),
                    "seed_attempts_summary": attempts,
                    "seed_switch_reasons": switch_reasons,
                    "selected_seed_reason": selected_checkpoint.get("reason")
                    or selected_summary.get("final_reason")
                    or "selected by adaptive seed score",
                    "final_oracle_risk": selected_summary.get("final_oracle_risk")
                    or selected_checkpoint.get("oracle_risk")
                    or {},
                    "final_surrogate_risk": selected_summary.get("final_surrogate_risk")
                    or selected_checkpoint.get("surrogate_risk")
                    or {},
                }
            )
            safe_json_dump(attempts, str(Path(output_dir) / "seed_attempts_summary.json"))
            safe_json_dump(
                {
                    "selected_seed_index": selected_seed_index,
                    "selected_seed_dir": str(selected_dir),
                    "selected_seed_reason": selected_summary["selected_seed_reason"],
                },
                str(Path(output_dir) / "selected_seed_summary.json"),
            )
            safe_json_dump(selected_summary, str(Path(output_dir) / "summary.json"))
            return FinalResult(
                instance_id=context.instance_id,
                status=str(selected_summary.get("status") or ""),
                final_test_path=str(top_final),
                rounds_used=int(selected_summary.get("rounds_used") or 0),
                buggy_execution=selected_summary.get("buggy_execution") or {},
                dual_version_result=selected_summary.get("dual_version_result") or {},
                behavior_target=selected_summary.get("behavior_target") or behavior.to_dict(),
                host_context=selected_summary.get("host_context") or {},
                observation_report=selected_summary.get("observation_report") or {},
                notes=str(selected_summary.get("notes") or ""),
                seed_mode="adaptive_top3",
                selected_seed_index=selected_seed_index,
                seed_attempts_count=len(attempts),
                seed_attempts_summary=attempts,
                seed_switch_reasons=switch_reasons,
                selected_seed_reason=str(selected_summary.get("selected_seed_reason") or ""),
                final_oracle_risk=selected_summary.get("final_oracle_risk") or {},
                final_surrogate_risk=selected_summary.get("final_surrogate_risk") or {},
                final_reason=str(selected_summary.get("final_reason") or ""),
            )
        if not generate_only:
            if _prepared_repo_path and _prepare_meta is not None:
                prepared_repo, prepare_meta = _prepared_repo_path, dict(_prepare_meta)
            else:
                prepared_repo, prepare_meta = prepare_instance_worktree(
                    context, output_dir, conda_env, timeout, no_conda
                )
            context.buggy_repo_path = prepared_repo
            safe_json_dump(prepare_meta, str(Path(output_dir) / "repo_prepare.json"))
            if prepare_meta.get("status") in {
                "WORKTREE_ERROR",
                "ENV_CREATE_ERROR",
                "SETUP_ERROR",
            }:
                result = FinalResult(
                    instance_id=context.instance_id,
                    status="SETUP_ERROR",
                    final_test_path="",
                    rounds_used=0,
                    buggy_execution=prepare_meta.get("setup_execution", {}),
                    dual_version_result={"mode": validation_mode, "status": "SKIPPED"},
                    behavior_target={},
                    host_context={},
                    observation_report={},
                notes="repository worktree/setup failed before BRT generation",
                protocol_recovery_enabled=enable_protocol_recovery,
                seed_mutation_enabled=enable_seed_mutation,
                observation_oracle_enabled=enable_observation_oracle,
                strict_verifier_enabled=enable_strict_semantic_verifier,
                final_reason="repository worktree/setup failed before BRT generation",
            )
                result.save_json(str(Path(output_dir) / "summary.json"))
                return result
        else:
            safe_json_dump({"status": "SKIPPED", "reason": "generate_only"}, str(Path(output_dir) / "repo_prepare.json"))
        behavior = _load_cached_behavior(context, output_dir)
        behavior.save_json(str(Path(output_dir) / "behavior_target.json"))
        protocol = None
        seed_fallback_used = False
        seed_attempts: list[dict[str, Any]] = []
        ranked_tests = rank_related_tests(context.retrieved_tests, behavior)
        related_test = ranked_tests[0] if ranked_tests else select_related_test(context.retrieved_tests, behavior)
        host = None
        if _forced_seed_index is not None and 0 <= _forced_seed_index < len(ranked_tests):
            seeds_to_try = [ranked_tests[_forced_seed_index]]
        else:
            seeds_to_try = ranked_tests[:3] if enable_protocol_recovery else ([related_test] if related_test else [])
        for seed_index, seed in enumerate(seeds_to_try):
            candidate_host = build_host_context(
                context.instance_id, seed, context.buggy_repo_path, behavior,
                context.retrieved_code, conda_env, timeout, no_conda,
                skip_execution=generate_only, repo=context.repo,
                version=str(context.metadata.get("version") or ""),
            )
            candidate_protocol = recover_test_protocol(
                context.instance_id, seed, context.buggy_repo_path, behavior,
                context.retrieved_code, context.repo,
                str(context.metadata.get("version") or ""),
            ) if enable_protocol_recovery else None
            seed_attempts.append({
                "rank": seed_index,
                "file": seed.file,
                "name": seed.name,
                "execution_status": candidate_host.seed_execution_status,
                "selected": False,
                "protocol_risks": candidate_protocol.protocol_risks if candidate_protocol else [],
            })
            related_test, host, protocol = seed, candidate_host, candidate_protocol
            if generate_only or candidate_host.seed_execution_status not in {
                "SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT", "ERROR"
            }:
                break
            seed_fallback_used = seed_index < min(2, len(seeds_to_try) - 1)
        if host is None:
            host = build_host_context(
                context.instance_id, None, context.buggy_repo_path, behavior,
                context.retrieved_code, conda_env, timeout, no_conda,
                skip_execution=generate_only, repo=context.repo,
                version=str(context.metadata.get("version") or ""),
            )
        if seed_attempts:
            seed_attempts[-1]["selected"] = True
        safe_json_dump({"fallback_used": seed_fallback_used, "attempts": seed_attempts}, str(Path(output_dir) / "seed_fallback.json"))
        if protocol is not None:
            try:
                protocol = audit_recovered_protocol(
                    protocol, behavior, related_test, llm_client, output_dir
                )
            except Exception as exc:  # noqa: BLE001
                protocol.protocol_risks.append(f"协议模型审计失败，保留 AST 恢复结果：{exc}")
            protocol.save_json(str(Path(output_dir) / "protocol_recovery.json"))
        host.save_json(str(Path(output_dir) / "host_context.json"))
        candidate = None
        execution = None
        decision = None
        observation = None
        dual = None
        final_code = ""
        mutation_plans = []
        strict_result = None
        oracle_type = ""
        oracle_rebound = False
        env_budget = max_env_rounds if max_env_rounds is not None else max_feedback_rounds
        brt_budget = max_brt_rounds if max_brt_rounds is not None else max_feedback_rounds
        initial_plan = build_mutation_plan(
            context.instance_id, 0, behavior, host, protocol, llm_client, output_dir
        ) if enable_seed_mutation else None
        if initial_plan:
            mutation_plans.append(initial_plan)
        candidate = generate_candidate(
            context.instance_id,
            behavior,
            host,
            related_test,
            context.retrieved_code,
            llm_client,
            output_dir,
            context.buggy_repo_path,
            0,
            write_to_repo=not generate_only,
            protocol=protocol,
            mutation_plan=initial_plan,
        )
        write_text(str(Path(output_dir) / "mutation_round_0_test.py"), candidate.code)
        _refresh_candidate_command(context, candidate)
        if generate_only:
            final_code = candidate.code
            write_text(str(Path(output_dir) / "final_test.py"), final_code)
            execution_stub = {"status": "SKIPPED", "reason": "generate_only"}
            result = FinalResult(
                instance_id=context.instance_id,
                status="GENERATED",
                final_test_path=str(Path(output_dir) / "final_test.py"),
                rounds_used=1,
                buggy_execution=execution_stub,
                dual_version_result={"mode": "buggy_only", "status": "SKIPPED"},
                behavior_target=behavior.to_dict(),
                host_context=host.to_dict(),
                observation_report={},
                notes="generate_only: complete same-directory test file generated without execution",
                protocol_recovery_enabled=enable_protocol_recovery,
                seed_mutation_enabled=enable_seed_mutation,
                observation_oracle_enabled=enable_observation_oracle,
                strict_verifier_enabled=enable_strict_semantic_verifier,
                selected_seed_file=related_test.file if related_test else "",
                selected_seed_name=related_test.name if related_test else "",
                seed_fallback_used=seed_fallback_used,
                mutation_ops=initial_plan.mutation_ops if initial_plan else [],
                final_reason="generate_only: generation completed without execution",
            )
            result.save_json(str(Path(output_dir) / "summary.json"))
            return result

        env_rounds_used = 0
        for env_round in range(env_budget):
            execution = run_command_in_conda(candidate.command, context.buggy_repo_path, conda_env, timeout, no_conda, behavior, context.instance_id)
            safe_json_dump(execution.to_dict(), str(Path(output_dir) / f"env_execution_round_{env_round}.json"))
            write_text(str(Path(output_dir) / "logs" / f"env_execution_round_{env_round}.log"), execution.stdout + "\n" + execution.stderr)
            env_rounds_used = env_round + 1
            if execution.status not in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR"}:
                break
            if execution.status == "SETUP_ERROR" and _recover_declared_dependency(
                context,
                execution,
                conda_env,
                timeout,
                no_conda,
                output_dir,
                env_round,
            ):
                continue
            if env_round == env_budget - 1:
                break
            candidate = repair_candidate(
                context.instance_id,
                behavior,
                host,
                candidate,
                execution,
                llm_client,
                output_dir,
                env_round + 1,
                "setup",
                context.retrieved_code,
                buggy_repo=context.buggy_repo_path,
                protocol=protocol,
            )
            _refresh_candidate_command(context, candidate)
        if execution is not None and execution.status in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR"}:
            final_code = candidate.code
            write_text(str(Path(output_dir) / "final_test.py"), final_code)
            result = FinalResult(
                instance_id=context.instance_id,
                status="ENV_UNRESOLVED",
                final_test_path=str(Path(output_dir) / "final_test.py"),
                rounds_used=env_rounds_used,
                buggy_execution=execution.to_dict(),
                dual_version_result={
                    "mode": validation_mode,
                    "status": "SKIPPED_ENV_UNRESOLVED",
                },
                behavior_target=behavior.to_dict(),
                host_context=host.to_dict(),
                observation_report={},
                notes=(
                    f"environment probe remained {execution.status} after "
                    f"{env_rounds_used} rounds; BRT and dual-version validation skipped"
                ),
                protocol_recovery_enabled=enable_protocol_recovery,
                seed_mutation_enabled=enable_seed_mutation,
                observation_oracle_enabled=enable_observation_oracle,
                strict_verifier_enabled=enable_strict_semantic_verifier,
                selected_seed_file=related_test.file if related_test else "",
                selected_seed_name=related_test.name if related_test else "",
                seed_fallback_used=seed_fallback_used,
                mutation_ops=[op for plan in mutation_plans for op in plan.mutation_ops],
                final_reason="environment qualification remained unresolved",
            )
            result.save_json(str(Path(output_dir) / "summary.json"))
            return result
        else:
            brt_attempt = 0
            semantic_repairs_used = 0
            late_setup_repairs_used = 0
            # Round 0 is the initial BRT. Environment qualification already
            # has its own budget above and must not expand this checkpoint loop.
            max_brt_attempts = 1 + max(0, brt_budget)
            checkpoints: list[CandidateCheckpoint] = []
            best_score = -1
            best_index = -1
            best_candidate = None
            best_execution = None
            best_decision = None
            best_dual = None
            best_observation = None
            best_strict_result = None
            while brt_attempt < max_brt_attempts:
                if brt_attempt > 0 or execution is None:
                    execution = run_command_in_conda(candidate.command, context.buggy_repo_path, conda_env, timeout, no_conda, behavior, context.instance_id)
                safe_json_dump(execution.to_dict(), str(Path(output_dir) / f"execution_round_{brt_attempt}.json"))
                write_text(str(Path(output_dir) / "logs" / f"execution_round_{brt_attempt}.log"), execution.stdout + "\n" + execution.stderr)
                effective_source = format_effective_source_context(
                    behavior, context.retrieved_code, context.buggy_repo_path
                )
                if enable_strict_semantic_verifier:
                    decision, strict_result = verify_strict_semantics(
                        context.issue_text, behavior, protocol, candidate,
                        execution, effective_source, llm_client, output_dir,
                        brt_attempt,
                    )
                else:
                    decision = verify_buggy_only(
                        context.issue_text, behavior, candidate, execution,
                        llm_client, host.to_dict(), effective_source,
                    )
                safe_json_dump(decision.to_dict(), str(Path(output_dir) / f"verifier_round_{brt_attempt}.json"))
                candidate_dual = None
                if decision.decision == "accept":
                    if validation_mode == "surrogate_patch":
                        validation_dir = str(
                            Path(output_dir)
                            / "candidate_validations"
                            / f"attempt_{brt_attempt}"
                        )
                        ensure_dir(validation_dir)
                        candidate_dual = run_surrogate_patch_loop(
                            context.instance_id,
                            behavior,
                            candidate,
                            context.retrieved_code,
                            context.buggy_repo_path,
                            execution,
                            llm_client,
                            validation_dir,
                            conda_env,
                            timeout,
                            no_conda,
                            max_patch_rounds,
                        )
                    else:
                        candidate_dual = DualVersionResult(
                            context.instance_id,
                            "buggy_only",
                            execution.to_dict(),
                            {},
                            "SKIPPED",
                            "Only buggy source was executed.",
                        )
                checkpoint = _save_checkpoint(
                    output_dir,
                    brt_attempt,
                    candidate,
                    execution,
                    decision,
                    candidate_dual,
                    behavior,
                    context.issue_text,
                    {item.path for item in context.retrieved_code if item.path},
                )
                checkpoints.append(checkpoint)
                if checkpoint.score > best_score:
                    best_score = checkpoint.score
                    best_index = len(checkpoints) - 1
                    best_candidate = copy.deepcopy(candidate)
                    best_execution = copy.deepcopy(execution)
                    best_decision = copy.deepcopy(decision)
                    best_dual = copy.deepcopy(candidate_dual)
                    best_observation = copy.deepcopy(observation)
                    best_strict_result = copy.deepcopy(strict_result)
                if decision.decision == "accept":
                    if (
                        validation_mode != "surrogate_patch"
                        or candidate_dual is not None
                        and candidate_dual.status
                        in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"}
                    ):
                        break
                    if semantic_repairs_used >= max(0, brt_budget):
                        break
                    decision = VerifierDecision(
                        instance_id=context.instance_id,
                        decision="repair_oracle",
                        reason=(
                            "The candidate failed on buggy source, but no independent "
                            "surrogate source repair made it pass. Re-check that the "
                            "oracle follows expected_behavior rather than an incidental "
                            "buggy observation."
                        ),
                        focus=["oracle"],
                        next_action=(
                            "Use a stable positive semantic assertion supported by the "
                            "Issue, related tests, and runtime observations."
                        ),
                    )
                    safe_json_dump(
                        decision.to_dict(),
                        str(
                            Path(output_dir)
                            / f"surrogate_feedback_round_{brt_attempt}.json"
                        ),
                    )
                focus = "trigger"
                if decision.decision == "repair_setup":
                    if late_setup_repairs_used >= env_budget:
                        final_code = candidate.code
                        write_text(str(Path(output_dir) / "final_test.py"), final_code)
                        break
                    focus = "setup"
                    late_setup_repairs_used += 1
                elif decision.decision == "repair_oracle":
                    if semantic_repairs_used >= max(0, brt_budget):
                        final_code = candidate.code
                        write_text(str(Path(output_dir) / "final_test.py"), final_code)
                        break
                    next_round = env_rounds_used + brt_attempt + 1
                    if enable_observation_oracle:
                        candidate, observation, oracle_type = rebind_observation_oracle(
                            behavior, protocol, candidate,
                            execution.stdout + "\n" + execution.stderr,
                            llm_client, output_dir, context.buggy_repo_path,
                            conda_env, timeout, no_conda, context.repo,
                            str(context.metadata.get("version") or ""), next_round,
                        )
                        final_code = candidate.code
                        oracle_rebound = True
                    else:
                        observation = run_observation_probe(
                            behavior, candidate, llm_client, output_dir,
                            context.buggy_repo_path, conda_env, timeout, no_conda,
                            context.repo, str(context.metadata.get("version") or ""),
                        )
                        final_code = synthesize_oracle(
                            behavior, candidate, observation,
                            execution.stdout + "\n" + execution.stderr,
                            llm_client, output_dir,
                        )
                        candidate.code = final_code
                    candidate.round_id = next_round
                    write_text(str(Path(output_dir) / f"candidate_round_{next_round}.py"), final_code)
                    _refresh_candidate_command(context, candidate)
                    semantic_repairs_used += 1
                    # Oracle synthesis already performs the oracle repair using
                    # runtime observations. Do not immediately rewrite it a
                    # second time with the stale pre-observation execution log.
                    brt_attempt += 1
                    continue
                else:
                    if semantic_repairs_used >= max(0, brt_budget):
                        final_code = candidate.code
                        write_text(str(Path(output_dir) / "final_test.py"), final_code)
                        break
                    semantic_repairs_used += 1
                mutation_plan = None
                if focus == "trigger" and enable_seed_mutation:
                    mutation_plan = build_mutation_plan(
                        context.instance_id,
                        env_rounds_used + brt_attempt + 1,
                        behavior, host, protocol, llm_client, output_dir,
                        execution.stdout + "\n" + execution.stderr,
                        decision.to_dict(),
                    )
                    mutation_plans.append(mutation_plan)
                candidate = repair_candidate(
                    context.instance_id,
                    behavior,
                    host,
                    candidate,
                    execution,
                    llm_client,
                    output_dir,
                    env_rounds_used + brt_attempt + 1,
                    focus,
                    context.retrieved_code,
                    json.dumps(observation.to_dict() if observation else {}, ensure_ascii=False),
                    decision.to_dict(),
                    context.buggy_repo_path,
                    protocol,
                    mutation_plan,
                )
                if mutation_plan is not None:
                    write_text(str(Path(output_dir) / f"mutation_round_{mutation_plan.round_id}_test.py"), candidate.code)
                _refresh_candidate_command(context, candidate)
                brt_attempt += 1
            if best_candidate is not None:
                candidate = best_candidate
                execution = best_execution
                decision = best_decision
                dual = best_dual
                observation = best_observation
                strict_result = best_strict_result
                final_code = candidate.code
                write_text(candidate.candidate_file_path, candidate.code)
                _refresh_candidate_command(context, candidate)
                checkpoints[best_index].selected = True
                checkpoints[best_index].save_json(
                    str(
                        Path(output_dir)
                        / "checkpoints"
                        / f"candidate_attempt_{checkpoints[best_index].round_id}.json"
                    )
                )
                safe_json_dump(
                    {
                        "selection_policy": (
                            "risk-adjusted: surrogate_f2p > verifier_accept > "
                            "executable_buggy_fail > buggy_pass > environment_failure; "
                            "earliest wins ties"
                        ),
                        "selected_attempt": checkpoints[best_index].round_id,
                        "checkpoints": [item.to_dict() for item in checkpoints],
                    },
                    str(Path(output_dir) / "candidate_ranking.json"),
                )
        assert candidate is not None and execution is not None
        if dual is not None:
            pass
        elif validation_mode == "surrogate_patch":
            if execution.returncode == 0:
                dual = DualVersionResult(
                    context.instance_id,
                    validation_mode,
                    execution.to_dict(),
                    {},
                    "BUGGY_PASS",
                    "Surrogate patch validation skipped because the BRT passes on buggy source.",
                )
            elif decision is not None and decision.decision == "accept":
                dual = run_surrogate_patch_loop(
                    context.instance_id,
                    behavior,
                    candidate,
                    context.retrieved_code,
                    context.buggy_repo_path,
                    execution,
                    llm_client,
                    output_dir,
                    conda_env,
                    timeout,
                    no_conda,
                    max_patch_rounds,
                )
            else:
                dual = DualVersionResult(
                    context.instance_id,
                    validation_mode,
                    execution.to_dict(),
                    {},
                    "SKIPPED_UNALIGNED_BUGGY_FAIL",
                    "Surrogate patch validation requires an issue-aligned buggy failure.",
                )
            dual.save_json(str(Path(output_dir) / "dual_version_result.json"))
        else:
            dual = DualVersionResult(
                context.instance_id,
                "buggy_only",
                execution.to_dict(),
                {},
                "SKIPPED",
                "Only the buggy repository was executed; no patched source was loaded.",
            )
            dual.save_json(str(Path(output_dir) / "dual_version_result.json"))
        write_text(str(Path(output_dir) / "final_test.py"), final_code or candidate.code)
        final_oracle_risk = assess_oracle_risk(
            final_code or candidate.code,
            behavior,
            context.issue_text,
            (execution.stdout + "\n" + execution.stderr) if execution else "",
            observation.to_dict() if observation else {},
        )
        final_surrogate_risk = assess_surrogate_risk(
            dual,
            final_oracle_risk,
            {item.path for item in context.retrieved_code if item.path},
        )
        candidate_selector = first_test_selector(final_code or candidate.code)
        placement_dir = str(Path(candidate.candidate_repo_path).parent)
        if dual.status in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"}:
            status = dual.status
        elif decision is not None and decision.decision == "accept":
            status = "ISSUE_ALIGNED_FAIL"
        elif execution.status in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT"}:
            status = execution.status
        elif execution.returncode != 0:
            # Executor keyword matching is only a triage hint. A rejected
            # verifier decision must never become an accepted issue failure.
            status = "UNRELATED_FAIL"
        else:
            status = execution.status
        result = FinalResult(
            instance_id=context.instance_id,
            status=status,
            final_test_path=str(Path(output_dir) / "final_test.py"),
            rounds_used=(candidate.round_id + 1),
            buggy_execution=execution.to_dict(),
            dual_version_result=dual.to_dict(),
            behavior_target=behavior.to_dict(),
            host_context=host.to_dict(),
            observation_report=observation.to_dict() if observation else {},
            notes=decision.reason if decision else "",
            protocol_recovery_enabled=enable_protocol_recovery,
            seed_mutation_enabled=enable_seed_mutation,
            observation_oracle_enabled=enable_observation_oracle,
            strict_verifier_enabled=enable_strict_semantic_verifier,
            selected_seed_file=related_test.file if related_test else "",
            selected_seed_name=related_test.name if related_test else "",
            seed_fallback_used=seed_fallback_used,
            mutation_ops=list(dict.fromkeys(op for plan in mutation_plans for op in plan.mutation_ops)),
            oracle_type=oracle_type,
            strict_verifier_decision=strict_result.decision if strict_result else "",
            strict_failure_class=strict_result.failure_class if strict_result else "",
            oracle_rebound=oracle_rebound,
            final_reason=decision.reason if decision else "",
            seed_mode="single_forced_seed" if _forced_seed_index is not None else "single_seed",
            selected_seed_index=_forced_seed_index if _forced_seed_index is not None else 0,
            seed_attempts_count=1,
            seed_attempts_summary=seed_attempts,
            final_oracle_risk=final_oracle_risk,
            final_surrogate_risk=final_surrogate_risk,
            candidate_repo_path=candidate.candidate_repo_path,
            pytest_nodeid=candidate.pytest_nodeid,
            command=candidate.command,
            direct_test_repo_path_hint=candidate.candidate_repo_path,
            placement_dir=placement_dir,
            runner_kind=context.repo.split("/")[-1],
            selector=candidate_selector,
        )
        result.save_json(str(Path(output_dir) / "summary.json"))
        return result
    except Exception as exc:  # noqa: BLE001
        safe_json_dump({
            "instance_id": context.instance_id,
            "status": "ERROR",
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "protocol_recovery_enabled": enable_protocol_recovery,
            "seed_mutation_enabled": enable_seed_mutation,
            "observation_oracle_enabled": enable_observation_oracle,
            "strict_verifier_enabled": enable_strict_semantic_verifier,
            "selected_seed_file": "",
            "selected_seed_name": "",
            "seed_fallback_used": False,
            "mutation_ops": [],
            "oracle_type": "",
            "strict_verifier_decision": "",
            "strict_failure_class": "",
            "oracle_rebound": False,
            "final_reason": str(exc),
        }, str(Path(output_dir) / "summary.json"))
        return FinalResult(instance_id=context.instance_id, status="ERROR", notes=str(exc))
