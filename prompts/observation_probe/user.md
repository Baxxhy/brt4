请把下面候选测试改成 probe 测试，插入运行时观测点。必须打印固定标记：
print("BRT_OBS_START")
print(json.dumps(observations, default=str, ensure_ascii=False))
print("BRT_OBS_END")

所有目标调用完成后只打印一次标记；不要在观测尚未完成时提前打印。
probe 可以捕获并记录异常，但不能吞掉 setup/import/collection 错误。

行为目标：{behavior_json}
候选测试：{candidate_code}
