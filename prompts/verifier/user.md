根据执行结果判断下一步。输出字段 decision, reason, focus, next_action。
decision 只能是 accept, repair_setup, repair_trigger, repair_oracle, reject。

accept 必须同时满足：
1. 测试确实执行了 Issue 描述的 target API、输入和调用路径；
2. 失败现象与 error_symptom 相同，而不是仅仅日志中出现了相似关键词；
3. 断言表达 expected_behavior，且在 buggy 版本上自然失败；
4. 失败不是错误格式名、错误 nodeid、缺依赖、无效 fixture、测试自身异常或额外无关断言造成；
5. 如果 Issue 表示“当前抛异常但修复后不应抛”，测试不能用 pytest.raises/assertRaises 把该 buggy 异常当作期望行为。
6. 测试只有一个入口，没有 skip、恒真断言、吞异常或 mock 掉 target API；
7. reason 不得同时出现“未触发、测试设置问题、环境问题、断言方向错误、与 Issue 无关”等否定结论；出现任一项都禁止 accept。

选择规则：
- 路径、输入、参数、调用方式错误，或测试 PASS：repair_trigger。
- 测试 PASS 时，reason 必须具体指出候选测试与 trigger_condition/mutation_hints
  之间缺少的输入、对象状态、signal/mock/config、调用顺序或内部路径条件；
  next_action 必须给出下一轮应删除或替换的具体测试模式，不能只写“加强触发”。
- 对“缺少检查/缺少 warning/缺少状态更新/静默接受”类 Issue，buggy 没有异常通常正是
  error_symptom。不得建议在 __init__/clean/save 之间盲目寻找异常；必须从相关源码识别
  check()/validate()/warning/state API，并建议断言 expected_behavior 的证据存在。
- 只有 expected_behavior 明确要求抛异常时，才建议 assertRaises/pytest.raises。
- 对“缺少日志”类 Issue，必须建议 assertLogs/caplog 捕获日志证据；不得建议 patch
  buggy 源码中不存在的 logger 或 _logger 属性。
- 已触达目标 API，但断言方向反了、过强或检查了无关输出：repair_oracle。
- import/fixture/collection/syntax/runner 问题：repair_setup。
- 只有失败路径和 oracle 都与 Issue 对齐时才 accept。

Issue：{issue_text}
行为目标：{behavior_json}
已验证测试上下文：{host_context_json}
相关 buggy 源码：{source_context}
测试代码：{candidate_code}
执行结果：{execution_json}
