"""Main feedback loop for BRT3."""

from __future__ import annotations

import copy
import json
import fcntl
import shlex
import shutil
import subprocess
import traceback
import re
from pathlib import Path
from typing import Any

from ..runtime.executor import run_command_in_conda
from ..generation.generator import (
    format_effective_source_context,
    generate_candidate,
    repair_candidate,
)
from .host_context import build_host_context, rank_related_tests, select_related_test
from ..context.protocol_recovery import audit_recovered_protocol, recover_test_protocol
from ..mutation.seed_mutator import build_mutation_plan
from ..generation.observation_oracle import rebind_observation_oracle
from ..validation.strict_semantic_verifier import verify_strict_semantics
from ..runtime.icore_runtime import (
    dump_spec,
    ensure_icore_environment,
    env_name_for,
    env_lock_path,
    icore_setup_command,
    icore_test_command,
    first_test_selector,
    make_instance_spec,
)
from .issue_rewriter import rewrite_issue
from ..generation.oracle import run_observation_probe, synthesize_oracle
from ..generation.patch_utils import run_surrogate_patch_loop
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


def _save_checkpoint(
    output_dir: str,
    attempt_id: int,
    candidate: Any,
    execution: ExecutionResult,
    decision: VerifierDecision,
    dual: DualVersionResult | None,
) -> CandidateCheckpoint:
    checkpoint_dir = ensure_dir(Path(output_dir) / "checkpoints")
    code_path = str(Path(checkpoint_dir) / f"candidate_attempt_{attempt_id}.py")
    write_text(code_path, candidate.code)
    score, reason = _checkpoint_score(execution, decision, dual)
    checkpoint = CandidateCheckpoint(
        instance_id=candidate.instance_id,
        round_id=attempt_id,
        code_path=code_path,
        score=score,
        reason=reason,
        execution=execution.to_dict(),
        verifier=decision.to_dict(),
        surrogate=dual.to_dict() if dual else {},
    )
    checkpoint.save_json(
        str(Path(checkpoint_dir) / f"candidate_attempt_{attempt_id}.json")
    )
    return checkpoint


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
            and "uninstall-no-record-file" in setup_log
            and "python -m pip install" in setup
            and " -e ." in setup
        ):
            fallback_setup = setup.replace(
                "python -m pip install",
                "python -m pip install --ignore-installed --no-deps",
                1,
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
) -> FinalResult:
    ensure_dir(output_dir)
    ensure_dir(Path(output_dir) / "prompts")
    ensure_dir(Path(output_dir) / "responses")
    ensure_dir(Path(output_dir) / "logs")
    try:
        if not generate_only:
            prepared_repo, prepare_meta = prepare_instance_worktree(context, output_dir, conda_env, timeout, no_conda)
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
        behavior_path = Path(output_dir) / "behavior_target.json"
        if behavior_path.exists():
            from .issue_rewriter import behavior_from_dict
            from ..core.utils import safe_json_load

            behavior = behavior_from_dict(context.instance_id, safe_json_load(behavior_path))
        else:
            behavior = rewrite_issue(context, llm_client, output_dir)
        behavior.save_json(str(Path(output_dir) / "behavior_target.json"))
        protocol = None
        seed_fallback_used = False
        seed_attempts: list[dict[str, Any]] = []
        ranked_tests = rank_related_tests(context.retrieved_tests, behavior)
        related_test = ranked_tests[0] if ranked_tests else select_related_test(context.retrieved_tests, behavior)
        host = None
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
                            "surrogate_f2p > verifier_accept > executable_buggy_fail "
                            "> buggy_pass > environment_failure; earliest wins ties"
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
