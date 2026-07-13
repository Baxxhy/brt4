请分析下面这个缺陷复现任务。

你的目标不是润色 Issue，而是将原始 Issue 与 iCoRe 检索出的相关源码、相关测试对齐，提取后续生成 Bug Reproduction Test 所需的信息。

====================
【原始 Issue】
{issue_text}

====================
【iCoRe 检索出的相关源代码】
{code_context}

====================
【iCoRe 检索出的相关测试代码】
{test_context}

====================

请只输出一个合法 JSON 对象，字段必须严格如下：

{{
  "issue_summary": "用一句话概括这个 Issue 报告的问题。",
  "trigger_condition": {{
    "text": "可能触发 bug 的输入、状态、配置、API 调用或操作步骤。",
    "evidence": ["来自 Issue、源码或测试的依据。"],
    "confidence": "high|medium|low"
  }},
  "error_symptom": {{
    "text": "Issue 中观察到的错误现象，例如异常、warning 缺失、返回值错误、类型错误、SQL 错误、顺序错误、状态未更新等。",
    "symptom_type": "exception|warning|wrong_return|wrong_type|wrong_sql|wrong_order|state_not_updated|serialization_error|performance|unknown",
    "evidence": ["来自 Issue、源码或测试的依据。"],
    "confidence": "high|medium|low"
  }},
  "expected_behavior": {{
    "text": "修复后应该满足的正确行为。",
    "evidence": ["来自 Issue、源码或测试的依据。"],
    "confidence": "high|medium|low"
  }},
  "target_apis": [
    {{
      "name": "后续测试中可能需要调用的函数、方法、类或模块名。",
      "kind": "function|method|class|module|unknown",
      "source_path": "如果知道源码路径就填写，否则为空字符串。",
      "reason": "为什么这个 API 和 Issue 相关。"
    }}
  ],
  "suspected_bug_locations": [
    {{
      "path": "相关源码文件路径。",
      "object": "相关函数、类或方法名。",
      "lines": "如果知道行号范围就填写，例如 10-30，否则为空字符串。",
      "reason": "为什么这里可能和缺陷有关。"
    }}
  ],
  "related_test_seeds": [
    {{
      "test_name": "相关测试名称。",
      "test_file": "相关测试所在文件路径。",
      "why_relevant": "这个测试为什么可以作为相似测试起点。",
      "reusable_parts": ["可以复用的部分，例如 imports、fixture、class、setup、对象构造、API 调用、assert 风格。"],
      "possible_gap": "这个相似测试为什么可能还不能直接复现当前 Issue。"
    }}
  ],
  "mutation_hints": [
    {{
      "slot": "input|argument|object_state|mock|config|call_chain|operator|boundary_value|unknown",
      "current_pattern": "相似测试中已有的正常模式。",
      "target_pattern": "根据 Issue 需要变异成的触发模式。",
      "reason": "为什么这个变异可能让测试走向缺陷路径。",
      "confidence": "high|medium|low"
    }}
  ],
  "observation_points": [
    {{
      "kind": "exception|warning|return_value|type|repr|str|sql|order|state|cache|config|file_output|serialization|unknown",
      "expression_hint": "后续插桩时建议观察的表达式，例如 str(query)、type(result)、warnings、before/after state。",
      "reason": "为什么这个观测点能帮助判断 Issue 是否被触发。"
    }}
  ],
  "assertion_hints": [
    {{
      "assertion_goal": "最终 assert 应该验证的语义目标。",
      "preferred_assertion_style": "contains_fragment|not_contains_fragment|equals|isinstance|raises|warns|before_after_relation|order_equals|unknown",
      "avoid": "需要避免的脆弱或无关断言，例如完整 SQL 字符串相等、完整 repr 相等、无关数量断言。",
      "reason": "为什么这种断言更稳定且与 Issue 对齐。"
    }}
  ],
	  "setup_hints": [
	    {{
	      "hint": "运行测试可能需要的上下文，例如 fixture、TestCase、settings、database、tmp_path、monkeypatch、mock。",
	      "source": "issue|retrieved_test|retrieved_source|inference",
	      "confidence": "high|medium|low"
	    }}
	  ],
	  "essential_trigger_factors": [
	    {{
	      "factor_id": "T1",
	      "description": "一个可最小消融的 issue-specific trigger factor。",
	      "factor_type": "argument|operator|state|configuration|call_sequence|lifecycle|boundary|input_shape|other",
	      "positive_form": "当前触发形式，必须来自 Issue、源码或相似测试依据。",
	      "negative_control_form": "删除或反转该条件后的形式；没有可靠依据则留空。",
	      "evidence": "来自 Issue、retrieved code 或 retrieved test 的依据。",
	      "target_api_preserved": true,
	      "necessity_confidence": 0.0
	    }}
	  ],
	  "trigger_ablation_rules": [
	    {{
	      "factor_id": "T1",
	      "operation": "ARG_VALUE_REPLACE|OPERATOR_FLIP|STATE_RESET|CONFIG_RESET|CALL_REMOVAL|BOUNDARY_NORMALIZE|OTHER",
	      "positive_form": "与正例候选中应出现的触发形式一致。",
	      "negative_control_form": "负向对照中的替代形式。",
	      "max_ast_edits": 1,
	      "preserve_setup": true,
	      "preserve_oracle": true,
	      "preserve_target_api": true
	    }}
	  ],
	  "trace_targets": [
	    {{
	      "module": "",
	      "class_name": "",
	      "function_name": "",
	      "source_file": "",
	      "required": true
	    }}
	  ],
	  "public_observation_schema": [
	    "exception_type",
	    "return_value",
	    "return_type",
	    "warning_type",
	    "public_state",
	    "serialization",
	    "render_output",
	    "ordering",
	    "sql_tokens",
	    "shape",
	    "dtype"
	  ],
	  "trigger_contract": {{
	    "required_conditions": [],
	    "target_apis": [],
	    "call_sequence": [],
	    "state_constraints": [],
	    "boundary_conditions": []
	  }},
	  "failure_contract": {{
	    "buggy_symptom": {{}},
	    "allowed_failure_types": [],
	    "forbidden_side_failures": ["setup_error", "collect_error", "syntax_error", "environment_error"]
	  }},
	  "expected_contract": {{
	    "expected_behavior": {{}},
	    "public_observation_targets": [],
	    "preferred_oracle_families": []
	  }},
	  "localization_contract": {{
	    "suspected_files": [],
	    "suspected_functions": [],
	    "trace_targets": []
	  }},
	  "uncertainties": [
	    "当前仍然不确定、需要后续通过运行相似测试或插桩观测确认的信息。"
	  ]
	}}

规则：
1. 所有字段都必须出现；
2. 如果某个字段没有足够依据，填写空数组、空字符串或 confidence=low；
3. 不要复制大段代码到 JSON 里；
4. 不要输出 markdown；
5. 不要输出 JSON 之外的任何解释；
6. 不要生成测试代码；
7. 不要假设已经有真实修复补丁；
8. expected_behavior 必须描述修复后成立的正向语义，不能复述当前 buggy 行为；
9. assertion_hints 的极性必须与 expected_behavior 一致：应该存在/支持/包含的能力不能建议
   not hasattr、not in 或“保持缺失”；当前会抛异常但修复后应正常时不能建议 raises；
	10. 若 Issue 没给出精确完整字符串，只提取稳定片段、类型或关系，不猜测完整 patched 输出；
	11. essential_trigger_factors 和 trigger_ablation_rules 只填写有证据且可最小消融的因素；
	    如果没有可靠负向对照形式，填写空数组或空字符串，不能为了完整而编造；
		12. trace_targets 只填写源码或 target_apis 能支持的目标函数/类/文件；
		13. 四个 contract 必须严格由已有字段中的证据归纳，不能补写未被 Issue、retrieved code 或 retrieved test 支持的条件；
		14. trigger_contract 要能直接约束 Trigger Search；expected_contract 只描述公开可观察的修复后行为；
		15. 输出必须能被 Python 的 json.loads 直接解析。
