判断当前测试在 buggy 上的失败是否真能表达 Issue expected_behavior。
Issue：{issue_text}
BehaviorTarget：{behavior_json}
ProtocolRecovery：{protocol_json}
测试代码：{candidate_code}
命令：{command}
执行分类：{execution_status}
stdout/stderr：{execution_log}
相关源码：{source_context}

accept 必须同时满足：buggy 失败；非环境/语法/收集/超时；目标 API/函数/生命周期确实被执行；失败与 symptom 对齐；oracle 来自 Issue；检查公开行为。
输出：
{{
  "decision": "accept|repair_setup|repair_trigger|repair_oracle|reject",
  "failure_class": "setup|syntax|collect|timeout|buggy_pass|target_not_hit|side_path|oracle_wrong|oracle_too_strong|issue_aligned",
  "target_hit": false,
  "oracle_grounded_in_issue": false,
  "uses_public_behavior": false,
  "reason": "",
  "next_action": "repair_setup|repair_trigger|repair_oracle|reject"
}}
