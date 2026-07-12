根据 Issue 结构化目标、相似测试和完整测试文件片段，判断新 BRT 应复用哪些 imports、fixture、class、setup、decorator 和断言风格。若 BehaviorTarget 中包含 essential_trigger_factors、trigger_ablation_rules 或 trace_targets，请保留它们对 trigger、target API 和公开观测的约束，不要改写成无依据的新目标。只输出 JSON。

实例：{instance_id}
行为目标：{behavior_json}
相似测试：{seed_test}
完整文件片段：{full_file_excerpt}

输出字段：host_file, host_class, seed_test_name, imports, setup_context, fixtures, decorators, pytestmark, insert_strategy, insert_location_hint, risks。
