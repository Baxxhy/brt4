根据 Issue expected_behavior、公开行为观测和可用的正负对照/代理修复对比证据，重写当前测试的 oracle。不要把 buggy observation 直接当 expected value；不要改 setup 和 trigger。SQL 只检查关键片段，warning/log 使用专用断言；不应崩溃时增加最小公开不变量。
BehaviorTarget：{behavior_json}
ProtocolRecovery：{protocol_json}
允许的 oracle_type：NO_EXCEPTION|EXCEPTION_TYPE|EQUAL|NOT_EQUAL|CONTAINS|NOT_CONTAINS|TYPE_IS|SHAPE_IS|STATE_EQUALS|ORDER_BEFORE|WARNING_TYPE|LOG_CONTAINS|SERIALIZATION_PROPERTY|WARNING|LOGGING|EXACT_VALUE|TYPE_OR_SHAPE|STATE_CHANGE|SQL_VALIDITY|SERIALIZATION|RENDER_OUTPUT|ORDERING
观测：{observation_json}
对比观察：{contrastive_observation_json}
当前测试：{candidate_code}
执行日志：{execution_log}
优先选择同时满足这些条件的观察项：正例 buggy 与负例 buggy 可区分；正例 buggy 与多数 surrogate 可区分；多个 surrogate 之间稳定；与 expected_behavior 对齐；属于公开行为；不包含随机值、临时路径、完整 repr、完整 SQL 或完整错误文本；能用 1～2 个最小断言表达。
第一行用注释写 # BRT_ORACLE_TYPE: <type>，随后只输出完整 Python 文件。只修改 oracle，不改 setup、trigger、target API 或测试入口结构。
