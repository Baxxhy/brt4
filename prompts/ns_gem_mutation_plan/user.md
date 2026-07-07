IssueGate:
{issue_gate_json}

TestSkeleton:
{test_skeleton_json}

OperatorApplicability:
{operator_applicability_json}

Previous execution feedback:
{execution_feedback}

Current candidate code excerpt:
{current_candidate_code}

Return exactly:
{{
  "selected_operator": "Mut_Boundary|Mut_NegatePredicateObject|Mut_Wrap|Mut_InjectArg|Mut_CallChain|Mut_StateLifecycle|Mut_Observe|Mut_OracleCompile",
  "target_node_id": "",
  "operator_parameters": {{
    "replacement_expr": "",
    "new_argument": "",
    "keyword": "",
    "value": "",
    "observable": ""
  }},
  "preserve_nodes": [],
  "expected_effect": "",
  "risk": "low|medium|high",
  "reason": ""
}}
