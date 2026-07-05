根据 Issue expected_behavior 和公开行为观测重写当前测试的 oracle。不要把 buggy observation 直接当 expected value；不要改 setup 和 trigger。SQL 只检查关键片段，warning/log 使用专用断言；不应崩溃时增加最小公开不变量。
BehaviorTarget：{behavior_json}
ProtocolRecovery：{protocol_json}
允许的 oracle_type：NO_EXCEPTION|EXCEPTION_TYPE|WARNING|LOGGING|EXACT_VALUE|TYPE_OR_SHAPE|STATE_CHANGE|SQL_VALIDITY|SERIALIZATION|RENDER_OUTPUT|ORDERING
观测：{observation_json}
当前测试：{candidate_code}
执行日志：{execution_log}
第一行用注释写 # BRT_ORACLE_TYPE: <type>，随后只输出完整 Python 文件。
