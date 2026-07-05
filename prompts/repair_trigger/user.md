当前测试没有触发 Issue 相关路径或失败无关。保留 setup，只调整输入、参数、状态、mock、配置、调用链或边界条件。
必须返回完整 Python 测试文件，后续会保存为同级目录下的 test_brt_<instance_id>.py。
不要返回 method 片段，不要依赖插入到已有 class/file。

行为目标：{behavior_json}
HostContext：{host_context_json}
相似测试种子：{seed_test_code}
相关源码：{code_context}
当前测试：{candidate_code}
执行日志：{execution_log}
Verifier 反馈：{verifier_feedback}

修复前先核对：
1. 测试是否实际调用 target_apis 中的 API；
2. 是否使用 Issue 明确给出的输入、边界值、配置、operator 或调用顺序；
3. 是否保留相似测试中已经能运行的对象构造和调用链；
4. 若测试 PASS，必须修改触发输入或状态，不能仅增加与 Issue 无关的断言；
5. 最终只保留一个与 expected_behavior 直接对应的稳定 oracle。
6. Verifier 反馈是上一轮语义核验结果；必须逐项执行 reason 和 next_action。
   若反馈指出某个输入、参数、字符串、operator 或调用顺序不精确，必须删除旧模式，
   并改成 Issue/BehaviorTarget 指定的目标模式，不能原样返回当前测试。
7. Verifier 的 next_action 是诊断建议，不是高于源码的事实。若建议与相关源码中明确存在的
   生命周期 API、检查机制或调用方式冲突，以源码和已通过的相似测试为准。
8. 若 expected_behavior 是“新增检查/尽早检查/报告配置错误”，且相关源码暴露 check()
   或类似检查入口，应直接调用该入口并检查其稳定返回证据；不要无依据地假设构造函数
   或 clean()/save() 会抛异常。
   环境修复只能补齐模型绑定、字段 name、app_label 等运行元数据，不能删除 check()
   或把它替换成普通值校验。standalone Field 可用 set_attributes_from_name()；更优先
   复用 HostContext 中真实模型并通过 Model._meta.get_field() 取得字段。
9. 对缺失行为，修复目标是增加与 expected_behavior 对齐的正向证据断言，让 buggy 因
   证据不存在而 assertion fail；不要继续寻找一个 buggy 已经实现的异常。
10. 对缺失日志，使用 assertLogs/caplog 观测修复后应出现的日志；不得 patch buggy
    源码中不存在的 logger 属性。
11. 禁止 skip/提前 return、恒真断言、吞异常，以及 mock/patch target API 来绕过真实路径。
12. Issue 给出的 MWE 字面输入、参数、operator 和调用顺序是路径约束；修复时不得将其
    简化为只覆盖普通路径的替代案例。
