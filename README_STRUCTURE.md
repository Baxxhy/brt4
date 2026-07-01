# BRT4 项目结构

## 1. 当前目录总览

```text
brt4/
  *.py                         核心生成、执行、验证与正式评测代码
  context/                     测试协议恢复
  mutation/                    小变异计划
  data/                        项目输入和历史输入
  retrieval_results/           项目内保留的检索结果
  scripts/                     推荐运行、监控、导出脚本
  scripts/legacy/              只供追溯的历史脚本
  logs/                        兼容空目录
  results/
    cleanup/                   清理清单与静态测试产物
    preserved/                 两次正式历史结果
    runs/                      新实验的统一输出目录
  README.md
  README_RUN.md
  README_STRUCTURE.md
  SELF_CHECK.md
  swt.txt
```

## 2. 每个文件夹说明

| 目录 | 用途与内容 | 自动生成 | 是否可删除 |
|---|---|---:|---|
| `context/` | 从一个 related test 恢复 framework、imports、fixture、class、conftest 和运行协议 | 否 | 不能删除 |
| `mutation/` | 生成并校验 issue 引导的小变异计划 | 否 | 不能删除 |
| `data/` | 输入数据；`legacy_inputs/` 保存从根目录整理来的历史 JSON/TXT | 部分 | 输入不能删除 |
| `retrieval_results/` | 已生成的检索数据或项目内副本 | 否 | 不能删除 |
| `scripts/` | 统一 pipeline、formal wrapper、导出、监控和兼容入口 | 否 | 不能删除 |
| `scripts/legacy/` | 历史命令留档，不再推荐执行 | 否 | 确认不需追溯后可删 |
| `logs/` | 旧接口兼容空目录；新日志不写这里 | 是 | 可清空，目录保留 |
| `results/cleanup/` | 移动、删除、保护、大小、哈希清单及合成测试 | 是 | 旧清单可删 |
| `results/preserved/` | 固定保留 42% 与 40% 的 generation/evaluation/log/scripts | 否 | 不能删除 |
| `results/runs/` | 所有新 run，含 generation、evaluation、logs、checkpoints、exports | 是 | 不需要的旧 run 可删 |

## 3. 每个 Python 代码文件

### `__init__.py`

声明 `brt4` Python 包。被所有 `python -m brt4.*` 入口使用；不读写实验数据，属于基础文件。

### `api_pool.py`

定义本地 OpenAI-compatible API 账户列表与 `configured_apis()` 校验。由 `llm_client.py` 使用；读取代码内账户配置，不写输出。属于运行配置，禁止把 key 复制到日志或 manifest。

### `config.py`

定义默认 top-k、worker、反馈轮数、timeout、temperature、token，以及 `LLMConfig`/`load_llm_config()`。由 CLI 和 LLM 客户端读取；不直接写文件。属于核心配置。

### `direct_eval.py`

真实 patch 正式评测入口 `main()`。主要函数有 `evaluate_one()`、`test_command()`、`setup_command()`、`fill_patches_from_swebench_lite()`。读取实例、generation 的 `final_test.py` 和 host context；在同级新文件运行 buggy/patch 两侧；写 `worker_*/results.json`、`merged_results.json` 和 `metrics.json`。只属于正式评测，不参与生成。

### `dual_version.py`

提供可选 `run_dual_version_validation()`，用于显式 patched repo/patch file 的双版本运行。当前统一生成流程使用 surrogate patch，真实 patch 由 `direct_eval.py` 独立处理。

### `executor.py`

统一命令执行和状态分类，入口为 `run_command_in_conda()` 与 `classify_execution()`。读取命令、cwd、Conda 环境和 issue 线索，输出结构化 `ExecutionResult`；由 feedback、host context、oracle 和验证逻辑调用。核心流程文件。

### `feedback.py`

主实例流水线 `run_instance_pipeline()`，还负责 `prepare_instance_worktree()`、checkpoint 评分保存和依赖恢复。串联 issue rewrite、协议恢复、mutation、生成、环境循环、strict verifier、oracle、surrogate patch；写实例 worktree、各轮代码/日志、summary 和 final test。核心流程中心。

### `generator.py`

候选生成和修复，主要入口 `generate_candidate()`、`repair_candidate()`、`write_candidate_to_repo()`。读取 BehaviorTarget、HostContext、源码上下文、mutation plan 和执行反馈；写同级完整 BRT 并返回 `CandidateTest`。核心流程文件。

### `host_context.py`

对 related tests 评分、选种子并恢复静态上下文，主要入口 `rank_related_tests()`、`select_related_test()`、`build_host_context()`。读取 buggy worktree 的完整测试文件和局部模型；输出 `HostContext`。核心流程文件。

### `icore_env_constants.py`

保存移植进 BRT4 的环境枚举和实例结构，如 `SWEbenchInstance`、`TestStatus`、`PatchType`。不依赖外部 iCoRe Python 包；由本地环境规格代码使用。

### `icore_env_utils.py`

保存本地化环境工具：requirements/environment.yml 获取、测试指令提取、日志和文件锁。被 `icore_exec_spec.py`/`icore_runtime.py` 调用；读取仓库配置，可能写环境日志和锁。属于环境核心。

### `icore_exec_spec.py`

定义 `ExecSpec` 及 `make_exec_spec()`，把实例转换为仓库安装和测试命令规格。由 `icore_runtime.py` 使用；不直接运行模型。

### `icore_runtime.py`

本地封装与历史 iCoRe 实验一致的 Conda 环境和项目命令。主要入口 `ensure_icore_environment()`、`icore_setup_command()`、`icore_test_command()`、`make_instance_spec()`。读取实例和 buggy 仓库，创建/复用环境并输出环境规格。核心环境文件，不在运行时 import 外部 iCoRe。

### `io_utils.py`

输入对齐层。`load_issue_data()` 兼容 JSON/JSONL/dict/list，`load_retrieved_code()`/`load_retrieved_tests()` 去重取 top-k，`build_instance_context()` 构造结构化上下文。读取 issue、检索 JSON 和仓库，不直接执行测试。核心流程文件。

### `issue_rewriter.py`

第一阶段入口 `rewrite_issue()`，将 issue、源码和相关测试转换为 `BehaviorTarget`；保存 prompt、response、behavior JSON、meta 和增强 issue 副本。由 feedback 和独立 rewrite CLI 调用。核心流程文件。

### `llm_client.py`

定义 `LLMClient`，调用 OpenAI-compatible DeepSeek API，处理多账户轮转、重试、429 和超时。读取 `api_pool.py` 或命令行覆盖配置，返回原始模型文本，不应输出密钥。核心模型接口。

### `observation_oracle.py`

增强版观测式 oracle 重绑定，入口 `rebind_observation_oracle()`。生成固定标记 probe、执行并解析公开行为，再依据 issue expected behavior 重写断言；写 oracle probe、observation 和 rebuilt test。核心反馈模块。

### `oracle.py`

基础观测与断言合成模块，入口 `run_observation_probe()`、`synthesize_oracle()`。由旧路径或兼容逻辑调用；读候选和执行上下文，写 probe/observation/最终断言代码。

### `patch_utils.py`

独立 surrogate source patch 生成与临时验证。主要入口 `run_surrogate_patch_loop()`、`generate_surrogate_patch()`、`validate_surrogate_patch_candidate()`。只读取 buggy 源码、issue 和候选测试，不读取真实 patch；在临时仓库写少量 search/replace 并运行 BRT。核心选择模块。

### `prompts.py`

集中保存中文 issue rewrite、协议恢复、mutation、生成、修复、oracle、verifier 和 surrogate patch prompt。由对应模型调用模块读取；不直接写输出。核心方法配置。

### `run.py`

真实生成入口 `python -m brt4.run`。`main()` 解析参数、对不同 Conda 环境交错调度，用环境锁串行共享环境实例，并发调用 `_run_one()`；读取三类输入，写 generation 根 summary 和每实例目录。核心 CLI。

### `run_issue_rewrite.py`

仅运行第一阶段的独立 CLI。并发调用 `issue_rewriter.rewrite_issue()`，读取 issue/检索结果，写逐实例 behavior target 和总 summary。不是完整实验入口。

### `schema.py`

定义全部 dataclass：`InstanceContext`、`BehaviorTarget`、`HostContext`、`ProtocolRecovery`、`MutationPlan`、`CandidateTest`、`ExecutionResult`、`ObservationReport`、`StrictVerifierResult`、`FinalResult` 等，并提供 dict/JSON 转换。所有核心模块共享。

### `semantic_guard.py`

静态语义审计入口 `audit_candidate()`。拒绝多个测试入口、恒真断言、宽泛异常、skip、与 expected behavior 反向的 oracle 等；读取代码和 BehaviorTarget，返回问题列表。核心验收保护。

### `strict_semantic_verifier.py`

严格语义门 `verify_strict_semantics()`。综合静态审计、执行分类、target hit、issue oracle 和模型判断，输出 accept/repair_setup/repair_trigger/repair_oracle/reject 及 failure class。核心反馈决策。

### `utils.py`

通用工具：ID 清洗、截断、目录、原子 JSON、时间、模型 JSON 提取和代码块清理。被几乎所有模块调用；负责 UTF-8 输出基础能力。

### `verifier.py`

基础 buggy-only verifier，入口 `verify_buggy_only()`。读取 issue、BehaviorTarget、候选和执行结果，输出 `VerifierDecision`；strict verifier 开启时作为组合判断的一部分或兼容路径。

## 4. 子模块 Python 文件

### `context/protocol_recovery.py`

`recover_test_protocol()` 从一个 seed 的完整文件恢复 framework、imports、fixtures、class setup、conftest、本地 helper/model 和 runner；`audit_recovered_protocol()` 检查风险。不能合并多个 seed 的 setup。

### `mutation/seed_mutator.py`

`build_mutation_plan()` 结合 BehaviorTarget、单一 seed、协议和上一轮反馈生成并规范化小变异计划，只允许受支持的 mutation operator。

## 5. scripts 中的代码和入口

- `scripts/run_latest_full_pipeline.sh`：唯一推荐的一键编排入口，创建 run、分阶段 generation/evaluation/export、写阶段标记。
- `scripts/run_formal_eval_after_generation.py`：检查 final test 完整性并调用 `brt4.direct_eval`。
- `scripts/export_run_outputs.py`：按完整输入列表汇总四类 UTF-8 JSON，并复制选中 checkpoint。
- `scripts/monitor_run.py`：同时监控 generation 和 formal worker 的只读工具。
- `scripts/monitor_generation.py`：兼容的 generation-only 监控器。
- `scripts/check_compile.sh`：编译核心和维护脚本，跳过 results/worktree。
- `scripts/run_full_generation.sh`、`run_smoke5_generation.sh`、`run_generation_then_eval.sh`：调用统一入口的兼容薄封装。
- `scripts/legacy/`：历史实验命令，仅用于追溯，不应作为新 run 入口。

## 6. 主流程调用关系

```text
scripts/run_latest_full_pipeline.sh
→ python -m brt4.run
→ io_utils.py
→ issue_rewriter.py
→ host_context.py
→ context/protocol_recovery.py
→ mutation/seed_mutator.py
→ generator.py
→ executor.py
→ verifier.py / strict_semantic_verifier.py / semantic_guard.py
→ observation_oracle.py / oracle.py
→ patch_utils.py
→ generation/<instance>/final_test.py

generation 完成后：
scripts/run_formal_eval_after_generation.py
→ brt4.direct_eval
→ evaluation/worker_*/results.json
→ evaluation/merged_results.json + metrics.json
→ scripts/export_run_outputs.py
→ exports/all_outputs.json 等四类导出
```

## 7. 重要配置来源

- `config.py`：默认 worker、timeout、temperature、top-k 和 token。
- `api_pool.py`：7 个本地 API 账户配置；禁止写入结果。
- 命令行参数：本次模型、并发、输入、run 目录、恢复模式。
- `resolved_instances.json`：本次 run 的确定实例列表。
- iCoRe 已生成检索 JSON：只作为输入，不重新检索。
- `swe_repos/` 与 Conda：buggy 工作树来源和执行环境。
- SWE-bench Lite：只在正式评测阶段补充真实 patch。

## 8. 可删与不可删边界

可以删除：

- `results/runs/` 下确认不再需要的旧 run；
- `results/cleanup/` 中过期的清单和合成测试；
- 旧 smoke run、临时日志和 `__pycache__`；
- 已完成且确认不需 resume 的新 run worktree，但应先留删除清单。

不能删除：

- 根目录和子模块源码 `.py`；
- `data/`、`retrieval_results/`、`context/`、`mutation/`；
- `results/preserved/` 两组正式结果；
- 正式输入文件、README 和 `swt.txt`；
- 正在运行或需要恢复的 run。
