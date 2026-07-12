根据 BehaviorTarget、ProtocolRecovery、当前正例测试和执行日志，选择一个最小 trigger ablation。

BehaviorTarget：{behavior_json}
ProtocolRecovery：{protocol_json}
当前正例测试：{candidate_code}
执行日志：{execution_log}

只输出 JSON：
{{
  "positive_trigger_factors": ["T1"],
  "selected_ablation_factor": "T1",
  "negative_control_goal": "",
  "negative_control_operation": "ARG_VALUE_REPLACE|OPERATOR_FLIP|STATE_RESET|CONFIG_RESET|CALL_REMOVAL|BOUNDARY_NORMALIZE|OTHER",
  "expected_buggy_effect": "PASS|DIFFERENT_FAILURE|NO_ISSUE_FAILURE|UNKNOWN",
  "frozen_regions": ["imports", "fixtures", "decorators", "class_context", "setup", "runner", "oracle"],
  "preserve_target_api": true,
  "max_ast_edits": 1,
  "abstain": false,
  "abstain_reason": ""
}}

如果不能安全只消融一个 trigger factor，必须返回 abstain=true，并说明原因。不要输出 Markdown。
