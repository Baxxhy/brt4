请根据观测结果和 Issue 语义，把 probe/candidate 改成干净的最终 BRT。不能保留 BRT_OBS 打印。

若 bug 是缺失行为，断言修复后应存在的 warning、check result、返回片段或状态变化；
不要因为 buggy 当前不抛异常就继续寻找 assertRaises 路径。
缺失日志使用 assertLogs/caplog，不要 mock 一个当前不存在的 logger。
只有 expected_behavior 明确要求异常时才生成 raises 断言。
最终文件必须只有一个测试入口和一个直接证明 expected_behavior 的主 oracle。
不得使用 skip、恒真断言、宽泛 Exception、完整大段 repr/SQL/帮助输出相等或无关 baseline。
buggy 观测只用于确认路径和当前值，不能把当前 buggy 值直接写成期望值。
若 expected_behavior 表示“存在/支持/包含/显示/保留”，必须写正向断言，不能断言缺失。

行为目标：{behavior_json}
候选测试：{candidate_code}
观测结果：{observation_json}
执行日志：{execution_log}
