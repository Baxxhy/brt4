"""Lightweight dynamic trace and fitness scoring for NS-GEM."""

from __future__ import annotations

from typing import Any

from ..core.schema import BehaviorTarget, ExecutionResult, IssueGate


def _contains_any(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return any(needle and needle.lower() in low for needle in needles)


def compute_trace_fitness(
    candidate_code: str,
    execution: ExecutionResult,
    behavior: BehaviorTarget,
    issue_gate: IssueGate,
    verifier: dict[str, Any] | None = None,
    oracle_risk: dict[str, Any] | None = None,
    surrogate_status: str = "",
) -> tuple[dict[str, Any], int, list[str], list[str]]:
    verifier = verifier or {}
    oracle_risk = oracle_risk or {}
    log_text = f"{execution.stdout}\n{execution.stderr}"
    target_apis = issue_gate.target_apis
    observable = issue_gate.observable_channels
    setup_like = execution.status in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT"}
    buggy_pass = execution.returncode == 0 or execution.status in {"PASS", "BUGGY_PASS"}
    issue_aligned = verifier.get("decision") == "accept" or verifier.get("failure_class") == "issue_aligned"
    profile = {
        "target_api_mentioned_in_test": _contains_any(candidate_code, target_apis),
        "target_api_mentioned_in_log": _contains_any(log_text, target_apis),
        "failure_matches_issue": bool(issue_aligned),
        "observable_channel_used": _contains_any(candidate_code + "\n" + log_text, observable),
        "setup_like_failure": setup_like,
        "unrelated_failure": execution.status == "UNRELATED_FAIL" or (execution.returncode != 0 and not issue_aligned and not setup_like),
        "buggy_pass": buggy_pass,
    }
    score = 0
    reasons: list[str] = []
    def add(cond: bool, value: int, reason: str) -> None:
        nonlocal score
        if cond:
            score += value
            reasons.append(reason)
    add(profile["target_api_mentioned_in_test"], 2, "+2 target API mentioned in test")
    add(profile["target_api_mentioned_in_log"], 2, "+2 target API mentioned in log")
    add(profile["observable_channel_used"], 3, "+3 observable channel used")
    add(issue_aligned, 5, "+5 issue-aligned failure")
    add(surrogate_status in {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"}, 4, "+4 surrogate success")
    add(profile["setup_like_failure"], -4, "-4 setup-like failure")
    add(profile["unrelated_failure"], -3, "-3 unrelated failure")
    add(profile["buggy_pass"] and not profile["target_api_mentioned_in_test"], -2, "-2 buggy pass without target")
    add(oracle_risk.get("level") == "high", -2, "-2 high oracle risk")
    next_ops: list[str] = []
    if profile["buggy_pass"]:
        next_ops.extend(["Mut_CallChain", "Mut_StateLifecycle"])
    if profile["setup_like_failure"]:
        next_ops.append("Mut_InjectArg")
    if not profile["observable_channel_used"]:
        next_ops.extend(["Mut_Observe", "Mut_OracleCompile"])
    return profile, score, reasons, list(dict.fromkeys(next_ops))
