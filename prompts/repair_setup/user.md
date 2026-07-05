当前测试存在 setup/import/fixture/class/collection/syntax 问题。只修复可执行上下文，不改变缺陷触发目标。
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
