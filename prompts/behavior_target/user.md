根据 Issue 结构化目标、相似测试和完整测试文件片段，判断新 BRT 应复用哪些 imports、fixture、class、setup、decorator 和断言风格。只输出 JSON。

实例：{instance_id}
行为目标：{behavior_json}
相似测试：{seed_test}
完整文件片段：{full_file_excerpt}

输出字段：host_file, host_class, seed_test_name, imports, setup_context, fixtures, decorators, pytestmark, insert_strategy, insert_location_hint, risks。
