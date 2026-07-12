判断当前测试在 buggy 上的失败是否真能表达 Issue expected_behavior。
Issue：{issue_text}
BehaviorTarget：{behavior_json}
ProtocolRecovery：{protocol_json}
测试代码：{candidate_code}
命令：{command}
	执行分类：{execution_status}
	stdout/stderr：{execution_log}
	相关源码：{source_context}
	Runtime target hit：{runtime_target_hit}
	Runtime target evidence：{runtime_target_evidence_json}
	Counterfactual evidence：{counterfactual_evidence_json}

	accept 必须同时满足：buggy 失败；非环境/语法/收集/超时；目标 API/函数/生命周期确实被执行；失败与 symptom 对齐；oracle 来自 Issue；检查公开行为。
	区分 runtime_target_hit 和 semantic target hit：runtime_target_hit=true 时优先相信执行证据；runtime_target_hit=false 且 traceback/trace 完整时应判 target_not_hit；runtime_target_hit=unknown 时可以使用语义证据，但只能作为弱证据。不能仅凭关键词 accept。
	输出：
	{{
	  "decision": "accept|repair_setup|repair_trigger|repair_oracle|reject",
	  "failure_class": "setup|syntax|collect|timeout|buggy_pass|target_not_hit|side_path|oracle_wrong|oracle_too_strong|issue_aligned",
	  "target_hit": false,
	  "semantic_target_hit": false,
	  "oracle_grounded_in_issue": false,
	  "uses_public_behavior": false,
	  "reason": "",
  "next_action": "repair_setup|repair_trigger|repair_oracle|reject"
}}
