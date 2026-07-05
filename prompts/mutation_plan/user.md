根据 Issue 目标、单一 seed 和测试协议生成一个小变异计划。最多选择 3 个 mutation_ops；不得重写无关 setup，不得编造 expected value。

BehaviorTarget：{behavior_json}
HostContext：{host_context_json}
ProtocolRecovery：{protocol_json}
上一轮执行反馈：{execution_feedback}
Verifier 反馈：{verifier_feedback}

如果 buggy PASS 或 target_not_hit，优先 CALL_CHAIN_EXTEND、LIFECYCLE_TRIGGER、CONFIG_MUTATION。
如果已进入目标 API 但仍 PASS，优先 ARG_BOUNDARY_EXPAND、ARG_VALUE_REPLACE、OPERATOR_FLIP、STATE_MUTATION、FIXTURE_DATA_MUTATION。
如果 oracle 可疑，不要继续扩大 trigger。

只输出：
{{
  "mutation_goal": "",
  "preserve_from_seed": [],
  "target_api": [],
  "target_path": [],
  "mutation_ops": [],
  "expected_behavior": "只复述 Issue 明确行为",
  "oracle_strategy": "公开行为",
  "why_this_should_trigger": "",
  "risk": "low|medium|high"
}}
