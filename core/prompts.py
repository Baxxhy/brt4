"""Chinese prompts used by BRT3."""

ISSUE_REWRITE_SYSTEM_PROMPT = """你是一个软件测试和缺陷复现测试生成专家。你的任务是阅读一个 GitHub Issue、iCoRe 检索出的相关源代码片段、iCoRe 检索出的相关测试代码片段，并将它们转换成结构化的缺陷行为目标。

你的输出会被后续模块用于：
1. 选择相似测试作为起点；
2. 根据 Issue 对相似测试做小变异；
3. 插入运行时观测点；
4. 生成稳定的 assert 断言。

你必须遵守：
1. 不要编造 Issue、源码或测试中没有依据的信息；
2. 要区分 Issue 明确说明的事实和根据代码/测试上下文推断出的内容；
3. 输出必须是单个合法 JSON 对象；
4. 不要输出 markdown；
5. 不要输出解释性文字；
6. 不要生成测试代码；
7. 不要使用 patched version、golden patch 或 golden test 的信息。
"""

ISSUE_REWRITE_USER_PROMPT = """请分析下面这个缺陷复现任务。

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
11. 输出必须能被 Python 的 json.loads 直接解析。
"""

_JSON_RULES = "只输出一个合法 JSON 对象；不要 markdown；不要解释；不要使用真实 patch、patched version 或 golden test；不要生成无关测试。"
_CODE_RULES = "只输出 Python 代码；不要 markdown；不要解释；不要使用真实 patch、patched version 或 golden test；优先复用相似测试上下文；只改必要部分。"

HOST_CONTEXT_SYSTEM_PROMPT = f"你是 Python 测试上下文恢复专家。{_JSON_RULES}"
HOST_CONTEXT_USER_PROMPT = """根据 Issue 结构化目标、相似测试和完整测试文件片段，判断新 BRT 应复用哪些 imports、fixture、class、setup、decorator 和断言风格。只输出 JSON。

实例：{instance_id}
行为目标：{behavior_json}
相似测试：{seed_test}
完整文件片段：{full_file_excerpt}

输出字段：host_file, host_class, seed_test_name, imports, setup_context, fixtures, decorators, pytestmark, insert_strategy, insert_location_hint, risks。
"""

MUTATION_GENERATION_SYSTEM_PROMPT = f"你是缺陷复现测试生成专家。{_CODE_RULES}"
MUTATION_GENERATION_USER_PROMPT = """请基于相似测试做最小变异，生成 1 个 BRT。

实例：{instance_id}
测试函数名必须是：test_brt_{safe_instance_id}
放置策略：必须生成一个完整的新 Python 测试文件，后续会保存到最相似测试同级目录下的 test_brt_{safe_instance_id}.py。
不要输出需要插入到已有 class 或已有文件中的 method 片段。
不要输出裸缩进代码。
如果需要 class wrapper，请在新文件中完整定义 class，并包含必要 imports。
完整文件必须能作为独立 .py 文件被 pytest/Django/Sympy 测试命令收集；不能依赖原相似测试文件里已经 import 的名字、模型、fixture helper、全局变量或 class，除非你在新文件中显式 import 或定义。
如果复用相似测试中的 TestCase、fixture、helper、model、decorator、pytestmark 或断言风格，必须把运行所需的 import/decorator/setup 一并写入新文件。
如果 HostContext.setup_context 包含类级属性（例如 CHECKER_CLASS、databases、app_label、配置常量），必须保留其精确名称和值，不得根据测试名称猜测替代类。
不得引入相似测试、完整宿主文件、相关源码或 Python 标准库中没有依据的第三方模块。
必须服从 HostContext 中的实际 runner：如果 runner 是 SymPy bin/test，不要默认 import pytest 或使用 pytest fixture；如果 runner 是 Django runtests，使用现有 Django TestCase 与测试应用约定。
测试的断言必须表达 expected_behavior，而不是期待 buggy error_symptom 继续发生。
完整文件只能包含一个可收集的 test 函数或 test method；不要生成 baseline、对照组、
备用候选或多个测试入口，因为评测只执行这个唯一 BRT。
不得使用 pytest.skip、skipIf、skipUnless 或平台不满足时提前 return；测试必须真实执行。
不得使用 assert True、A or not A、x == x or x != x 等恒真断言。
不得用 broad try/except 吞掉异常，不得 mock/patch 掉 target API 或 Issue 要观察的内部行为。
Issue 给出 MWE、字面输入、参数、operator、调用顺序时必须原样保留这些路径锚点；
不能为了让测试可运行而换成更简单但不触发缺陷的输入或 API。
如果缺陷是“缺少检查、缺少 warning、缺少状态更新或静默接受错误配置”，不要寻找一个
buggy 版本已经存在的异常路径。应调用项目真实的检查/验证/状态转换 API，并断言修复后
应出现的稳定证据；buggy 版本因证据缺失而自然 assertion fail。
如果缺陷是“缺少日志”，使用 assertLogs/caplog 捕获目标模块的日志命名空间；
不要 patch buggy 源码中尚不存在的 logger 属性。
只有 expected_behavior 明确要求抛异常时才使用 assertRaises/pytest.raises。
行为目标：{behavior_json}
HostContext：{host_context_json}
相关源码：{code_context}
相似测试代码：{seed_test_code}
上一轮反馈：{feedback}

要求：保留可执行上下文，只修改输入值、参数、对象状态、mock、配置、调用链、边界值或 operator 中与 Issue 相关的部分。不要裸 assert False。只输出 Python 代码，不要 markdown，不要解释。
"""

OBSERVATION_PROBE_SYSTEM_PROMPT = f"你是测试插桩观测专家。{_CODE_RULES}"
OBSERVATION_PROBE_USER_PROMPT = """请把下面候选测试改成 probe 测试，插入运行时观测点。必须打印固定标记：
print("BRT_OBS_START")
print(json.dumps(observations, default=str, ensure_ascii=False))
print("BRT_OBS_END")

所有目标调用完成后只打印一次标记；不要在观测尚未完成时提前打印。
probe 可以捕获并记录异常，但不能吞掉 setup/import/collection 错误。

行为目标：{behavior_json}
候选测试：{candidate_code}
"""

ASSERT_SYNTHESIS_SYSTEM_PROMPT = f"你是稳定断言生成专家。{_CODE_RULES}"
ASSERT_SYNTHESIS_USER_PROMPT = """请根据观测结果和 Issue 语义，把 probe/candidate 改成干净的最终 BRT。不能保留 BRT_OBS 打印。

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
"""

BUGGY_ONLY_VERIFIER_SYSTEM_PROMPT = f"你是 buggy-only 缺陷复现测试判定器。{_JSON_RULES}"
BUGGY_ONLY_VERIFIER_USER_PROMPT = """根据执行结果判断下一步。输出字段 decision, reason, focus, next_action。
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
"""

REPAIR_SETUP_SYSTEM_PROMPT = f"你是测试环境修复专家。{_CODE_RULES}"
REPAIR_SETUP_USER_PROMPT = """当前测试存在 setup/import/fixture/class/collection/syntax 问题。只修复可执行上下文，不改变缺陷触发目标。
必须返回完整 Python 测试文件，后续会保存为同级目录下的 test_brt_<instance_id>.py。
不要返回 method 片段，不要依赖插入到已有 class/file。
优先删除没有上下文依据、环境中不存在的第三方 import，不要建议安装新依赖。
必须优先从 HostContext.setup_context 恢复原 class 的类级属性，不能编造不存在的 checker/model/helper 名称。
测试函数名、class wrapper 或 runner 不匹配时，必须让返回代码中的真实测试名称与 HostContext runner 可收集的名称一致。
如果 HostContext runner 是 SymPy bin/test，不要使用 pytest fixture；如果是 Django runtests，不要手工配置一个与项目测试应用冲突的全局 settings。
如果 HostContext 已提供可用的项目测试模型、fixture 或 helper，必须优先复用，不能重新声明同名模型。
HostContext.model_context 是这些导入模型在 buggy 仓库中的真实定义；字段名、必填字段、
关系类型和 app_label 必须以它为准，不得凭 Issue 示例猜测仓库模型结构。
不得把仓库中不存在的模块名或临时 app_label 加入 INSTALLED_APPS。只有 HostContext 或仓库文件明确存在的应用才能进入 Django 配置。
在 Django 中，`Model.field_name` 通常是 DeferredAttribute descriptor，不是 Field 实例；
需要调用字段方法时必须使用 `Model._meta.get_field("field_name")`。若 standalone Field
调用 check() 因 name/model 元数据缺失而失败，可先调用 set_attributes_from_name()
补齐字段名，或在已存在的测试模型上通过 `_meta.get_field()` 取得字段；不要退回无关的
clean()/save() 路径。
当 Issue 要求新增日志而 buggy 源码尚无 logger 时，setup 修复必须保留 assertLogs/caplog
观测方式，不能通过 patch 不存在的 logger、_logger 属性来制造环境错误。
若 MigrationWriter、deconstruct、pickle、serializer 或其他序列化流程生成的路径含
`<locals>`，说明被序列化的自定义类或函数仍定义在测试方法内部。必须把这些定义真正
移动到 Python 文件模块顶层，使其 `__module__` 和 `__qualname__` 可导入；仅修改注释、
重命名局部类，或在方法内声称“module-level”都不是修复。
若测试通过 exec() 执行 inspectdb 或其他动态生成的 Django 模型代码，必须为执行
namespace 提供一个仓库中真实存在且可注册的模块/app context，或只对生成文本做与
Issue 对齐的 compile/结构检查；不能让动态模型因为缺少 app_label/INSTALLED_APPS
而在目标行为之前失败。
若测试管理命令的 parser/help 行为，优先直接实例化已有或测试内定义的 Command，并
调用 create_parser()/print_help()/run_from_argv()。Django 测试进程启动后再临时修改
DJANGO_SETTINGS_MODULE、sys.path 并创建 app 目录，不会可靠刷新 management command
注册缓存，不能把 `fetch_command` 的 KeyError 当成缺陷结果。
若生成测试自己注册 admin/model/URL 后出现 NoReverseMatch，而 Issue 不是专门报告
NoReverseMatch，必须修复真实 admin 注册与 URL name，或直接从实际 response/link
验证 Issue 所述 URL 语义；不能把测试自造 URL 不存在当作缺陷。
若 SQLite 在建表阶段因 PostgreSQL 专属字段（例如 ArrayField 的 `[]` 类型）报 SQL
语法错误，必须避免让该模型参与 SQLite 测试数据库建表。优先复用仓库已有 PostgreSQL
测试上下文，或把仅供表单/field/formset 构造的临时模型设为 `Meta.managed = False`
并避免数据库读写，在不建表的层次复现输入解析与状态保留行为。仅添加
skipUnlessDBFeature 无效，因为测试数据库会在测试方法执行前建表。不能把后端不兼容
当作 Issue 失败。

行为目标：{behavior_json}
HostContext：{host_context_json}
当前测试：{candidate_code}
执行日志：{execution_log}
Verifier 反馈：{verifier_feedback}

必须优先完成 Verifier 反馈中的 next_action。若反馈指出具体 import、fixture、
class、nodeid 或 setup 问题，返回代码必须实质修改对应位置，不能原样返回。
"""

REPAIR_TRIGGER_SYSTEM_PROMPT = f"你是缺陷路径触发修复专家。{_CODE_RULES}"
REPAIR_TRIGGER_USER_PROMPT = """当前测试没有触发 Issue 相关路径或失败无关。保留 setup，只调整输入、参数、状态、mock、配置、调用链或边界条件。
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
"""

REPAIR_ORACLE_SYSTEM_PROMPT = f"你是断言修复专家。{_CODE_RULES}"
REPAIR_ORACLE_USER_PROMPT = """当前测试触达相关行为但 oracle 可能错误或脆弱。只修复断言和观测证据，不改 setup。
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
"""

SURROGATE_PATCH_SYSTEM_PROMPT = """你是最小生产代码修补代理。你只能根据 Issue、结构化行为目标、检索源码、当前 BRT 和执行日志生成临时 surrogate patch。

严格禁止：
1. 使用、猜测或请求 golden patch、patched version、golden test；
2. 修改 BRT、已有测试、测试配置或依赖文件来让测试通过；
3. 跳过、xfail、mock 掉目标行为，或捕获并吞掉异常；
4. 大范围重写生产代码；
5. 输出 markdown 或 JSON 之外的文字。

输出必须是单个合法 JSON 对象。每个修改使用精确 search/replace，search 必须来自提供的源码。"""

SURROGATE_PATCH_USER_PROMPT = """请生成一个最小 surrogate source patch，用于判断当前 BRT 是否可能是 fail-to-pass 测试。

行为目标：
{behavior_json}

buggy 版本测试：
{final_test}

buggy 执行日志：
{buggy_execution_log}

允许修改的相关源码：
{code_context}

此前 surrogate 尝试及反馈：
{previous_attempts}

要求：
1. 只修改上面列出的生产源码文件，最多 3 个 search/replace；
2. 修补应实现 expected_behavior，而不是针对测试返回常量；
3. 不得修改任何 tests、test_*.py、conftest.py、配置或依赖文件；
4. search 必须逐字复制自提供源码，并且尽量是 3-30 行的小片段；不要复制或重写整个函数/类；
5. replace 只包含这个小片段的替换版本，必须和 search 有实质差异；
6. 如果前一次 patch 已经应用但测试仍失败，必须根据反馈改变根因假设或修改位置，不能原样重复 patch；
7. 如果证据不足，返回空 patches，不要编造。

只输出：
{{
  "analysis": "简短说明根因和修补策略",
  "confidence": "high|medium|low",
  "patches": [
    {{
      "path": "相关生产源码相对路径",
      "search": "需要替换的精确原始文本",
      "replace": "替换后的完整文本",
      "reason": "该修改如何实现 expected_behavior"
    }}
  ]
}}
"""


_ENHANCEMENT_RULES = """
通用硬约束：
1. 不得使用、猜测或请求真实 patch、golden patch、golden test、FAIL_TO_PASS 或 PASS_TO_PASS。
2. 只能生成一个测试入口，不得修改原测试文件。
3. 不得吞异常，不得写 assert True/assert False，不得使用 pytest.raises(Exception)，不得无条件 skip。
4. expected_behavior 必须来自 Issue；buggy observation 只能帮助选择观察对象，不能直接作为 expected value。
5. 优先断言公开行为，保留 seed test 的测试协议，只做 Issue 相关的小变异。
"""

PROTOCOL_RECOVERY_SYSTEM_PROMPT = """你是 Python 项目测试协议恢复专家。你只分析一个相关测试及其所在上下文，不合并其他测试的 setup。只输出一个合法 JSON 对象，不输出 Markdown或解释。""" + _ENHANCEMENT_RULES
PROTOCOL_RECOVERY_USER_PROMPT = """请审计下面自动恢复的测试协议，补充风险和 runner_hints，但不得发明不存在的 fixture、helper 或配置。
BehaviorTarget：{behavior_json}
相关测试：{seed_test}
自动恢复结果：{protocol_json}
输出字段必须与自动恢复结果相同。"""

SEED_MUTATION_PLAN_SYSTEM_PROMPT = """你是基于相似测试的缺陷触发变异规划器。先规划最小变异，不生成测试代码。只输出一个合法 JSON 对象，不输出 Markdown或解释。""" + _ENHANCEMENT_RULES
SEED_MUTATION_PLAN_USER_PROMPT = """根据 Issue 目标、单一 seed 和测试协议生成一个小变异计划。最多选择 3 个 mutation_ops；不得重写无关 setup，不得编造 expected value。

BehaviorTarget：{behavior_json}
HostContext：{host_context_json}
ProtocolRecovery：{protocol_json}
上一轮执行反馈：{execution_feedback}
Verifier 反馈：{verifier_feedback}

如果 buggy PASS 或 target_not_hit，优先 CALL_CHAIN_EXTEND、LIFECYCLE_TRIGGER、CONFIG_MUTATION。
如果已进入目标 API 但仍 PASS，优先 ARG_BOUNDARY_EXPAND、ARG_VALUE_REPLACE、OPERATOR_FLIP、STATE_MUTATION、FIXTURE_DATA_MUTATION。
如果 oracle 可疑，不要继续扩大 trigger。

只输出：
{{
  "mutation_goal": "",
  "preserve_from_seed": [],
  "target_api": [],
  "target_path": [],
  "mutation_ops": [],
  "expected_behavior": "只复述 Issue 明确行为",
  "oracle_strategy": "公开行为",
  "why_this_should_trigger": "",
  "risk": "low|medium|high"
}}"""

OBSERVATION_ORACLE_SYSTEM_PROMPT = """你是观测式测试 oracle 重绑定专家。你必须让 oracle 来自 Issue expected_behavior，而不是复制 buggy 观测值。代码任务只输出完整 Python 文件，JSON 任务只输出 JSON。""" + _ENHANCEMENT_RULES
OBSERVATION_ORACLE_PROBE_PROMPT = """把当前单入口测试改为 probe。保留 setup 和 trigger，只移除或替换原断言，观测公开行为。必须打印 BRT_OBS_START、一个 JSON、BRT_OBS_END。
BehaviorTarget：{behavior_json}
ProtocolRecovery：{protocol_json}
当前测试：{candidate_code}
只输出完整 Python 文件。"""
OBSERVATION_ORACLE_REBIND_PROMPT = """根据 Issue expected_behavior 和公开行为观测重写当前测试的 oracle。不要把 buggy observation 直接当 expected value；不要改 setup 和 trigger。SQL 只检查关键片段，warning/log 使用专用断言；不应崩溃时增加最小公开不变量。
BehaviorTarget：{behavior_json}
ProtocolRecovery：{protocol_json}
允许的 oracle_type：NO_EXCEPTION|EXCEPTION_TYPE|WARNING|LOGGING|EXACT_VALUE|TYPE_OR_SHAPE|STATE_CHANGE|SQL_VALIDITY|SERIALIZATION|RENDER_OUTPUT|ORDERING
观测：{observation_json}
当前测试：{candidate_code}
执行日志：{execution_log}
第一行用注释写 # BRT_ORACLE_TYPE: <type>，随后只输出完整 Python 文件。"""

STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT = """你是严格 Bug Reproduction Test 语义验证器。不能只因 API 名或异常关键词出现在日志中就接受。只输出一个合法 JSON 对象，不输出 Markdown或解释。""" + _ENHANCEMENT_RULES
STRICT_SEMANTIC_VERIFIER_USER_PROMPT = """判断当前测试在 buggy 上的失败是否真能表达 Issue expected_behavior。
Issue：{issue_text}
BehaviorTarget：{behavior_json}
ProtocolRecovery：{protocol_json}
测试代码：{candidate_code}
命令：{command}
执行分类：{execution_status}
stdout/stderr：{execution_log}
相关源码：{source_context}

accept 必须同时满足：buggy 失败；非环境/语法/收集/超时；目标 API/函数/生命周期确实被执行；失败与 symptom 对齐；oracle 来自 Issue；检查公开行为。
输出：
{{
  "decision": "accept|repair_setup|repair_trigger|repair_oracle|reject",
  "failure_class": "setup|syntax|collect|timeout|buggy_pass|target_not_hit|side_path|oracle_wrong|oracle_too_strong|issue_aligned",
  "target_hit": false,
  "oracle_grounded_in_issue": false,
  "uses_public_behavior": false,
  "reason": "",
  "next_action": "repair_setup|repair_trigger|repair_oracle|reject"
}}"""
