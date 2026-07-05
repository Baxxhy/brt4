当前测试触达相关行为但 oracle 可能错误或脆弱。只修复断言和观测证据，不改 setup。
必须返回完整 Python 测试文件，后续会保存为同级目录下的 test_brt_<instance_id>.py。
不要返回 method 片段，不要依赖插入到已有 class/file。
最终 BRT 的语义必须是：buggy 版本失败，修复后版本通过。
不要把 Issue 中的错误现象当成 expected behavior。若 Issue 说“当前会抛异常/报错/crash，但应该正常工作”，最终测试不能使用 pytest.raises/with self.assertRaises 来期待该异常。
不要添加与 Issue 无关的 baseline 断言，例如普通输出格式、完整字符串、默认列名、无关单位显示等。只保留能证明 expected_behavior 的最小稳定断言。
若 expected_behavior 要求能力、属性、文本或状态存在，必须使用正向断言；不得用
not hasattr/not in 把 buggy 的缺失状态当作正确结果。不得使用恒真式、skip、宽泛
Exception 或因为路径中偶然出现单个字符而成立/失败的脆弱断言。

行为目标：{behavior_json}
当前测试：{candidate_code}
执行日志：{execution_log}
观测结果：{observation_json}
Verifier 反馈：{verifier_feedback}

必须优先完成 Verifier 反馈中的 oracle 修复目标；返回代码必须对断言产生实质修改，
不能原样返回当前测试。
