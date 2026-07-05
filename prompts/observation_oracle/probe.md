把当前单入口测试改为 probe。保留 setup 和 trigger，只移除或替换原断言，观测公开行为。必须打印 BRT_OBS_START、一个 JSON、BRT_OBS_END。
BehaviorTarget：{behavior_json}
ProtocolRecovery：{protocol_json}
当前测试：{candidate_code}
只输出完整 Python 文件。
