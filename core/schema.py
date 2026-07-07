"""Dataclasses used by the BRT3 pipeline."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from .utils import safe_json_dump


class JsonMixin:
    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def save_json(self, path: str) -> None:
        safe_json_dump(self.to_dict(), path)


@dataclass
class RetrievedCode(JsonMixin):
    instance_id: str
    obj_name: str = ""
    node_type: str = ""
    path: str = ""
    code_start_line: str | int = ""
    code_end_line: str | int = ""
    code_content: str = ""
    parent: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievedTest(JsonMixin):
    instance_id: str
    name: str = ""
    file: str = ""
    code_content: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstanceContext(JsonMixin):
    instance_id: str
    issue_text: str
    repo: str = ""
    base_commit: str = ""
    buggy_repo_path: str = ""
    retrieved_code: list[RetrievedCode] = field(default_factory=list)
    retrieved_tests: list[RetrievedTest] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BehaviorTarget(JsonMixin):
    instance_id: str
    issue_summary: str = ""
    trigger_condition: dict[str, Any] = field(default_factory=dict)
    error_symptom: dict[str, Any] = field(default_factory=dict)
    expected_behavior: dict[str, Any] = field(default_factory=dict)
    target_apis: list[dict[str, Any]] = field(default_factory=list)
    suspected_bug_locations: list[dict[str, Any]] = field(default_factory=list)
    related_test_seeds: list[dict[str, Any]] = field(default_factory=list)
    mutation_hints: list[dict[str, Any]] = field(default_factory=list)
    observation_points: list[dict[str, Any]] = field(default_factory=list)
    assertion_hints: list[dict[str, Any]] = field(default_factory=list)
    setup_hints: list[dict[str, Any]] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HostContext(JsonMixin):
    instance_id: str
    host_file: str = ""
    host_class: str = ""
    seed_test_name: str = ""
    seed_test_code: str = ""
    imports: str = ""
    setup_context: str = ""
    model_context: str = ""
    fixtures: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    pytestmark: str = ""
    test_command: str = ""
    seed_execution_status: str = "ERROR"
    seed_execution: dict[str, Any] = field(default_factory=dict)
    insert_strategy: str = "same_dir_new_file"
    insert_location_hint: str = ""
    adjacent_tests: list[str] = field(default_factory=list)
    full_test_file_path: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProtocolRecovery(JsonMixin):
    instance_id: str
    test_file: str = ""
    test_framework: str = "unknown"
    test_command: str = ""
    imports: list[str] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)
    pytest_marks: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    class_context: str = ""
    setup_methods: list[str] = field(default_factory=list)
    teardown_methods: list[str] = field(default_factory=list)
    local_helpers: list[dict[str, str]] = field(default_factory=list)
    local_models: list[dict[str, str]] = field(default_factory=list)
    conftest_context: list[dict[str, Any]] = field(default_factory=list)
    runner_hints: list[str] = field(default_factory=list)
    protocol_risks: list[str] = field(default_factory=list)
    selected_seed_name: str = ""
    placement_dir: str = ""


@dataclass
class MutationPlan(JsonMixin):
    instance_id: str
    round_id: int = 0
    mutation_goal: str = ""
    preserve_from_seed: list[str] = field(default_factory=list)
    target_api: list[str] = field(default_factory=list)
    target_path: list[str] = field(default_factory=list)
    mutation_ops: list[str] = field(default_factory=list)
    expected_behavior: str = ""
    oracle_strategy: str = ""
    why_this_should_trigger: str = ""
    risk: str = "medium"


@dataclass
class IssueGate(JsonMixin):
    instance_id: str
    target_apis: list[str] = field(default_factory=list)
    trigger_condition: str = ""
    state_variables: list[str] = field(default_factory=list)
    observable_channels: list[str] = field(default_factory=list)
    expected_failure_signature: list[str] = field(default_factory=list)
    forbidden_failures: list[str] = field(default_factory=list)
    source_evidence: dict[str, list[str]] = field(default_factory=dict)
    uncertainties: list[str] = field(default_factory=list)


@dataclass
class TestSkeleton(JsonMixin):
    instance_id: str
    setup_nodes: list[dict[str, Any]] = field(default_factory=list)
    action_nodes: list[dict[str, Any]] = field(default_factory=list)
    oracle_nodes: list[dict[str, Any]] = field(default_factory=list)
    target_call_nodes: list[dict[str, Any]] = field(default_factory=list)
    def_use_chain: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    mutable_slice: list[dict[str, Any]] = field(default_factory=list)
    protected_nodes: list[dict[str, Any]] = field(default_factory=list)
    parse_ok: bool = False
    reason: str = ""


@dataclass
class MutationOperator(JsonMixin):
    name: str
    cn_name: str = ""
    description: str = ""
    applicable: bool = False
    target_node: str = ""
    reason: str = ""
    risk: str = "medium"


@dataclass
class NSGEMOperatorPlan(JsonMixin):
    instance_id: str
    selected_operator: str = ""
    target_node_id: str = ""
    operator_parameters: dict[str, Any] = field(default_factory=dict)
    preserve_nodes: list[str] = field(default_factory=list)
    expected_effect: str = ""
    risk: str = "medium"
    reason: str = ""
    valid: bool = False
    fallback_reason: str = ""


@dataclass
class StrictVerifierResult(JsonMixin):
    instance_id: str
    decision: str = "reject"
    failure_class: str = "side_path"
    target_hit: bool = False
    oracle_grounded_in_issue: bool = False
    uses_public_behavior: bool = False
    reason: str = ""
    next_action: str = "reject"


@dataclass
class CandidateTest(JsonMixin):
    instance_id: str
    round_id: int = 0
    code: str = ""
    candidate_file_path: str = ""
    candidate_repo_path: str = ""
    pytest_nodeid: str = ""
    command: str = ""
    prompt_path: str = ""
    response_path: str = ""
    status: str = "CREATED"
    notes: str = ""


@dataclass
class CandidateCheckpoint(JsonMixin):
    instance_id: str
    round_id: int
    code_path: str = ""
    score: int = 0
    reason: str = ""
    oracle_risk: dict[str, Any] = field(default_factory=dict)
    surrogate_risk: dict[str, Any] = field(default_factory=dict)
    selector_score_before_risk: int = 0
    selector_score_after_risk: int = 0
    selector_penalty_reasons: list[str] = field(default_factory=list)
    issue_gate_hit_score: int = 0
    operator_used: str = ""
    ast_applied: bool = False
    fitness_score: int = 0
    minimality_score: int = 0
    score_after_ns_gem: int = 0
    ns_gem_mode: str = ""
    ns_gem_used_for_initial_plan: bool = False
    p0_primary_score: int = 0
    ns_gem_secondary_score: int = 0
    scoring_policy: str = ""
    status_tier: int = 0
    final_rank_key: list[int] = field(default_factory=list)
    ns_gem_changed_tier: bool = False
    ns_gem_selection_trace: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    verifier: dict[str, Any] = field(default_factory=dict)
    surrogate: dict[str, Any] = field(default_factory=dict)
    selected: bool = False


@dataclass
class ExecutionResult(JsonMixin):
    instance_id: str = ""
    command: str = ""
    cwd: str = ""
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timeout: bool = False
    status: str = "PASS"
    error_reason: str = ""


@dataclass
class ObservationReport(JsonMixin):
    instance_id: str
    probe_code: str = ""
    probe_file_path: str = ""
    execution: dict[str, Any] = field(default_factory=dict)
    observations: dict[str, Any] = field(default_factory=dict)
    raw_output: str = ""
    status: str = "UNKNOWN"


@dataclass
class VerifierDecision(JsonMixin):
    instance_id: str
    decision: str = "reject"
    reason: str = ""
    focus: list[str] = field(default_factory=list)
    next_action: str = ""


@dataclass
class DualVersionResult(JsonMixin):
    instance_id: str
    mode: str = "buggy_only"
    buggy_execution: dict[str, Any] = field(default_factory=dict)
    patched_execution: dict[str, Any] = field(default_factory=dict)
    status: str = "NOT_RUN"
    notes: str = ""
    surrogate_patch: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SurrogatePatchCandidate(JsonMixin):
    instance_id: str
    round_id: int = 0
    patches: list[dict[str, Any]] = field(default_factory=list)
    applied_paths: list[str] = field(default_factory=list)
    diff: str = ""
    status: str = "CREATED"
    reason: str = ""
    prompt_path: str = ""
    response_path: str = ""


@dataclass
class FinalResult(JsonMixin):
    instance_id: str
    status: str = "BEST_EFFORT"
    final_test_path: str = ""
    rounds_used: int = 0
    buggy_execution: dict[str, Any] = field(default_factory=dict)
    dual_version_result: dict[str, Any] = field(default_factory=dict)
    behavior_target: dict[str, Any] = field(default_factory=dict)
    host_context: dict[str, Any] = field(default_factory=dict)
    observation_report: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    protocol_recovery_enabled: bool = False
    seed_mutation_enabled: bool = False
    observation_oracle_enabled: bool = False
    strict_verifier_enabled: bool = False
    selected_seed_file: str = ""
    selected_seed_name: str = ""
    seed_fallback_used: bool = False
    mutation_ops: list[str] = field(default_factory=list)
    oracle_type: str = ""
    strict_verifier_decision: str = ""
    strict_failure_class: str = ""
    oracle_rebound: bool = False
    final_reason: str = ""
    seed_mode: str = ""
    selected_seed_index: int = -1
    seed_attempts_count: int = 0
    seed_attempts_summary: list[dict[str, Any]] = field(default_factory=list)
    seed_switch_reasons: list[str] = field(default_factory=list)
    selected_seed_reason: str = ""
    final_oracle_risk: dict[str, Any] = field(default_factory=dict)
    final_surrogate_risk: dict[str, Any] = field(default_factory=dict)
    candidate_repo_path: str = ""
    pytest_nodeid: str = ""
    command: str = ""
    direct_test_repo_path_hint: str = ""
    placement_dir: str = ""
    runner_kind: str = ""
    selector: str = ""
    method_version: str = ""
    issue_gate: dict[str, Any] = field(default_factory=dict)
    test_skeleton_summary: dict[str, Any] = field(default_factory=dict)
    operator_applicability_summary: dict[str, Any] = field(default_factory=dict)
    ns_gem_operator_plan: dict[str, Any] = field(default_factory=dict)
    ns_gem_ast_applied: bool = False
    ns_gem_ast_fallback_reason: str = ""
    runtime_trace_profile: dict[str, Any] = field(default_factory=dict)
    fitness_score: int = 0
    fitness_reasons: list[str] = field(default_factory=list)
    probe_first_oracle_used: bool = False
    oracle_from_observable_channel: bool = False
    delta_minimization: dict[str, Any] = field(default_factory=dict)
    score_after_ns_gem: int = 0
    ns_gem_selection_reason: str = ""
    ns_gem_mode: str = ""
    ns_gem_used_for_initial_plan: bool = False
    p0_plan_preserved: bool = True
    ns_gem_operator_hint: str = ""
    ast_transform_default: str = ""
    ast_transform_rescue_used: bool = False
    ast_transform_allowed: bool = False
    ast_transform_reject_reason: str = ""
    ast_transform_rollback: bool = False
    scoring_policy: str = ""
    p0_primary_score: int = 0
    ns_gem_secondary_score: int = 0
    status_tier: int = 0
    final_rank_key: list[int] = field(default_factory=list)
    ns_gem_changed_tier: bool = False
