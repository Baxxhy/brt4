"""Main feedback loop for BRT3."""

from __future__ import annotations

import copy
import hashlib
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
from ..generation.archive import CandidateArchive
from ..generation.adaptive_search import (
    SearchBudgets,
    decide_search_action,
    recompose_candidate,
    search_prompt_context,
    select_checkpoint_v2,
    validate_typed_transformation,
)
from ..generation.segmenter import segment_test, transplant_typed_segment
from ..generation.structured_observation import collect_structured_observation
from ..generation.counterfactual import (
    build_counterfactual_evidence,
    build_counterfactual_plan,
    collect_target_reachability,
    counterfactual_summary,
    generate_negative_control,
)
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
    CandidateTest,
    DualVersionResult,
    ExecutionResult,
    FinalResult,
    InstanceContext,
    VerifierDecision,
)
from ..core.utils import ensure_dir, safe_json_dump, write_text
from ..validation.verifier import verify_buggy_only
from ..validation.oracle_risk import assess_oracle_risk, assess_surrogate_risk
from ..runtime.conda_env_manager import environment_identity_metadata


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


def _oracle_complexity(code: str) -> int:
    lowered = code.lower()
    return len(re.findall(r"\bassert\b", lowered)) + lowered.count("assert")


def _status_rank(value: str, mapping: dict[str, int], default: int = 0) -> int:
    return mapping.get(str(value or "").upper(), default)


def _build_evidence_rank(
    execution: ExecutionResult,
    decision: VerifierDecision,
    oracle_risk: dict[str, Any],
    candidate_code: str,
    counterfactual_evidence: dict[str, Any] | None,
    protocol_valid: bool = True,
) -> tuple[dict[str, Any], list[int], int]:
    evidence = counterfactual_evidence or {}
    trigger = evidence.get("trigger_necessity") if isinstance(evidence, dict) else {}
    repair = evidence.get("repair_sufficiency") if isinstance(evidence, dict) else {}
    oracle_stability = evidence.get("oracle_stability") if isinstance(evidence, dict) else {}
    bidirectional = evidence.get("bidirectional_support") if isinstance(evidence, dict) else {}
    positive = evidence.get("positive_buggy") if isinstance(evidence, dict) else {}
    runtime_target_hit = str(
        (positive or {}).get("runtime_target_hit")
        or execution.runtime_target_hit
        or "unknown"
    )
    evidence_rank = {
        "protocol_valid": protocol_valid,
        "runtime_target_hit": runtime_target_hit,
        "semantic_accept": decision.decision == "accept",
        "buggy_issue_fail": (
            decision.decision == "accept"
            or execution.status == "ISSUE_ALIGNED_FAIL"
        ),
        "trigger_necessity": str((trigger or {}).get("status") or "UNKNOWN"),
        "repair_sufficiency": str((repair or {}).get("status") or "UNKNOWN"),
        "oracle_stability": str((oracle_stability or {}).get("status") or "UNKNOWN"),
        "bidirectional_support": str((bidirectional or {}).get("status") or "UNKNOWN"),
        "oracle_risk": str(oracle_risk.get("level") or "low").upper(),
        "oracle_complexity": _oracle_complexity(candidate_code),
        "test_edit_distance": 0,
    }
    key = [
        1 if evidence_rank["protocol_valid"] else 0,
        1 if execution.status not in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT"} else 0,
        1 if evidence_rank["buggy_issue_fail"] else 0,
        _status_rank(
            evidence_rank["runtime_target_hit"],
            {"TRUE": 2, "UNKNOWN": 1, "FALSE": 0},
            1,
        ),
        _status_rank(
            evidence_rank["bidirectional_support"],
            {"STRONG": 3, "PARTIAL": 2, "UNKNOWN": 1, "NONE": 0},
            1,
        ),
        _status_rank(
            evidence_rank["trigger_necessity"],
            {"SUPPORTED": 3, "WEAK": 2, "UNKNOWN": 1, "UNSUPPORTED": 0},
            1,
        ),
        _status_rank(
            evidence_rank["repair_sufficiency"],
            {"SUPPORTED": 3, "WEAK": 2, "UNKNOWN": 1, "UNSUPPORTED": 0},
            1,
        ),
        _status_rank(
            evidence_rank["oracle_stability"],
            {"STABLE": 2, "UNKNOWN": 1, "UNSTABLE": 0},
            1,
        ),
        1 if evidence_rank["semantic_accept"] else 0,
        _status_rank(
            evidence_rank["oracle_risk"],
            {"LOW": 2, "MEDIUM": 1, "HIGH": 0},
            2,
        ),
        -int(evidence_rank["oracle_complexity"]),
        -int(evidence_rank["test_edit_distance"]),
    ]
    bonus = 0
    if evidence_rank["bidirectional_support"] == "STRONG":
        bonus += 40
    elif evidence_rank["bidirectional_support"] == "PARTIAL":
        bonus += 15
    if evidence_rank["trigger_necessity"] == "SUPPORTED":
        bonus += 10
    if evidence_rank["repair_sufficiency"] == "SUPPORTED":
        bonus += 10
    if evidence_rank["oracle_stability"] == "STABLE":
        bonus += 5
    return evidence_rank, key, bonus


def _counterfactual_checkpoint_order_key(checkpoint: CandidateCheckpoint) -> tuple[int, ...]:
    return tuple(
        [checkpoint.legacy_score or checkpoint.selector_score_after_risk or checkpoint.score]
        + [int(item) for item in checkpoint.evidence_rank_key]
        + [-int(checkpoint.round_id)]
    )


REPAIR_AWARE_ONLY_ORIGINS = {
    "contrastive_observation_oracle",
    "counterfactual_trigger_repair",
    "counterfactual_oracle_repair",
}


def _checkpoint_origin(checkpoint: CandidateCheckpoint | dict[str, Any]) -> str:
    lineage = checkpoint.lineage if isinstance(checkpoint, CandidateCheckpoint) else checkpoint.get("lineage")
    if isinstance(lineage, dict):
        return str(lineage.get("origin") or "UNKNOWN")
    return "UNKNOWN"


def _is_repair_aware_only(checkpoint: CandidateCheckpoint | dict[str, Any]) -> bool:
    return _checkpoint_origin(checkpoint) in REPAIR_AWARE_ONLY_ORIGINS


def _legacy_checkpoint_order_key(checkpoint: CandidateCheckpoint) -> tuple[int, int]:
    return (
        int(checkpoint.legacy_score or checkpoint.selector_score_after_risk or checkpoint.score),
        -int(checkpoint.round_id),
    )


def _counterfactual_guided_decision(
    decision: VerifierDecision,
    execution: ExecutionResult,
    runtime_target_hit: str,
    evidence: dict[str, Any] | None,
    mode: str,
) -> VerifierDecision:
    if execution.status in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR"}:
        return decision
    if execution.returncode == 0 and decision.decision != "repair_setup":
        return VerifierDecision(
            instance_id=decision.instance_id,
            decision="repair_trigger",
            reason=decision.reason + " Counterfactual guidance: buggy source passed, so the trigger remains insufficient.",
            focus=["trigger"],
            next_action="repair_trigger",
        )
    if runtime_target_hit == "false" and execution.returncode != 0:
        return VerifierDecision(
            instance_id=decision.instance_id,
            decision="repair_trigger",
            reason=decision.reason + " Counterfactual guidance: runtime evidence says target path was not reached.",
            focus=["trigger"],
            next_action="repair_trigger",
        )
    if not evidence:
        return decision
    trigger_status = str(
        (evidence.get("trigger_necessity") or {}).get("status") or "UNKNOWN"
    )
    repair_status = str(
        (evidence.get("repair_sufficiency") or {}).get("status") or "UNKNOWN"
    )
    if mode == "strict" and trigger_status == "SUPPORTED" and repair_status == "SUPPORTED":
        if decision.decision in {"reject", "repair_oracle"} and execution.returncode != 0:
            return VerifierDecision(
                instance_id=decision.instance_id,
                decision="accept",
                reason=decision.reason + " Strict counterfactual mode: bidirectional evidence supports acceptance.",
                focus=["accept"],
                next_action="accept",
            )
    if decision.decision == "reject" and trigger_status in {"SUPPORTED", "WEAK"}:
        return VerifierDecision(
            instance_id=decision.instance_id,
            decision="repair_oracle",
            reason=decision.reason + " Counterfactual guidance: trigger evidence exists, so repair oracle instead of rejecting.",
            focus=["oracle"],
            next_action="repair_oracle",
        )
    return decision


def _contrastive_observation_context(
    evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    if not evidence:
        return {}
    surrogate_runs = evidence.get("surrogate_runs")
    if not isinstance(surrogate_runs, list):
        surrogate_runs = []
    positive_surrogates = []
    negative_surrogates = []
    for run in surrogate_runs:
        if not isinstance(run, dict):
            continue
        positive = run.get("positive_result") if isinstance(run.get("positive_result"), dict) else {}
        negative = run.get("negative_result") if isinstance(run.get("negative_result"), dict) else {}
        if positive:
            positive_surrogates.append(positive.get("public_observations") or positive)
        if negative:
            negative_surrogates.append(negative.get("public_observations") or negative)
    positive_buggy = evidence.get("positive_buggy") if isinstance(evidence.get("positive_buggy"), dict) else {}
    negative_buggy = evidence.get("negative_buggy") if isinstance(evidence.get("negative_buggy"), dict) else {}
    context = {
        "positive_buggy_observation": positive_buggy.get("public_observations") or positive_buggy,
        "negative_buggy_observation": negative_buggy.get("public_observations") or negative_buggy,
        "positive_surrogate_observations": positive_surrogates,
        "negative_surrogate_observations": negative_surrogates,
        "counterfactual_evidence": evidence,
    }
    return context if any(value for value in context.values()) else {}


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
    counterfactual_evidence: dict[str, Any] | None = None,
    counterfactual_summary_data: dict[str, Any] | None = None,
    protocol_valid: bool = True,
    counterfactual_shadow_mode: bool = True,
    lineage: dict[str, Any] | None = None,
    archive_entry: dict[str, Any] | None = None,
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
    evidence_rank, evidence_rank_key, evidence_bonus = _build_evidence_rank(
        execution,
        decision,
        oracle_risk,
        candidate.code,
        counterfactual_evidence,
        protocol_valid,
    )
    checkpoint = CandidateCheckpoint(
        instance_id=candidate.instance_id,
        round_id=attempt_id,
        code_path=code_path,
        score=adjusted_score if counterfactual_shadow_mode else adjusted_score + evidence_bonus,
        reason=reason,
        oracle_risk=oracle_risk,
        surrogate_risk=surrogate_risk,
        selector_score_before_risk=score,
        selector_score_after_risk=adjusted_score,
        selector_penalty_reasons=penalty_reasons,
        execution=execution.to_dict(),
        verifier=decision.to_dict(),
        surrogate=dual.to_dict() if dual else {},
        legacy_score=adjusted_score,
        evidence_rank=evidence_rank,
        evidence_rank_key=evidence_rank_key,
        counterfactual_evidence_rank=evidence_rank,
        counterfactual_evidence=counterfactual_evidence or {},
        counterfactual_summary=counterfactual_summary_data or {},
        lineage=lineage or getattr(candidate, "lineage", {}) or {
            "origin": "UNKNOWN",
            "parent_candidate_id": "",
            "seed_id": "",
            "generation_round": attempt_id,
            "repair_round": 0,
            "observation_context_used": "none",
            "counterfactual_evidence_id": "",
            "negative_control_id": "",
        },
        candidate_id=str(
            (getattr(candidate, "lineage", {}) or {}).get("candidate_id") or attempt_id
        ),
        archive_entry=archive_entry or {},
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


def _checkpoint_legacy_score(checkpoint: dict[str, Any]) -> int:
    return int(
        checkpoint.get("legacy_score")
        or checkpoint.get("selector_score_after_risk")
        or checkpoint.get("score")
        or 0
    )


def _seed_result_score(summary: dict[str, Any], checkpoint: dict[str, Any]) -> int:
    status = str(summary.get("status") or "")
    dual = summary.get("dual_version_result") if isinstance(summary.get("dual_version_result"), dict) else {}
    if status in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"} or dual.get("status") in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"}:
        return 300
    if status == "ISSUE_ALIGNED_FAIL" or summary.get("strict_failure_class") == "issue_aligned":
        return 200
    if checkpoint:
        return _checkpoint_legacy_score(checkpoint)
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
    if _checkpoint_legacy_score(checkpoint) < 100:
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
        issue_meta = {
            "instance_id": context.instance_id,
            "repo": context.repo,
            "version": version,
            "base_commit": base_commit,
            "environment_setup_commit": environment_setup_commit,
        }
        return str(worktree), {
            "status": "ENV_CREATE_ERROR",
            "source_repo": source_repo,
            "repo_path": str(worktree),
            "base_commit": base_commit,
            "env_name": resolved_env,
            "environment_identity": environment_identity_metadata(
                issue_meta,
                resolved_env,
                run_id=str(Path(output_dir).parent.name),
                setup_status="ENV_CREATE_ERROR",
                setup_script_fingerprint="",
                source="generation_env_create",
            ),
            "environment": env_result,
            "worktree": add_result,
        }
    setup = icore_setup_command(spec, str(worktree))
    setup_fingerprint = hashlib.sha256(setup.encode("utf-8")).hexdigest()
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
    issue_meta = {
        "instance_id": context.instance_id,
        "repo": context.repo,
        "version": version,
        "base_commit": base_commit,
        "environment_setup_commit": environment_setup_commit,
    }
    env_identity = environment_identity_metadata(
        issue_meta,
        resolved_env,
        run_id=str(Path(output_dir).parent.name),
        setup_status=status,
        setup_script_fingerprint=setup_fingerprint,
        source="generation_repo_prepare",
    )
    return str(worktree), {
        "status": status,
        "source_repo": source_repo,
        "repo_path": str(worktree),
        "base_commit": base_commit,
        "environment_setup_commit": environment_setup_commit,
        "env_name": resolved_env,
        "environment_identity": env_identity,
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
    counterfactual_shadow_mode: bool = True,
    enable_bidirectional_counterfactual_validation: bool = False,
    enable_negative_control: bool = False,
    max_negative_control_attempts: int = 1,
    max_negative_control_ast_edits: int = 1,
    enable_runtime_target_reachability: bool = True,
    enable_contrastive_observation_oracle: bool = False,
    min_valid_surrogate_patches_for_consensus: int = 2,
    surrogate_consensus_threshold: float = 0.67,
    counterfactual_evidence_mode: str = "soft",
    enable_adaptive_typed_search: bool = True,
    enable_structured_observation_extractor: bool = True,
    enable_minimal_oracle_search: bool = True,
    enable_trigger_search: bool = True,
    enable_duplicate_aware_archive: bool = True,
    enable_optional_recomposition: bool = True,
    enable_selector_v2: bool = True,
    max_extra_unique_candidates: int = 3,
    max_trigger_search_candidates: int = 2,
    max_minimal_oracle_candidates: int = 2,
    max_protocol_repair_candidates: int = 1,
    max_recomposition_candidates: int = 1,
    _adaptive_disabled: bool = False,
    _forced_seed_index: int | None = None,
    _prepared_repo_path: str = "",
    _prepare_meta: dict[str, Any] | None = None,
    _shared_archive_path: str = "",
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
                    counterfactual_shadow_mode,
                    enable_bidirectional_counterfactual_validation,
                    enable_negative_control,
                    max_negative_control_attempts,
                    max_negative_control_ast_edits,
                    enable_runtime_target_reachability,
                    enable_contrastive_observation_oracle,
                    min_valid_surrogate_patches_for_consensus,
                    surrogate_consensus_threshold,
                    counterfactual_evidence_mode,
                    enable_adaptive_typed_search,
                    enable_structured_observation_extractor,
                    enable_minimal_oracle_search,
                    enable_trigger_search,
                    enable_duplicate_aware_archive,
                    enable_optional_recomposition,
                    enable_selector_v2,
                    max_extra_unique_candidates,
                    max_trigger_search_candidates,
                    max_minimal_oracle_candidates,
                    max_protocol_repair_candidates,
                    max_recomposition_candidates,
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
                        enable_bidirectional_counterfactual_validation=enable_bidirectional_counterfactual_validation,
                        counterfactual_shadow_mode=counterfactual_shadow_mode,
                        enable_negative_control=enable_negative_control,
                        max_negative_control_attempts=max_negative_control_attempts,
                        max_negative_control_ast_edits=max_negative_control_ast_edits,
                        enable_runtime_target_reachability=enable_runtime_target_reachability,
                        enable_contrastive_observation_oracle=enable_contrastive_observation_oracle,
                        min_valid_surrogate_patches_for_consensus=min_valid_surrogate_patches_for_consensus,
                        surrogate_consensus_threshold=surrogate_consensus_threshold,
                        counterfactual_evidence_mode=counterfactual_evidence_mode,
                        final_reason="repository worktree/setup failed before BRT generation",
                        seed_mode="adaptive_top3",
                    )
                    result.save_json(str(Path(output_dir) / "summary.json"))
                    return result
            seed_root = Path(output_dir) / "seed_candidates"
            ensure_dir(seed_root)
            shared_archive_path = str(Path(output_dir) / "candidate_archive.json")
            remaining_extra_budget = max(0, max_extra_unique_candidates)
            attempts: list[dict[str, Any]] = []
            switch_reasons: list[str] = []
            best: tuple[
                tuple[int, ...], Path, dict[str, Any], dict[str, Any]
            ] | None = None
            for seed_index, seed in enumerate(seeds_to_try):
                seed_dir = seed_root / f"seed_{seed_index}"
                ensure_dir(seed_dir)
                behavior.save_json(str(seed_dir / "behavior_target.json"))
                seed_context = copy.deepcopy(context)
                assigned_extra_budget = remaining_extra_budget
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
                    counterfactual_shadow_mode,
                    enable_bidirectional_counterfactual_validation,
                    enable_negative_control,
                    max_negative_control_attempts,
                    max_negative_control_ast_edits,
                    enable_runtime_target_reachability,
                    enable_contrastive_observation_oracle,
                    min_valid_surrogate_patches_for_consensus,
                    surrogate_consensus_threshold,
                    counterfactual_evidence_mode,
                    enable_adaptive_typed_search,
                    enable_structured_observation_extractor,
                    enable_minimal_oracle_search,
                    enable_trigger_search,
                    enable_duplicate_aware_archive,
                    enable_optional_recomposition,
                    enable_selector_v2,
                    assigned_extra_budget,
                    max_trigger_search_candidates,
                    max_minimal_oracle_candidates,
                    max_protocol_repair_candidates,
                    max_recomposition_candidates,
                    _adaptive_disabled=True,
                    _forced_seed_index=seed_index,
                    _prepared_repo_path=prepared_repo_path,
                    _prepare_meta=prepared_meta,
                    _shared_archive_path=shared_archive_path,
                )
                summary_path = seed_dir / "summary.json"
                try:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    summary = result.to_dict()
                checkpoint = _best_checkpoint_from_summary(seed_dir)
                score = _seed_result_score(summary, checkpoint)
                archive_summary = summary.get("candidate_archive_summary")
                unique_by_origin = (
                    archive_summary.get("unique_by_origin")
                    if isinstance(archive_summary, dict)
                    else {}
                )
                ats_origins = {
                    "protocol_repair",
                    "trigger_search",
                    "minimal_oracle_conservative",
                    "minimal_oracle_public",
                    "recomposition",
                }
                total_ats_unique = sum(
                    int((unique_by_origin or {}).get(origin) or 0)
                    for origin in ats_origins
                )
                previously_used = max(0, max_extra_unique_candidates - remaining_extra_budget)
                extra_unique_used = max(0, total_ats_unique - previously_used)
                extra_unique_used = min(remaining_extra_budget, extra_unique_used)
                remaining_extra_budget -= extra_unique_used
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
                    "selector_v2_rank": checkpoint.get("selector_v2_rank") or [],
                    "adaptive_search_summary": summary.get("adaptive_search_summary") or {},
                    "extra_unique_budget_assigned": assigned_extra_budget,
                    "extra_unique_used": extra_unique_used,
                    "extra_unique_budget_remaining": remaining_extra_budget,
                }
                attempts.append(attempt)
                selector_rank = tuple(
                    int(value) for value in (checkpoint.get("selector_v2_rank") or [])
                )
                order_key = (
                    (1,) + selector_rank + (score, -seed_index, -int(checkpoint.get("round_id") or 0))
                    if enable_selector_v2 and selector_rank
                    else (0, score, -seed_index, -int(checkpoint.get("round_id") or 0))
                )
                attempts[-1]["instance_selector_order_key"] = list(order_key)
                if best is None or order_key > best[0]:
                    best = (order_key, seed_dir, summary, checkpoint)
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
            _, selected_dir, selected_summary, selected_checkpoint = best
            selected_seed_index = int(selected_dir.name.rsplit("_", 1)[-1])
            for name in (
                "final_test.py",
                "summary.json",
                "host_context.json",
                "protocol_recovery.json",
                "candidate_ranking.json",
                "candidate_archive.json",
                "adaptive_search_trace.json",
                "structured_observations.json",
                "unique_candidate_summary.json",
                "selector_v2_ranking.json",
                "dual_version_result.json",
                "counterfactual_summary.json",
                "counterfactual",
                "repo_prepare.json",
                "icore_exec_spec.json",
                "worktree",
            ):
                _copy_if_exists(selected_dir, Path(output_dir), name)
            try:
                aggregate_archive_summary = json.loads(
                    (Path(output_dir) / "unique_candidate_summary.json").read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, json.JSONDecodeError):
                aggregate_archive_summary = selected_summary.get(
                    "candidate_archive_summary"
                ) or {}
            aggregate_branch_counts = {
                "protocol_repair": 0,
                "trigger_search": 0,
                "minimal_oracle_search": 0,
                "optional_recomposition": 0,
            }
            for attempt in attempts:
                adaptive_summary = attempt.get("adaptive_search_summary") or {}
                for action, count in (adaptive_summary.get("branch_counts") or {}).items():
                    aggregate_branch_counts[action] = (
                        aggregate_branch_counts.get(action, 0) + int(count or 0)
                    )
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
                    "counterfactual_summary": selected_summary.get("counterfactual_summary")
                    or selected_checkpoint.get("counterfactual_summary")
                    or {},
                    "candidate_archive_summary": aggregate_archive_summary,
                    "adaptive_search_summary": {
                        "branch_counts": aggregate_branch_counts,
                        "seed_attempts": len(attempts),
                        "max_extra_unique_candidates_per_instance": max_extra_unique_candidates,
                        "remaining_extra_unique_budget": remaining_extra_budget,
                    },
                }
            )
            safe_json_dump(attempts, str(Path(output_dir) / "seed_attempts_summary.json"))
            root_ranking_path = Path(output_dir) / "candidate_ranking.json"
            try:
                root_ranking = json.loads(root_ranking_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                root_ranking = {}
            root_ranking.update(
                {
                    "instance_selector_version": "selector_v2",
                    "selected_seed_index": selected_seed_index,
                    "selected_candidate_id": selected_checkpoint.get("candidate_id") or "",
                    "seed_rankings": attempts,
                    "max_extra_unique_candidates_per_instance": max_extra_unique_candidates,
                    "remaining_extra_unique_budget": remaining_extra_budget,
                    "counterfactual_fields_used_by_selector_v2": False,
                }
            )
            safe_json_dump(root_ranking, str(root_ranking_path))
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
                counterfactual_summary=selected_summary.get("counterfactual_summary") or {},
                method_name=str(selected_summary.get("method_name") or "ATS-BRT"),
                enable_adaptive_typed_search=enable_adaptive_typed_search,
                enable_structured_observation_extractor=enable_structured_observation_extractor,
                enable_minimal_oracle_search=enable_minimal_oracle_search,
                enable_trigger_search=enable_trigger_search,
                enable_duplicate_aware_archive=enable_duplicate_aware_archive,
                enable_optional_recomposition=enable_optional_recomposition,
                enable_selector_v2=enable_selector_v2,
                max_extra_unique_candidates=max_extra_unique_candidates,
                max_trigger_search_candidates=max_trigger_search_candidates,
                max_minimal_oracle_candidates=max_minimal_oracle_candidates,
                max_protocol_repair_candidates=max_protocol_repair_candidates,
                max_recomposition_candidates=max_recomposition_candidates,
                candidate_archive_summary=selected_summary.get("candidate_archive_summary") or {},
                adaptive_search_summary=selected_summary.get("adaptive_search_summary") or {},
                enable_bidirectional_counterfactual_validation=enable_bidirectional_counterfactual_validation,
                counterfactual_shadow_mode=counterfactual_shadow_mode,
                enable_negative_control=enable_negative_control,
                max_negative_control_attempts=max_negative_control_attempts,
                max_negative_control_ast_edits=max_negative_control_ast_edits,
                enable_runtime_target_reachability=enable_runtime_target_reachability,
                enable_contrastive_observation_oracle=enable_contrastive_observation_oracle,
                min_valid_surrogate_patches_for_consensus=min_valid_surrogate_patches_for_consensus,
                surrogate_consensus_threshold=surrogate_consensus_threshold,
                counterfactual_evidence_mode=counterfactual_evidence_mode,
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
                    enable_bidirectional_counterfactual_validation=enable_bidirectional_counterfactual_validation,
                    counterfactual_shadow_mode=counterfactual_shadow_mode,
                    enable_negative_control=enable_negative_control,
                    max_negative_control_attempts=max_negative_control_attempts,
                    max_negative_control_ast_edits=max_negative_control_ast_edits,
                    enable_runtime_target_reachability=enable_runtime_target_reachability,
                    enable_contrastive_observation_oracle=enable_contrastive_observation_oracle,
                    min_valid_surrogate_patches_for_consensus=min_valid_surrogate_patches_for_consensus,
                    surrogate_consensus_threshold=surrogate_consensus_threshold,
                    counterfactual_evidence_mode=counterfactual_evidence_mode,
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
        archive = (
            CandidateArchive(
                context.instance_id,
                output_dir,
                shared_archive_path=_shared_archive_path,
            )
            if enable_duplicate_aware_archive or enable_adaptive_typed_search or enable_selector_v2
            else None
        )
        candidate_history: list[CandidateTest] = []
        adaptive_search_trace: list[dict[str, Any]] = []
        branch_counts = {
            "protocol_repair": 0,
            "trigger_search": 0,
            "minimal_oracle_search": 0,
            "optional_recomposition": 0,
        }
        search_budgets = SearchBudgets(
            extra_unique=max(0, max_extra_unique_candidates),
            protocol=max(0, max_protocol_repair_candidates),
            trigger=max(0, max_trigger_search_candidates),
            oracle=max(0, max_minimal_oracle_candidates),
            recomposition=max(0, max_recomposition_candidates),
        )

        def archive_candidate(current: CandidateTest) -> tuple[Any, Any]:
            segments = segment_test(current.code, behavior, context.instance_id)
            safe_json_dump(
                segments.to_dict(),
                str(Path(output_dir) / f"segments_round_{current.round_id}.json"),
            )
            current.lineage = dict(current.lineage or {})
            current.lineage["segment_hashes"] = {
                "scaffold": segments.scaffold_hash,
                "trigger": segments.trigger_hash,
                "oracle": segments.oracle_hash,
            }
            entry = None
            if archive is not None:
                candidate_id = str(current.lineage.get("candidate_id") or "")
                entry = next(
                    (item for item in reversed(archive.entries) if item.candidate_id == candidate_id),
                    None,
                )
                if entry is None:
                    entry = archive.register(current, segments)
            if not any(
                str((item.lineage or {}).get("candidate_id") or "")
                == str(current.lineage.get("candidate_id") or "")
                for item in candidate_history
            ):
                candidate_history.append(copy.deepcopy(current))
            return segments, entry

        def execution_from_archive(data: dict[str, Any]) -> ExecutionResult:
            fields = ExecutionResult.__dataclass_fields__
            return ExecutionResult(
                **{key: value for key, value in data.items() if key in fields}
            )

        current_segments, current_archive_entry = archive_candidate(candidate)
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
                enable_bidirectional_counterfactual_validation=enable_bidirectional_counterfactual_validation,
                counterfactual_shadow_mode=counterfactual_shadow_mode,
                enable_negative_control=enable_negative_control,
                max_negative_control_attempts=max_negative_control_attempts,
                max_negative_control_ast_edits=max_negative_control_ast_edits,
                enable_runtime_target_reachability=enable_runtime_target_reachability,
                enable_contrastive_observation_oracle=enable_contrastive_observation_oracle,
                min_valid_surrogate_patches_for_consensus=min_valid_surrogate_patches_for_consensus,
                surrogate_consensus_threshold=surrogate_consensus_threshold,
                counterfactual_evidence_mode=counterfactual_evidence_mode,
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
            current_segments, current_archive_entry = archive_candidate(candidate)
            reused_duplicate = bool(
                current_archive_entry is not None
                and current_archive_entry.duplicate_status == "CODE_DUPLICATE"
                and archive is not None
            )
            duplicate_source = (
                next(
                    (
                        item
                        for item in archive.entries
                        if item.candidate_id == current_archive_entry.duplicate_of
                        and item.executed
                    ),
                    None,
                )
                if reused_duplicate and archive is not None
                else None
            )
            if duplicate_source is not None:
                execution = execution_from_archive(duplicate_source.buggy_execution)
                archive.record_redirect(
                    current_archive_entry,
                    "protocol_repair",
                    "exact AST duplicate reused prior execution during environment qualification",
                )
            else:
                execution = run_command_in_conda(candidate.command, context.buggy_repo_path, conda_env, timeout, no_conda, behavior, context.instance_id)
            safe_json_dump(execution.to_dict(), str(Path(output_dir) / f"env_execution_round_{env_round}.json"))
            write_text(str(Path(output_dir) / "logs" / f"env_execution_round_{env_round}.log"), execution.stdout + "\n" + execution.stderr)
            env_rounds_used = env_round + 1
            if archive is not None and current_archive_entry is not None:
                archive.finalize(
                    current_archive_entry.candidate_id,
                    candidate.code,
                    current_segments,
                    execution.to_dict(),
                    {},
                    {},
                    {},
                    {},
                )
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
                enable_bidirectional_counterfactual_validation=enable_bidirectional_counterfactual_validation,
                counterfactual_shadow_mode=counterfactual_shadow_mode,
                enable_negative_control=enable_negative_control,
                max_negative_control_attempts=max_negative_control_attempts,
                max_negative_control_ast_edits=max_negative_control_ast_edits,
                enable_runtime_target_reachability=enable_runtime_target_reachability,
                enable_contrastive_observation_oracle=enable_contrastive_observation_oracle,
                min_valid_surrogate_patches_for_consensus=min_valid_surrogate_patches_for_consensus,
                surrogate_consensus_threshold=surrogate_consensus_threshold,
                counterfactual_evidence_mode=counterfactual_evidence_mode,
                selected_seed_file=related_test.file if related_test else "",
                selected_seed_name=related_test.name if related_test else "",
                seed_fallback_used=seed_fallback_used,
                mutation_ops=[op for plan in mutation_plans for op in plan.mutation_ops],
                final_reason="environment qualification remained unresolved",
            )
            result.save_json(str(Path(output_dir) / "summary.json"))
            return result
        else:
            if archive is not None:
                search_budgets.starting_unique = archive.unique_count
            brt_attempt = 0
            semantic_repairs_used = 0
            late_setup_repairs_used = 0
            # Round 0 is the initial BRT. Environment qualification already
            # has its own budget above and must not expand this checkpoint loop.
            max_brt_attempts = (
                1 + max(0, max_extra_unique_candidates) + 4
                if enable_adaptive_typed_search
                else 1 + max(0, brt_budget)
            )
            unique_budget_limit = (
                search_budgets.starting_unique
                + max(0, max_extra_unique_candidates)
            )
            checkpoints: list[CandidateCheckpoint] = []
            checkpoint_candidates: list[CandidateTest] = []
            checkpoint_executions: list[ExecutionResult] = []
            checkpoint_decisions: list[VerifierDecision] = []
            checkpoint_duals: list[DualVersionResult | None] = []
            checkpoint_observations: list[Any] = []
            checkpoint_strict_results: list[Any] = []
            best_key: tuple[int, ...] | None = None
            best_index = -1
            best_candidate = None
            best_execution = None
            best_decision = None
            best_dual = None
            best_observation = None
            best_strict_result = None
            best_counterfactual_summary: dict[str, Any] = {}
            best_counterfactual_evidence: dict[str, Any] = {}
            consecutive_nonunique_searches = 0
            consecutive_behavior_duplicates = 0

            def generate_adaptive_candidate(
                search_decision: Any,
                parent: CandidateTest,
                parent_execution: ExecutionResult,
                next_round: int,
                duplicate_redirected_from: str = "",
            ) -> tuple[CandidateTest | None, dict[str, Any]]:
                action = str(search_decision.action or "stop")
                variant = str(search_decision.search_action or "")
                before_segments = segment_test(parent.code, behavior, context.instance_id)
                search_context = search_prompt_context(
                    action, variant, behavior, before_segments.to_dict()
                )
                search_context["duplicate_redirected_from"] = duplicate_redirected_from
                new_candidate: CandidateTest | None = None
                local_observation = None
                if action == "protocol_repair":
                    new_candidate = repair_candidate(
                        context.instance_id,
                        behavior,
                        host,
                        parent,
                        parent_execution,
                        llm_client,
                        output_dir,
                        next_round,
                        "setup",
                        context.retrieved_code,
                        verifier_feedback=search_decision.to_dict(),
                        buggy_repo=context.buggy_repo_path,
                        protocol=protocol,
                        search_context=search_context,
                        origin_override="protocol_repair",
                    )
                elif action == "trigger_search" and enable_trigger_search:
                    plan = build_mutation_plan(
                        context.instance_id,
                        next_round,
                        behavior,
                        host,
                        protocol,
                        llm_client,
                        output_dir,
                        parent_execution.stdout + "\n" + parent_execution.stderr,
                        search_decision.to_dict(),
                    ) if enable_seed_mutation else None
                    if plan is not None:
                        mutation_plans.append(plan)
                    new_candidate = repair_candidate(
                        context.instance_id,
                        behavior,
                        host,
                        parent,
                        parent_execution,
                        llm_client,
                        output_dir,
                        next_round,
                        "trigger",
                        context.retrieved_code,
                        verifier_feedback=search_decision.to_dict(),
                        buggy_repo=context.buggy_repo_path,
                        protocol=protocol,
                        mutation_plan=plan,
                        search_context=search_context,
                        origin_override="trigger_search",
                    )
                elif action == "minimal_oracle_search" and enable_minimal_oracle_search:
                    if enable_structured_observation_extractor:
                        local_observation = collect_structured_observation(
                            behavior,
                            parent,
                            output_dir,
                            context.buggy_repo_path,
                            conda_env,
                            timeout,
                            no_conda,
                            context.repo,
                            str(context.metadata.get("version") or ""),
                        )
                        search_context["observation_id"] = local_observation.observation_id
                    if local_observation is not None and local_observation.status == "COLLECTED":
                        new_candidate = repair_candidate(
                            context.instance_id,
                            behavior,
                            host,
                            parent,
                            parent_execution,
                            llm_client,
                            output_dir,
                            next_round,
                            "oracle",
                            context.retrieved_code,
                            json.dumps(local_observation.to_dict(), ensure_ascii=False),
                            search_decision.to_dict(),
                            context.buggy_repo_path,
                            protocol,
                            search_context=search_context,
                            origin_override=(
                                "minimal_oracle_conservative"
                                if variant == "conservative"
                                else "minimal_oracle_public"
                            ),
                        )
                    else:
                        new_candidate, fallback_report, _ = rebind_observation_oracle(
                            behavior,
                            protocol,
                            parent,
                            parent_execution.stdout + "\n" + parent_execution.stderr,
                            llm_client,
                            output_dir,
                            context.buggy_repo_path,
                            conda_env,
                            timeout,
                            no_conda,
                            context.repo,
                            str(context.metadata.get("version") or ""),
                            next_round,
                            contrastive_context=None,
                            enable_contrastive_observation=False,
                        )
                        new_candidate.lineage = dict(new_candidate.lineage or {})
                        new_candidate.lineage.update(
                            {
                                "origin": (
                                    "minimal_oracle_conservative"
                                    if variant == "conservative"
                                    else "minimal_oracle_public"
                                ),
                                "search_action": variant,
                                "observation_id": str(
                                    getattr(local_observation, "observation_id", "")
                                ),
                                "observation_context_used": "llm_probe_fallback",
                            }
                        )
                elif action == "optional_recomposition" and enable_optional_recomposition and archive is not None:
                    new_candidate, recomposition_meta = recompose_candidate(
                        behavior,
                        candidate_history,
                        archive.unique_entries(),
                        next_round,
                    )
                    safe_json_dump(
                        recomposition_meta,
                        str(Path(output_dir) / f"recomposition_round_{next_round}.json"),
                    )
                    if new_candidate is not None:
                        write_text(new_candidate.candidate_file_path, new_candidate.code)
                if new_candidate is None:
                    return None, {
                        "valid": False,
                        "action": action,
                        "reason": "search action disabled or abstained",
                    }
                if action == "optional_recomposition":
                    after_segments = segment_test(
                        new_candidate.code, behavior, context.instance_id
                    )
                    validation = {
                        "valid": (
                            not after_segments.parse_error
                            and after_segments.test_entry_count == 1
                            and bool(after_segments.target_call_locations)
                        ),
                        "action": action,
                        "target_api_preserved": bool(
                            after_segments.target_call_locations
                        ),
                    }
                else:
                    validation = validate_typed_transformation(
                        parent.code, new_candidate.code, behavior, action
                    )
                    if not validation.get("valid"):
                        proposal_code = new_candidate.code
                        write_text(
                            str(
                                Path(output_dir)
                                / f"candidate_round_{next_round}_proposal.py"
                            ),
                            proposal_code,
                        )
                        transplanted_code, transplant_report = transplant_typed_segment(
                            parent.code,
                            proposal_code,
                            behavior,
                            action,
                        )
                        validation["initial_validation"] = dict(validation)
                        validation["transplant"] = transplant_report
                        if transplanted_code is not None:
                            transplanted_validation = validate_typed_transformation(
                                parent.code,
                                transplanted_code,
                                behavior,
                                action,
                            )
                            transplanted_validation["initial_validation"] = validation[
                                "initial_validation"
                            ]
                            transplanted_validation["transplant"] = transplant_report
                            transplanted_validation["transplant_applied"] = True
                            validation = transplanted_validation
                            if validation.get("valid"):
                                new_candidate.code = transplanted_code
                                new_candidate.lineage = dict(
                                    new_candidate.lineage or {}
                                )
                                new_candidate.lineage["typed_transplant_applied"] = True
                validation["search_action"] = variant
                safe_json_dump(
                    validation,
                    str(Path(output_dir) / f"typed_validation_round_{next_round}.json"),
                )
                if not validation.get("valid"):
                    write_text(parent.candidate_file_path, parent.code)
                    return None, validation
                new_candidate.round_id = next_round
                new_candidate.lineage = dict(new_candidate.lineage or {})
                new_candidate.lineage["duplicate_redirected_from"] = duplicate_redirected_from
                write_text(new_candidate.candidate_file_path, new_candidate.code)
                write_text(
                    str(Path(output_dir) / f"candidate_round_{next_round}.py"),
                    new_candidate.code,
                )
                _refresh_candidate_command(context, new_candidate)
                return new_candidate, validation

            while brt_attempt < max_brt_attempts:
                current_segments, current_archive_entry = archive_candidate(candidate)
                duplicate_source = None
                if (
                    archive is not None
                    and current_archive_entry is not None
                    and current_archive_entry.duplicate_status == "CODE_DUPLICATE"
                ):
                    duplicate_source = next(
                        (
                            item
                            for item in archive.entries
                            if item.candidate_id == current_archive_entry.duplicate_of
                            and item.executed
                        ),
                        None,
                    )
                if duplicate_source is not None and enable_adaptive_typed_search:
                    duplicate_decision = decide_search_action(
                        duplicate_source.buggy_execution,
                        duplicate_source.verifier_decision,
                        str(
                            duplicate_source.target_evidence.get("target_hit")
                            or "unknown"
                        ),
                        duplicate_source.oracle_risk,
                        duplicate_source.surrogate_result,
                        branch_counts,
                        archive,
                        search_budgets,
                        duplicate=True,
                        allow_recomposition=enable_optional_recomposition,
                    )
                    trace_item = duplicate_decision.to_dict()
                    trace_item.update(
                        {
                            "round": brt_attempt,
                            "candidate_id": current_archive_entry.candidate_id,
                            "duplicate_of": duplicate_source.candidate_id,
                        }
                    )
                    adaptive_search_trace.append(trace_item)
                    safe_json_dump(
                        adaptive_search_trace,
                        str(Path(output_dir) / "adaptive_search_trace.json"),
                    )
                    if duplicate_decision.action == "stop":
                        break
                    branch_counts[duplicate_decision.action] += 1
                    archive.record_redirect(
                        current_archive_entry,
                        duplicate_decision.action,
                        duplicate_decision.reason,
                    )
                    next_round = max(candidate.round_id + 1, brt_attempt + 1)
                    parent_execution = execution_from_archive(
                        duplicate_source.buggy_execution
                    )
                    redirected, validation = generate_adaptive_candidate(
                        duplicate_decision,
                        candidate,
                        parent_execution,
                        next_round,
                        duplicate_source.candidate_id,
                    )
                    trace_item["transformation_validation"] = validation
                    safe_json_dump(
                        adaptive_search_trace,
                        str(Path(output_dir) / "adaptive_search_trace.json"),
                    )
                    if redirected is not None:
                        candidate = redirected
                        consecutive_nonunique_searches = 0
                    else:
                        consecutive_nonunique_searches += 1
                        trace_item["consecutive_nonunique_searches"] = (
                            consecutive_nonunique_searches
                        )
                        if consecutive_nonunique_searches >= 2:
                            trace_item["early_stop"] = (
                                "two consecutive searches produced no unique candidate"
                            )
                            safe_json_dump(
                                adaptive_search_trace,
                                str(Path(output_dir) / "adaptive_search_trace.json"),
                            )
                            break
                    brt_attempt += 1
                    continue
                if brt_attempt > 0 or execution is None:
                    execution = run_command_in_conda(candidate.command, context.buggy_repo_path, conda_env, timeout, no_conda, behavior, context.instance_id)
                safe_json_dump(execution.to_dict(), str(Path(output_dir) / f"execution_round_{brt_attempt}.json"))
                write_text(str(Path(output_dir) / "logs" / f"execution_round_{brt_attempt}.log"), execution.stdout + "\n" + execution.stderr)
                effective_source = format_effective_source_context(
                    behavior, context.retrieved_code, context.buggy_repo_path
                )
                reachability = (
                    collect_target_reachability(behavior, candidate, execution)
                    if enable_runtime_target_reachability
                    else None
                )
                runtime_target_hit = (
                    reachability.target_hit if reachability is not None else "unknown"
                )
                if enable_strict_semantic_verifier:
                    decision, strict_result = verify_strict_semantics(
                        context.issue_text, behavior, protocol, candidate,
                        execution, effective_source, llm_client, output_dir,
                        brt_attempt,
                        runtime_target_hit=runtime_target_hit,
                        runtime_target_evidence=(
                            reachability.to_dict() if reachability else {}
                        ),
                    )
                else:
                    decision = verify_buggy_only(
                        context.issue_text, behavior, candidate, execution,
                        llm_client, host.to_dict(), effective_source,
                    )
                safe_json_dump(decision.to_dict(), str(Path(output_dir) / f"verifier_round_{brt_attempt}.json"))
                candidate_dual = None
                negative_candidate = None
                negative_execution = None
                negative_metadata = None
                negative_reachability = None
                cf_evidence: dict[str, Any] | None = None
                cf_summary = counterfactual_summary(
                    enable_bidirectional_counterfactual_validation,
                    None,
                    None,
                    selected_candidate_id=str(brt_attempt),
                    fallback_used=not enable_bidirectional_counterfactual_validation,
                    shadow_mode=counterfactual_shadow_mode,
                )
                cf_attempt_dir = (
                    Path(output_dir) / "counterfactual" / f"attempt_{brt_attempt}"
                )
                if enable_bidirectional_counterfactual_validation:
                    ensure_dir(cf_attempt_dir)
                    safe_json_dump(
                        execution.to_dict(),
                        str(cf_attempt_dir / "positive_buggy_execution.json"),
                    )
                    if reachability is not None:
                        safe_json_dump(
                            reachability.to_dict(),
                            str(cf_attempt_dir / "target_reachability.json"),
                        )
                    if enable_negative_control and max_negative_control_attempts > 0:
                        plan = build_counterfactual_plan(
                            behavior,
                            candidate,
                            str(cf_attempt_dir),
                            max_negative_control_ast_edits,
                        )
                        negative_candidate, negative_metadata = generate_negative_control(
                            behavior,
                            plan,
                            candidate,
                            str(cf_attempt_dir),
                            context.repo,
                            str(context.metadata.get("version") or ""),
                        )
                        if (
                            negative_candidate is not None
                            and negative_metadata is not None
                            and negative_metadata.status == "VALID"
                        ):
                            negative_execution = run_command_in_conda(
                                negative_candidate.command,
                                context.buggy_repo_path,
                                conda_env,
                                timeout,
                                no_conda,
                                behavior,
                                context.instance_id,
                            )
                            safe_json_dump(
                                negative_execution.to_dict(),
                                str(cf_attempt_dir / "negative_buggy_execution.json"),
                            )
                            negative_reachability = (
                                collect_target_reachability(
                                    behavior, negative_candidate, negative_execution
                                )
                                if enable_runtime_target_reachability
                                else None
                            )
                            if negative_reachability is not None:
                                safe_json_dump(
                                    negative_reachability.to_dict(),
                                    str(cf_attempt_dir / "negative_target_reachability.json"),
                                )
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
                            negative_candidate=negative_candidate,
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
                if enable_bidirectional_counterfactual_validation:
                    semantic_target_hit = (
                        strict_result.semantic_target_hit
                        if strict_result is not None
                        else ("true" if decision.decision == "accept" else "unknown")
                    )
                    cf_evidence_obj = build_counterfactual_evidence(
                        behavior,
                        execution,
                        negative_execution,
                        negative_metadata,
                        reachability,
                        semantic_target_hit,
                        candidate_dual,
                        min_valid_surrogate_patches_for_consensus,
                        surrogate_consensus_threshold,
                        positive_issue_aligned=(
                            decision.decision == "accept"
                            or (strict_result is not None and strict_result.failure_class == "issue_aligned")
                        ),
                        negative_reachability=negative_reachability,
                    )
                    cf_evidence = cf_evidence_obj.to_dict()
                    safe_json_dump(
                        cf_evidence,
                        str(cf_attempt_dir / "counterfactual_evidence.json"),
                    )
                    cf_summary = counterfactual_summary(
                        True,
                        negative_metadata,
                        cf_evidence_obj,
                        selected_candidate_id=str(brt_attempt),
                        shadow_mode=counterfactual_shadow_mode,
                    )
                    safe_json_dump(
                        cf_summary,
                        str(cf_attempt_dir / "counterfactual_summary.json"),
                    )
                    if not counterfactual_shadow_mode:
                        decision = _counterfactual_guided_decision(
                            decision,
                            execution,
                            runtime_target_hit,
                            cf_evidence,
                            counterfactual_evidence_mode,
                        )
                    safe_json_dump(
                        decision.to_dict(),
                        str(Path(output_dir) / f"verifier_round_{brt_attempt}.json"),
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
                    counterfactual_evidence=cf_evidence,
                    counterfactual_summary_data=cf_summary,
                    protocol_valid=not bool(protocol and protocol.protocol_risks),
                    counterfactual_shadow_mode=counterfactual_shadow_mode,
                    lineage=candidate.lineage,
                    archive_entry=(
                        current_archive_entry.to_dict()
                        if current_archive_entry is not None
                        else {}
                    ),
                )
                finalized_entry = None
                if archive is not None and current_archive_entry is not None:
                    finalized_entry = archive.finalize(
                        current_archive_entry.candidate_id,
                        candidate.code,
                        current_segments,
                        execution.to_dict(),
                        decision.to_dict(),
                        reachability.to_dict() if reachability is not None else {},
                        checkpoint.oracle_risk,
                        candidate_dual.to_dict() if candidate_dual is not None else {},
                    )
                    if finalized_entry is not None:
                        checkpoint.archive_entry = finalized_entry.to_dict()
                        checkpoint.candidate_id = finalized_entry.candidate_id
                        checkpoint.save_json(
                            str(
                                Path(output_dir)
                                / "checkpoints"
                                / f"candidate_attempt_{brt_attempt}.json"
                            )
                        )
                if (
                    finalized_entry is not None
                    and finalized_entry.duplicate_status == "BEHAVIOR_DUPLICATE"
                ):
                    consecutive_behavior_duplicates += 1
                else:
                    consecutive_behavior_duplicates = 0
                checkpoints.append(checkpoint)
                checkpoint_candidates.append(copy.deepcopy(candidate))
                checkpoint_executions.append(copy.deepcopy(execution))
                checkpoint_decisions.append(copy.deepcopy(decision))
                checkpoint_duals.append(copy.deepcopy(candidate_dual))
                checkpoint_observations.append(copy.deepcopy(observation))
                checkpoint_strict_results.append(copy.deepcopy(strict_result))
                checkpoint_key = (
                    _legacy_checkpoint_order_key(checkpoint)
                    if counterfactual_shadow_mode
                    else _counterfactual_checkpoint_order_key(checkpoint)
                )
                if best_key is None or checkpoint_key > best_key:
                    best_key = checkpoint_key
                    best_index = len(checkpoints) - 1
                    best_candidate = copy.deepcopy(candidate)
                    best_execution = copy.deepcopy(execution)
                    best_decision = copy.deepcopy(decision)
                    best_dual = copy.deepcopy(candidate_dual)
                    best_observation = copy.deepcopy(observation)
                    best_strict_result = copy.deepcopy(strict_result)
                    best_counterfactual_summary = copy.deepcopy(cf_summary)
                    best_counterfactual_evidence = copy.deepcopy(cf_evidence or {})
                if enable_adaptive_typed_search and archive is not None:
                    if consecutive_behavior_duplicates >= 2:
                        adaptive_search_trace.append(
                            {
                                "action": "stop",
                                "search_action": "",
                                "reason": (
                                    "two consecutive candidates had duplicate "
                                    "behavior signatures"
                                ),
                                "round": brt_attempt,
                                "candidate_id": checkpoint.candidate_id,
                                "early_stop": "consecutive_behavior_duplicates",
                                "consecutive_behavior_duplicates": (
                                    consecutive_behavior_duplicates
                                ),
                            }
                        )
                        safe_json_dump(
                            adaptive_search_trace,
                            str(Path(output_dir) / "adaptive_search_trace.json"),
                        )
                        break
                    if archive.unique_count >= unique_budget_limit:
                        search_decision = decide_search_action(
                            execution.to_dict(),
                            decision.to_dict(),
                            runtime_target_hit,
                            checkpoint.oracle_risk,
                            candidate_dual.to_dict() if candidate_dual else {},
                            branch_counts,
                            archive,
                            search_budgets,
                            duplicate=bool(
                                finalized_entry is not None
                                and finalized_entry.duplicate_status
                                == "BEHAVIOR_DUPLICATE"
                            ),
                            allow_recomposition=enable_optional_recomposition,
                        )
                        search_decision.action = "stop"
                        search_decision.search_action = ""
                        search_decision.reason = "per-instance unique candidate budget exhausted"
                    else:
                        search_decision = decide_search_action(
                            execution.to_dict(),
                            decision.to_dict(),
                            runtime_target_hit,
                            checkpoint.oracle_risk,
                            candidate_dual.to_dict() if candidate_dual else {},
                            branch_counts,
                            archive,
                            search_budgets,
                            duplicate=bool(
                                finalized_entry is not None
                                and finalized_entry.duplicate_status
                                == "BEHAVIOR_DUPLICATE"
                            ),
                            allow_recomposition=enable_optional_recomposition,
                        )
                    if search_decision.action == "trigger_search" and not enable_trigger_search:
                        search_decision.action = "stop"
                        search_decision.reason = "trigger search is disabled"
                    if search_decision.action == "minimal_oracle_search" and not enable_minimal_oracle_search:
                        search_decision.action = "stop"
                        search_decision.reason = "minimal oracle search is disabled"
                    if search_decision.action == "optional_recomposition" and not enable_optional_recomposition:
                        search_decision.action = "stop"
                        search_decision.reason = "optional recomposition is disabled"
                    trace_item = search_decision.to_dict()
                    trace_item.update(
                        {
                            "round": brt_attempt,
                            "candidate_id": checkpoint.candidate_id,
                            "archive_unique_count": archive.unique_count,
                            "unique_budget_limit": unique_budget_limit,
                        }
                    )
                    adaptive_search_trace.append(trace_item)
                    safe_json_dump(
                        adaptive_search_trace,
                        str(Path(output_dir) / "adaptive_search_trace.json"),
                    )
                    if search_decision.action == "stop":
                        break
                    branch_counts[search_decision.action] += 1
                    next_round = max(candidate.round_id + 1, brt_attempt + 1)
                    searched_candidate, validation = generate_adaptive_candidate(
                        search_decision,
                        candidate,
                        execution,
                        next_round,
                    )
                    trace_item["transformation_validation"] = validation
                    safe_json_dump(
                        adaptive_search_trace,
                        str(Path(output_dir) / "adaptive_search_trace.json"),
                    )
                    if searched_candidate is None:
                        consecutive_nonunique_searches += 1
                        trace_item["consecutive_nonunique_searches"] = (
                            consecutive_nonunique_searches
                        )
                        if consecutive_nonunique_searches >= 2:
                            trace_item["early_stop"] = (
                                "two consecutive searches produced no unique candidate"
                            )
                            safe_json_dump(
                                adaptive_search_trace,
                                str(Path(output_dir) / "adaptive_search_trace.json"),
                            )
                            break
                        # Feed the unchanged code through archive dedup on the next
                        # iteration so the controller redirects to another branch.
                        candidate = copy.deepcopy(candidate)
                        candidate.round_id = next_round
                        candidate.lineage = dict(candidate.lineage or {})
                        candidate.lineage.pop("candidate_id", None)
                        candidate.lineage["origin"] = "duplicate_redirect"
                        candidate.lineage["search_action"] = search_decision.search_action
                        candidate.lineage["duplicate_redirected_from"] = checkpoint.candidate_id
                    else:
                        candidate = searched_candidate
                        consecutive_nonunique_searches = 0
                    brt_attempt += 1
                    continue
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
                            contrastive_context=_contrastive_observation_context(cf_evidence),
                            enable_contrastive_observation=enable_contrastive_observation_oracle,
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
                legacy_order = sorted(
                    checkpoints,
                    key=_legacy_checkpoint_order_key,
                    reverse=True,
                )
                counterfactual_order = sorted(
                    checkpoints,
                    key=_counterfactual_checkpoint_order_key,
                    reverse=True,
                )
                legacy_best = legacy_order[0]
                counterfactual_would_best = counterfactual_order[0]
                ranking_changed = (
                    counterfactual_would_best.round_id != legacy_best.round_id
                )
                legacy_rank_by_round = {
                    item.round_id: index + 1
                    for index, item in enumerate(legacy_order)
                }
                for item in checkpoints:
                    item.legacy_rank = legacy_rank_by_round.get(item.round_id, 0)
                    item.legacy_selected = item.round_id == legacy_best.round_id
                    item.counterfactual_would_select = (
                        item.round_id == counterfactual_would_best.round_id
                    )
                    item.ranking_changed_in_shadow = (
                        counterfactual_shadow_mode and ranking_changed
                    )
                    item.ranking_change_reason = (
                        "counterfactual evidence rank would select a different candidate"
                        if item.ranking_changed_in_shadow
                        else ""
                    )
                selector_v2_summary: dict[str, Any] = {
                    "selector_version": "disabled",
                    "rankings": [],
                }
                if enable_selector_v2 and archive is not None:
                    selected_index, selector_v2_summary = select_checkpoint_v2(
                        checkpoints, archive
                    )
                    if selected_index >= 0:
                        best_index = selected_index
                        candidate = copy.deepcopy(checkpoint_candidates[best_index])
                        execution = copy.deepcopy(checkpoint_executions[best_index])
                        decision = copy.deepcopy(checkpoint_decisions[best_index])
                        dual = copy.deepcopy(checkpoint_duals[best_index])
                        observation = copy.deepcopy(checkpoint_observations[best_index])
                        strict_result = copy.deepcopy(checkpoint_strict_results[best_index])
                        best_candidate = copy.deepcopy(candidate)
                        best_execution = copy.deepcopy(execution)
                        best_decision = copy.deepcopy(decision)
                        best_dual = copy.deepcopy(dual)
                        best_observation = copy.deepcopy(observation)
                        best_strict_result = copy.deepcopy(strict_result)
                        final_code = candidate.code
                        write_text(candidate.candidate_file_path, candidate.code)
                        _refresh_candidate_command(context, candidate)
                    safe_json_dump(
                        selector_v2_summary,
                        str(Path(output_dir) / "selector_v2_ranking.json"),
                    )
                best_counterfactual_summary = dict(best_counterfactual_summary or {})
                best_counterfactual_summary.update(
                    {
                        "shadow_mode": counterfactual_shadow_mode,
                        "ranking_changed": (False if counterfactual_shadow_mode else ranking_changed),
                        "selected_candidate_id": str(checkpoints[best_index].round_id),
                        "legacy_selected_candidate_id": str(legacy_best.round_id),
                        "counterfactual_would_select_candidate_id": str(counterfactual_would_best.round_id),
                        "ranking_changed_in_shadow": (
                            counterfactual_shadow_mode and ranking_changed
                        ),
                    }
                )
                checkpoints[best_index].selected = True
                checkpoints[best_index].selection_changed_by_counterfactual = (
                    False if counterfactual_shadow_mode else ranking_changed
                )
                checkpoints[best_index].counterfactual_summary = best_counterfactual_summary
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
                            "ATS-BRT uses generation-only Selector V2; no 2x2 field participates in ranking"
                            if enable_selector_v2
                            else (
                            "shadow mode uses the legacy P0 selector for final choice; "
                            "counterfactual evidence rank is recorded as would-select evidence"
                            if counterfactual_shadow_mode
                            else (
                                "legacy risk-adjusted score remains the base score; "
                                "counterfactual evidence rank adds soft tie-breaking/bonus "
                                "without penalizing UNKNOWN or ABSTAIN negative controls"
                            )
                            )
                        ),
                        "counterfactual_shadow_mode": counterfactual_shadow_mode,
                        "legacy_selected_attempt": legacy_best.round_id,
                        "counterfactual_would_select_attempt": counterfactual_would_best.round_id,
                        "selected_attempt": checkpoints[best_index].round_id,
                        "selection_changed_by_counterfactual": (
                            False if counterfactual_shadow_mode else ranking_changed
                        ),
                        "ranking_changed_in_shadow": (
                            counterfactual_shadow_mode and ranking_changed
                        ),
                        "ranking_change_reason": (
                            "counterfactual evidence rank would select a different candidate"
                            if counterfactual_shadow_mode and ranking_changed
                            else ""
                        ),
                        "selected_reason": checkpoints[best_index].reason,
                        "selected_evidence_rank": checkpoints[best_index].evidence_rank,
                        "selector_v2": selector_v2_summary,
                        "selector_v2_selected_candidate_id": checkpoints[best_index].candidate_id,
                        "selector_v2_changed_from_legacy": (
                            checkpoints[best_index].round_id != legacy_best.round_id
                        ),
                        "counterfactual_fields_used_by_selector_v2": False,
                        "checkpoints": [item.to_dict() for item in checkpoints],
                    },
                    str(Path(output_dir) / "candidate_ranking.json"),
                )
                safe_json_dump(
                    best_counterfactual_summary,
                    str(Path(output_dir) / "counterfactual_summary.json"),
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
            counterfactual_summary=best_counterfactual_summary,
            enable_bidirectional_counterfactual_validation=enable_bidirectional_counterfactual_validation,
            counterfactual_shadow_mode=counterfactual_shadow_mode,
            enable_negative_control=enable_negative_control,
            max_negative_control_attempts=max_negative_control_attempts,
            max_negative_control_ast_edits=max_negative_control_ast_edits,
            enable_runtime_target_reachability=enable_runtime_target_reachability,
            enable_contrastive_observation_oracle=enable_contrastive_observation_oracle,
            min_valid_surrogate_patches_for_consensus=min_valid_surrogate_patches_for_consensus,
            surrogate_consensus_threshold=surrogate_consensus_threshold,
            counterfactual_evidence_mode=counterfactual_evidence_mode,
            method_name="ATS-BRT" if enable_adaptive_typed_search else "P0",
            enable_adaptive_typed_search=enable_adaptive_typed_search,
            enable_structured_observation_extractor=enable_structured_observation_extractor,
            enable_minimal_oracle_search=enable_minimal_oracle_search,
            enable_trigger_search=enable_trigger_search,
            enable_duplicate_aware_archive=enable_duplicate_aware_archive,
            enable_optional_recomposition=enable_optional_recomposition,
            enable_selector_v2=enable_selector_v2,
            max_extra_unique_candidates=max_extra_unique_candidates,
            max_trigger_search_candidates=max_trigger_search_candidates,
            max_minimal_oracle_candidates=max_minimal_oracle_candidates,
            max_protocol_repair_candidates=max_protocol_repair_candidates,
            max_recomposition_candidates=max_recomposition_candidates,
            candidate_archive_summary=archive.summary() if archive is not None else {},
            adaptive_search_summary={
                "branch_counts": branch_counts,
                "trace_steps": len(adaptive_search_trace),
                "unique_budget_limit": unique_budget_limit,
            },
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
            "enable_bidirectional_counterfactual_validation": enable_bidirectional_counterfactual_validation,
            "counterfactual_shadow_mode": counterfactual_shadow_mode,
            "enable_negative_control": enable_negative_control,
            "enable_runtime_target_reachability": enable_runtime_target_reachability,
            "enable_contrastive_observation_oracle": enable_contrastive_observation_oracle,
            "counterfactual_evidence_mode": counterfactual_evidence_mode,
            "method_name": "ATS-BRT" if enable_adaptive_typed_search else "P0",
            "enable_adaptive_typed_search": enable_adaptive_typed_search,
            "enable_structured_observation_extractor": enable_structured_observation_extractor,
            "enable_minimal_oracle_search": enable_minimal_oracle_search,
            "enable_trigger_search": enable_trigger_search,
            "enable_duplicate_aware_archive": enable_duplicate_aware_archive,
            "enable_optional_recomposition": enable_optional_recomposition,
            "enable_selector_v2": enable_selector_v2,
            "max_extra_unique_candidates": max_extra_unique_candidates,
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
