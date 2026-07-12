根据 CounterfactualPlan 对当前正例测试生成负向对照。

BehaviorTarget：{behavior_json}
ProtocolRecovery：{protocol_json}
CounterfactualPlan：{counterfactual_plan_json}
当前正例测试：{candidate_code}

只输出完整 Python 文件，不要 Markdown。第一目标是最小 trigger ablation；如果无法安全完成，输出原测试是不允许的。
