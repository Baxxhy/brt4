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
    essential_trigger_factors: list[dict[str, Any]] = field(default_factory=list)
    trigger_ablation_rules: list[dict[str, Any]] = field(default_factory=list)
    trace_targets: list[dict[str, Any]] = field(default_factory=list)
    public_observation_schema: list[str] = field(default_factory=list)
    trigger_contract: dict[str, Any] = field(default_factory=dict)
    failure_contract: dict[str, Any] = field(default_factory=dict)
    expected_contract: dict[str, Any] = field(default_factory=dict)
    localization_contract: dict[str, Any] = field(default_factory=dict)
    uncertainties: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestSegments(JsonMixin):
    instance_id: str = ""
    scaffold_nodes: list[dict[str, Any]] = field(default_factory=list)
    trigger_nodes: list[dict[str, Any]] = field(default_factory=list)
    oracle_nodes: list[dict[str, Any]] = field(default_factory=list)
    scaffold_hash: str = ""
    trigger_hash: str = ""
    oracle_hash: str = ""
    target_call_locations: list[dict[str, Any]] = field(default_factory=list)
    observation_candidates: list[dict[str, Any]] = field(default_factory=list)
    segment_confidence: dict[str, float] = field(
        default_factory=lambda: {"scaffold": 0.0, "trigger": 0.0, "oracle": 0.0}
    )
    test_entry_count: int = 0
    parse_error: str = ""


@dataclass
class StructuredObservation(JsonMixin):
    instance_id: str = ""
    observation_id: str = ""
    exception_type: str | None = None
    warning_types: list[str] = field(default_factory=list)
    return_type: str = ""
    return_repr_short: str = ""
    length: int | None = None
    shape: list[int | str] | None = None
    dtype: str | None = None
    public_attrs: dict[str, Any] = field(default_factory=dict)
    serialization_tokens: list[str] = field(default_factory=list)
    render_tokens: list[str] = field(default_factory=list)
    sql_tokens: list[str] = field(default_factory=list)
    ordering: list[str] = field(default_factory=list)
    log_tokens: list[str] = field(default_factory=list)
    source: str = ""
    status: str = "UNKNOWN"
    target_expression: str = ""
    fallback_reason: str = ""


@dataclass
class CandidateArchiveEntry(JsonMixin):
    candidate_id: str = ""
    code_hash: str = ""
    normalized_ast_hash: str = ""
    behavior_signature: str = ""
    segment_hashes: dict[str, str] = field(default_factory=dict)
    origin: str = "UNKNOWN"
    parent_candidate_id: str = ""
    seed_id: str = ""
    round: int = 0
    search_action: str = ""
    code_path: str = ""
    buggy_execution: dict[str, Any] = field(default_factory=dict)
    verifier_decision: dict[str, Any] = field(default_factory=dict)
    target_evidence: dict[str, Any] = field(default_factory=dict)
    oracle_risk: dict[str, Any] = field(default_factory=dict)
    surrogate_result: dict[str, Any] = field(default_factory=dict)
    novelty: float = 1.0
    duplicate_status: str = "UNIQUE"
    duplicate_of: str = ""
    duplicate_redirected_from: str = ""
    observation_id: str = ""
    executed: bool = False


@dataclass
class AdaptiveSearchDecision(JsonMixin):
    action: str = "stop"
    search_action: str = ""
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    abstain: bool = False


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
class CounterfactualPlan(JsonMixin):
    instance_id: str = ""
    positive_trigger_factors: list[str] = field(default_factory=list)
    selected_ablation_factor: str = ""
    negative_control_goal: str = ""
    negative_control_operation: str = ""
    expected_buggy_effect: str = "UNKNOWN"
    frozen_regions: list[str] = field(
        default_factory=lambda: [
            "imports",
            "fixtures",
            "decorators",
            "class_context",
            "setup",
            "runner",
            "oracle",
        ]
    )
    preserve_target_api: bool = True
    max_ast_edits: int = 1
    abstain: bool = False
    abstain_reason: str = ""
    source_anchor: dict[str, Any] = field(default_factory=dict)
    positive_ast_pattern: str = ""
    negative_ast_pattern: str = ""


@dataclass
class NegativeControlMetadata(JsonMixin):
    instance_id: str = ""
    status: str = "ABSTAIN"
    selected_factor_id: str = ""
    changed_ast_nodes: list[str] = field(default_factory=list)
    frozen_region_changed: bool = False
    target_api_preserved: bool = True
    oracle_preserved: bool = True
    setup_preserved: bool = True
    test_entry_preserved: bool = True
    validation_reasons: list[str] = field(default_factory=list)
    ast_edit_count: int = 0
    semantic_edits: list[dict[str, Any]] = field(default_factory=list)
    semantic_edit_count: int = 0
    raw_changed_node_count: int = 0
    max_ast_edits: int = 1
    retry_count: int = 0
    cache_key: str = ""
    generation_method: str = ""
    imports_preserved: bool = True
    protocol_preserved: bool = True
    fixtures_preserved: bool = True
    decorators_preserved: bool = True
    trigger_only_changed: bool = True


@dataclass
class TargetReachability(JsonMixin):
    instance_id: str = ""
    target_hit: str = "unknown"
    hit_functions: list[str] = field(default_factory=list)
    hit_files: list[str] = field(default_factory=list)
    traceback_frames: list[dict[str, Any]] = field(default_factory=list)
    covered_target_lines: list[str] = field(default_factory=list)
    target_call_count: int = 0
    reachability_source: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence_complete: bool = False


@dataclass
class FailureSignature(JsonMixin):
    instance_id: str = ""
    outcome: str = "UNKNOWN"
    exception_type: str = ""
    exception_message_normalized: str = ""
    failure_location: str = ""
    top_project_frame: str = ""
    normalized_failure_signature: str = ""
    runtime_target_hit: str = "unknown"
    semantic_target_hit: str = "unknown"


@dataclass
class CounterfactualSurrogateRun(JsonMixin):
    patch_id: str = ""
    patch_valid: bool = False
    positive_result: dict[str, Any] = field(default_factory=dict)
    negative_result: dict[str, Any] = field(default_factory=dict)
    positive_executed: bool = False
    negative_executed: bool = False
    negative_skip_reason: str = ""
    paired_execution_complete: bool = False


@dataclass
class CounterfactualEvidence(JsonMixin):
    instance_id: str = ""
    positive_buggy: dict[str, Any] = field(default_factory=dict)
    negative_buggy: dict[str, Any] = field(default_factory=dict)
    surrogate_runs: list[dict[str, Any]] = field(default_factory=list)
    trigger_necessity: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "UNKNOWN",
            "score": 0.0,
            "reason": "",
        }
    )
    repair_sufficiency: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "UNKNOWN",
            "valid_patch_count": 0,
            "positive_pass_count": 0,
            "supported_patch_count": 0,
            "conflicting_patch_count": 0,
            "invalid_patch_count": 0,
            "paired_support_score": 0.0,
            "score": 0.0,
            "reason": "",
        }
    )
    oracle_stability: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "UNKNOWN",
            "score": 0.0,
            "reason": "",
        }
    )
    bidirectional_support: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "UNKNOWN",
            "reason": "",
        }
    )


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
    runtime_target_hit: str = "unknown"
    semantic_target_hit: str = "unknown"
    combined_target_hit: str = "unknown"
    target_hit_evidence: dict[str, Any] = field(default_factory=dict)


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
    lineage: dict[str, Any] = field(default_factory=dict)


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
    execution: dict[str, Any] = field(default_factory=dict)
    verifier: dict[str, Any] = field(default_factory=dict)
    surrogate: dict[str, Any] = field(default_factory=dict)
    legacy_score: int = 0
    legacy_rank: int = 0
    legacy_selected: bool = False
    evidence_rank: dict[str, Any] = field(default_factory=dict)
    evidence_rank_key: list[int] = field(default_factory=list)
    counterfactual_evidence_rank: dict[str, Any] = field(default_factory=dict)
    counterfactual_would_select: bool = False
    counterfactual_evidence: dict[str, Any] = field(default_factory=dict)
    counterfactual_summary: dict[str, Any] = field(default_factory=dict)
    selection_changed_by_counterfactual: bool = False
    ranking_changed_in_shadow: bool = False
    ranking_change_reason: str = ""
    lineage: dict[str, Any] = field(default_factory=dict)
    repair_aware_would_select: bool = False
    candidate_id: str = ""
    archive_entry: dict[str, Any] = field(default_factory=dict)
    selector_v2_rank: list[int] = field(default_factory=list)
    selector_v2_reason: str = ""
    selector_v2_selected: bool = False
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
    outcome: str = ""
    exception_type: str = ""
    exception_message_normalized: str = ""
    failure_location: str = ""
    top_project_frame: str = ""
    normalized_failure_signature: str = ""
    runtime_target_hit: str = "unknown"
    semantic_target_hit: str = "unknown"
    public_observations: dict[str, Any] = field(default_factory=dict)
    return_code: int = 0


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
    counterfactual_summary: dict[str, Any] = field(default_factory=dict)
    enable_bidirectional_counterfactual_validation: bool = False
    counterfactual_shadow_mode: bool = True
    enable_negative_control: bool = False
    max_negative_control_attempts: int = 1
    max_negative_control_ast_edits: int = 1
    enable_negative_control_llm_fallback: bool = True
    max_negative_control_llm_attempts: int = 1
    enable_runtime_target_reachability: bool = False
    enable_contrastive_observation_oracle: bool = False
    enable_counterfactual_repair_branch: bool = False
    max_counterfactual_trigger_repairs: int = 1
    max_counterfactual_oracle_repairs: int = 1
    counterfactual_repair_requires_valid_negative: bool = True
    min_valid_surrogate_patches_for_consensus: int = 2
    surrogate_consensus_threshold: float = 0.67
    counterfactual_evidence_mode: str = "soft"
    method_name: str = "P0"
    enable_adaptive_typed_search: bool = False
    enable_structured_observation_extractor: bool = False
    enable_minimal_oracle_search: bool = False
    enable_trigger_search: bool = False
    enable_duplicate_aware_archive: bool = False
    enable_optional_recomposition: bool = False
    enable_selector_v2: bool = False
    max_extra_unique_candidates: int = 3
    max_trigger_search_candidates: int = 2
    max_minimal_oracle_candidates: int = 2
    max_protocol_repair_candidates: int = 1
    max_recomposition_candidates: int = 1
    candidate_archive_summary: dict[str, Any] = field(default_factory=dict)
    adaptive_search_summary: dict[str, Any] = field(default_factory=dict)
