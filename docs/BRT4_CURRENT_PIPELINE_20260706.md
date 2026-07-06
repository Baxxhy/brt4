# BRT4 当前完整流程 20260706

本文档按当前 brt4 代码梳理端到端流程。它描述当前实现，不描述未实现的设想。

## Step 1：配置加载与实例读取

### 输入

- `scripts/run_generate.sh` 或 `python -m brt4.run` 传入的 CLI 参数。
- 默认 issue 文件：`data/issues/swt276_issues.json`。
- 默认 code retrieval：`retrieval_results/code/code_retrieval_results_gpt.json`。
- 默认 test retrieval：`retrieval_results/test/icore/gpt/related_tests.json`。
- 默认 repo root：`../swe_repos`。
- 运行参数：`model`、`temperature`、`max_workers`、`timeout`、`num_candidates`、`output_dir`。

### 处理

`run.py` 解析参数，读取 276 个 issue 实例，为每个实例构造 `InstanceContext`。实例字段进入系统后会被后续 retrieval、generation、execution 和 evaluation 阶段复用。

### 输出

- 每个实例的 `InstanceContext`。
- run 级别输出目录：`results/runs/<run_name>/generation/`。
- run 配置：`run_config.json`。
- 实际命令：`command.txt`。

### 涉及文件

- `run.py`
- `config.py`
- `core/schema.py`
- `scripts/run_generate.sh`
- `scripts/run_full_pipeline.sh`

### 关键字段

- `instance_id`
- `repo`
- `base_commit`
- `problem_statement`
- `patch`
- `test_patch`
- `repo_root`
- `output_dir`

### 下一步

进入 retrieval 读取与 issue/behavior target 准备。

## Step 2：iCoRe code/test retrieval 读取

### 输入

- `InstanceContext`
- code retrieval JSON
- test retrieval JSON

### 处理

当前流程读取 iCoRe 检索得到的 source code 和 related tests。code context 用于提示模型理解目标 API、调用路径和可能修改区域；test context 用于 seed ranking、HostContext recovery 和 scaffold 恢复。

### 输出

- `RetrievedCode` 列表。
- `RetrievedTest` 列表。
- 排序后的 related tests。

### 涉及文件

- `retrieval/icore_env_constants.py`
- `retrieval/icore_env_utils.py`
- `retrieval/icore_exec_spec.py`
- `retrieval/icore_runtime.py`
- `core/schema.py`
- `execution/feedback.py`

### 关键字段

- `file`
- `name`
- `code`
- `score`
- `repo`
- `instance_id`

### 下一步

进入 BehaviorTarget 加载或 issue rewrite。

## Step 3：Issue rewrite / BehaviorTarget

### 输入

- `problem_statement`
- 检索上下文
- 可选已有 behavior target cache
- prompts：`prompts/issue_rewrite/`、`prompts/behavior_target/`

### 处理

如果存在已缓存的 behavior target，当前 generation 会优先读取缓存，避免重新跑 issue rewrite。没有缓存时才通过 LLM 重写 issue 并抽取行为目标。BehaviorTarget 用于后续 mutation、generation、verifier 和 risk check。

### 输出

- `behavior_target.json`
- `BehaviorTarget`

### 涉及文件

- `issue/issue_rewriter.py`
- `execution/feedback.py`
- `prompts/loader.py`
- `prompts/issue_rewrite/`
- `prompts/behavior_target/`
- `core/schema.py`

### 关键字段

- `summary`
- `trigger_condition`
- `expected_behavior`
- `error_symptom`
- `target_apis`
- `mutation_hints`
- `assertion_hints`
- `setup_hints`
- `evidence_spans`

### 下一步

进入 seed ranking。

## Step 4：Seed ranking

### 输入

- `RetrievedTest` 列表。
- `BehaviorTarget`
- source/test retrieval context。

### 处理

当前流程对 related tests 进行排序，选择更可能提供有效 scaffold 和触发路径的 seed test。排序结果供 adaptive top3 seed retry 使用。

### 输出

- rank 后的 seed candidates。
- seed 的 file/name/score 等信息。

### 涉及文件

- `execution/feedback.py`
- `core/schema.py`
- `context/host_context.py`

### 关键字段

- `selected_seed_file`
- `selected_seed_name`
- `score`
- `file`
- `name`

### 下一步

进入 adaptive top3 seed retry。

## Step 5：Adaptive top3 seed retry

### 输入

- rank 后 top seed candidates。
- `InstanceContext`
- `BehaviorTarget`
- retrieval context。
- generation output instance dir。

### 处理

当前版本最多尝试 top3 seed，但不是无条件全跑。每个 seed 会在自己的 `seed_candidates/seed_<idx>/` 中执行完整生成链路。如果当前 seed 产生 `SURROGATE_F2P_SUCCESS`、verifier accept + issue-aligned failure，或 observation oracle 成功 rebind 并保留 buggy fail，可以提前停止。若出现 `PASS`、`BUGGY_PASS`、`UNRELATED_FAIL`、scaffold 不稳定或没有可竞争 checkpoint，则尝试下一个 seed。

### 输出

- `seed_candidates/seed_<idx>/`
- `seed_attempts_summary.json`
- `selected_seed_summary.json`
- 顶层 `summary.json` 中的 seed 选择字段。

### 涉及文件

- `execution/feedback.py`
- `core/schema.py`

### 关键字段

- `seed_mode`
- `selected_seed_index`
- `seed_attempts_count`
- `seed_attempts_summary`
- `seed_switch_reasons`
- `selected_seed_reason`

### 下一步

每个 seed 内进入 HostContext recovery。

## Step 6：HostContext recovery

### 输入

- seed test file/name/code。
- repository worktree。
- `BehaviorTarget`。

### 处理

HostContext recovery 从原测试文件中恢复 imports、fixture、class、setup、decorator、pytestmark、Django test settings、runner selector 等 scaffold 信息。它决定生成测试应该放在哪里、用什么 selector 执行，以及如何保留宿主测试环境。

### 输出

- `host_context.json`
- `HostContext`

### 涉及文件

- `context/host_context.py`
- `execution/feedback.py`
- `core/schema.py`

### 关键字段

- `host_file`
- `host_class`
- `imports`
- `fixtures`
- `pytestmark`
- `decorators`
- `setup_code`
- `candidate_repo_path`
- `runner_kind`
- `test_command`
- `selector`

### 下一步

进入 ProtocolRecovery。

## Step 7：ProtocolRecovery

### 输入

- `HostContext`
- seed test code。
- `BehaviorTarget`
- retrieval context。
- prompts：`prompts/protocol_recovery/`

### 处理

ProtocolRecovery 尝试恢复 seed 的调用协议、fixture 使用方式、runner 约束和潜在 scaffold 风险。失败时保留 AST/HostContext 恢复结果，并在 protocol risks 中记录风险，不直接中断整个实例。

### 输出

- `protocol_recovery.json`
- `ProtocolRecovery`

### 涉及文件

- `execution/feedback.py`
- `prompts/protocol_recovery/`
- `core/schema.py`

### 关键字段

- `recovered`
- `setup_steps`
- `call_sequence`
- `assertion_style`
- `protocol_risks`
- `selected_seed_name`

### 下一步

进入 MutationPlan。

## Step 8：MutationPlan

### 输入

- `BehaviorTarget`
- `HostContext`
- `ProtocolRecovery`
- seed test。
- source/test context。
- prompts：`prompts/mutation_plan/`

### 处理

MutationPlan 用显式 mutation operator 指导生成阶段如何从 seed test 变形到 bug reproduction test。当前版本不扩大 mutation operators 数量，不引入多 aspect/morph/mask。

### 输出

- `mutation_plan.json`
- `MutationPlan`

### 涉及文件

- `mutation/`
- `execution/feedback.py`
- `prompts/mutation_plan/`
- `core/schema.py`

### 关键字段

- `operators`
- `rationale`
- `target_api`
- `risk`
- `expected_failure`

### 下一步

进入 BRT generation。

## Step 9：BRT generation

### 输入

- `InstanceContext`
- `BehaviorTarget`
- `RetrievedCode`
- `RetrievedTest`
- `HostContext`
- `ProtocolRecovery`
- `MutationPlan`
- prompts：`prompts/generation/`
- model/temperature。

### 处理

generation 生成一个 candidate test。测试通常结合 host scaffold，放入目标 repo 中合适的测试目录。当前外部契约仍是 `num_candidates=1` 和 one final test，adaptive seed 只在内部为 seed 失败时重试。

### 输出

- candidate test code。
- `selected_candidate.py`
- seed candidate 下的中间文件。
- 顶层最终 `final_test.py`。

### 涉及文件

- `generation/generator.py`
- `execution/feedback.py`
- `prompts/generation/`
- `core/schema.py`

### 关键字段

- `code`
- `candidate_repo_path`
- `test_command`
- `selector`
- `round`

### 下一步

进入 execution / environment qualification。

## Step 10：Execution / environment qualification

### 输入

- candidate test code。
- repo worktree。
- env name / conda setup。
- `HostContext.test_command`。
- timeout。

### 处理

执行阶段准备 worktree、写入 candidate test，按 pytest/unittest/Django runner 执行。它会区分 setup、collection、syntax、import、buggy pass、issue-aligned fail、unrelated fail 等状态。

### 输出

- `ExecutionResult`
- 执行日志。
- candidate checkpoint 的 execution 部分。

### 涉及文件

- `execution/executor.py`
- `execution/feedback.py`
- `retrieval/icore_runtime.py`
- `core/schema.py`

### 关键字段

- `command`
- `cwd`
- `returncode`
- `stdout`
- `stderr`
- `duration`
- `timeout`
- `status`
- `error_reason`

### 下一步

进入 strict verifier。

## Step 11：Strict verifier

### 输入

- candidate code。
- execution result。
- `BehaviorTarget`。
- issue text。
- source/test context。
- prompts：`prompts/verifier/`、`prompts/strict_semantic_verifier/`

### 处理

strict verifier 检查 candidate 是否真正命中 issue 行为目标，是否存在 assert True、skip、过宽 exception、吞异常、私有内部细节等风险，并给出 accept/repair/reject 方向。

### 输出

- `StrictVerifierResult`
- `VerifierDecision`

### 涉及文件

- `validation/verifier.py`
- `validation/strict_semantic_verifier.py`
- `validation/semantic_guard.py`
- `execution/feedback.py`
- `core/schema.py`

### 关键字段

- `decision`
- `target_hit`
- `issue_aligned`
- `failure_class`
- `reason`
- `suggestions`

### 下一步

根据 decision 进入 repair 或 checkpoint selection。

## Step 12：repair_setup / repair_trigger / repair_oracle

### 输入

- candidate code。
- execution log。
- verifier decision。
- `BehaviorTarget`。
- `HostContext`。
- prompts：`prompts/repair_setup/`、`prompts/repair_trigger/`、`prompts/repair_oracle/`

### 处理

repair loop 根据失败类型选择修复方向：

- `repair_setup` 修复 import、fixture、collection、runner、placement 等问题。
- `repair_trigger` 强化触发路径，避免 `PASS` / `BUGGY_PASS`。
- `repair_oracle` 调整断言，避免过强或不对齐 oracle。

每轮修复后重新执行并再次 verifier。

### 输出

- repaired candidate。
- 新 execution result。
- 新 verifier result。
- 新 checkpoint。

### 涉及文件

- `execution/feedback.py`
- `execution/executor.py`
- `prompts/repair_setup/`
- `prompts/repair_trigger/`
- `prompts/repair_oracle/`
- `core/schema.py`

### 关键字段

- `repair_type`
- `round`
- `status`
- `decision`
- `error_reason`

### 下一步

必要时进入 observation oracle，否则进入 surrogate validation / selection。

## Step 13：observation oracle

### 输入

- candidate code。
- execution log。
- `BehaviorTarget`。
- prompts：`prompts/observation_probe/`、`prompts/observation_oracle/`、`prompts/assert_synthesis/`

### 处理

observation oracle 用执行观察结果辅助重新绑定 oracle。它用于减少 oracle 不对齐导致的失败，但不会改变 formal eval 定义。

### 输出

- `ObservationReport`
- rebind 后的 candidate 或 oracle 信息。

### 涉及文件

- `generation/observation_oracle.py`
- `generation/oracle.py`
- `execution/feedback.py`
- `prompts/observation_probe/`
- `prompts/observation_oracle/`
- `prompts/assert_synthesis/`
- `core/schema.py`

### 关键字段

- `observations`
- `assertion`
- `oracle_rebound`
- `status`

### 下一步

进入 surrogate patch validation。

## Step 14：surrogate patch validation

### 输入

- issue-aligned buggy failing candidate。
- source context。
- `BehaviorTarget`。
- prompts：`prompts/surrogate_patch/`
- worktree。

### 处理

surrogate patch validation 尝试生成或应用 surrogate patch，并在 buggy 与 surrogate patched version 上运行 candidate。它是 generation selection 信号，不使用 golden patch，也不等于 formal F2P。

### 输出

- `dual_version_result.json`
- `DualVersionResult`

### 涉及文件

- `execution/dual_version.py`
- `execution/patch_utils.py`
- `execution/feedback.py`
- `prompts/surrogate_patch/`
- `core/schema.py`

### 关键字段

- `mode`
- `status`
- `buggy_execution`
- `patched_execution`
- `surrogate_patch`
- `attempts`

### 下一步

进入 oracle/surrogate risk scoring。

## Step 15：oracle/surrogate risk scoring

### 输入

- candidate code。
- `BehaviorTarget.expected_behavior`
- `BehaviorTarget.error_symptom`
- issue text。
- execution log。
- optional observation report。
- `DualVersionResult`。
- retrieved source paths。

### 处理

`validation/oracle_risk.py` 计算 oracle risk 和 surrogate risk。`execution/feedback.py` 用软扣分更新 selector score。风险高不会 hard reject，只影响排名并写入 JSON。

### 输出

- `oracle_risk`
- `surrogate_risk`
- `selector_score_before_risk`
- `selector_score_after_risk`
- `selector_penalty_reasons`

### 涉及文件

- `validation/oracle_risk.py`
- `execution/feedback.py`
- `core/schema.py`

### 关键字段

- `level`
- `reasons`
- `signals`
- `selector_score_after_risk`

### 下一步

进入 cross-seed candidate selection。

## Step 16：cross-seed candidate selection

### 输入

- 每个 seed 的 candidate checkpoints。
- risk-adjusted scores。
- execution/verifier/surrogate 状态。

### 处理

跨 seed 按风险调整后的 checkpoint score 选择最终 candidate。优先级大体为 surrogate success、verifier accept + buggy fail、executable buggy fail、PASS/BUGGY_PASS、setup/collect/syntax/import failure。同分时优先更早 seed 和更早 checkpoint。

### 输出

- selected checkpoint。
- `selected_seed_summary.json`
- `candidate_ranking.json`
- 顶层 `final_test.py`。

### 涉及文件

- `execution/feedback.py`
- `core/schema.py`

### 关键字段

- `status`
- `reason`
- `score`
- `selected_seed_index`
- `selector_score_after_risk`

### 下一步

写 final outputs。

## Step 17：final_test.py / summary.json 输出

### 输入

- selected candidate。
- selected seed metadata。
- execution/verifier/surrogate/risk results。

### 处理

实例目录写入最终测试和 summary。即便所有 seed 都 `PASS` 或 `BUGGY_PASS`，也尽量保留一个 `final_test.py`，并在 summary 标注状态和风险。

### 输出

```text
generation/<instance_id>/final_test.py
generation/<instance_id>/summary.json
generation/<instance_id>/candidate_ranking.json
generation/<instance_id>/seed_attempts_summary.json
generation/<instance_id>/selected_seed_summary.json
generation/<instance_id>/host_context.json
generation/<instance_id>/behavior_target.json
```

### 涉及文件

- `execution/feedback.py`
- `io/io_utils.py`
- `core/schema.py`

### 关键字段

- `FinalResult.status`
- `final_test_path`
- `final_reason`
- `strict_verifier_decision`
- `dual_version_result`
- `final_oracle_risk`
- `final_surrogate_risk`

### 下一步

进入 direct evaluation 或 formal F2P evaluation。

## Step 18：direct evaluation

### 输入

- generation outputs。
- issue dataset。
- repo root。
- generated final tests。

### 处理

direct evaluation 读取生成结果，准备 worktree，把 generated test 写入对应 repo path，然后按 test command 执行。它可用于快速检查生成结果和 runner 信息。

### 输出

- `evaluation/metrics.json`
- `evaluation/merged_results.json`
- logs。

### 涉及文件

- `evaluation/direct_eval.py`
- `direct_eval.py`
- `scripts/run_evaluate.sh`

### 关键字段

- `status`
- `buggy`
- `fixed`
- `direct_test_repo_path`
- `test_command`
- `runner_parity`

### 下一步

进入 formal F2P evaluation。

## Step 19：formal F2P evaluation

### 输入

- `generation/<instance_id>/final_test.py`
- full issue dataset。
- true patch from dataset。
- repo worktree。
- `HostContext` / `summary.json` 中的 placement 与 command hints。

### 处理

formal eval 在 buggy commit 上运行 generated test，再应用 true patch 并运行 fixed version。F2P 定义保持不变：buggy fail 且 fixed pass 才是 `F2P_SUCCESS`。patch apply 失败、setup 失败、buggy pass、fixed fail 都会分类为非 success。

### 输出

```text
evaluation/formal_eval_summary.json
evaluation/formal/metrics.json
evaluation/formal/merged_results.json
formal_eval.done
```

### 涉及文件

- `evaluation/direct_eval.py`
- `scripts/run_formal_eval_after_generation.py`
- `scripts/run_formal_eval.sh`

### 关键字段

- `F2P_SUCCESS`
- `BUGGY_PASS`
- `FIXED_FAIL`
- `BUGGY_SETUP_ERROR`
- `ERROR`
- `patch_apply`
- `runner_parity`

### 下一步

可选 export，并用 `show_latest_results.sh` 查看。

## Step 20：scripts 运行入口

### 输入

- 环境变量和脚本参数。
- 默认 dataset/retrieval/repo paths。

### 处理

`scripts/` 提供统一入口，负责创建 run dir、写 `run_config.json`、保存 command、tee logs，并把输出放到 `results/`。

### 输出

- `results/runs/<run_name>/`
- `results/issue_rewrite/<timestamp>/`
- `results/smoke/<timestamp>/`
- logs、done markers、evaluation artifacts。

### 涉及文件

- `scripts/run_issue_rewrite.sh`
- `scripts/run_generate.sh`
- `scripts/run_evaluate.sh`
- `scripts/run_formal_eval.sh`
- `scripts/run_full_pipeline.sh`
- `scripts/run_smoke.sh`
- `scripts/show_latest_results.sh`
- `scripts/clean_old_results.sh`

### 关键字段

- `RUN_NAME`
- `RUN_DIR`
- `WORKERS`
- `SEED_WORKERS`
- `MODEL`
- `TEMPERATURE`
- `TIMEOUT`

### 下一步

用于日常运行、检查和清理。

## 入口脚本表

| 脚本 | 用途 | 输入 | 输出 | 是否调用 LLM | 是否执行测试 |
|---|---|---|---|---|---|
| `scripts/run_issue_rewrite.sh` | 单独跑 issue rewrite | issue dataset、model、workers | `results/issue_rewrite/<timestamp>/` | 是 | 否 |
| `scripts/run_generate.sh` | 跑 BRT generation | dataset、retrieval、repo root、model、temperature | `results/runs/<run_name>/generation/` | 是 | 是，generation 内会执行 candidate |
| `scripts/run_evaluate.sh` | 跑 direct evaluation | `RUN_DIR` | `results/runs/<run_name>/evaluation/` | 否 | 是 |
| `scripts/run_formal_eval.sh` | 跑 formal F2P eval | `RUN_DIR`、dataset、repo root | `results/runs/<run_name>/evaluation/formal/` | 否 | 是 |
| `scripts/run_full_pipeline.sh` | generation + evaluation + formal eval + export | dataset、retrieval、repo root、run env | `results/runs/<run_name>/` | 是 | 是 |
| `scripts/run_smoke.sh` | 小规模 smoke | limit 或 smoke subset | `results/smoke/<timestamp>/` 或 run dir | 是 | 是 |
| `scripts/show_latest_results.sh` | 查看最近结果 | `results/runs/` | 终端表格 | 否 | 否 |
| `scripts/clean_old_results.sh` | 清理旧结果，默认 dry-run | results dirs | manifest / dry-run 输出 | 否 | 否 |

## 关键数据结构表

| 数据结构 | 来源文件 | 主要字段 | 谁写入 | 谁读取 |
|---|---|---|---|---|
| `InstanceContext` | `core/schema.py` | `instance_id`, `repo`, `base_commit`, `problem_statement`, `repo_root` | `run.py` | retrieval、generation、execution、evaluation |
| `BehaviorTarget` | `core/schema.py` | `summary`, `trigger_condition`, `expected_behavior`, `error_symptom`, `target_apis`, `mutation_hints`, `assertion_hints`, `setup_hints`, `evidence_spans` | `issue/issue_rewriter.py`, `execution/feedback.py` cache loader | generation、mutation、verifier、risk checks |
| `RetrievedCode` | `core/schema.py` | `file`, `code`, `score` | retrieval loading | generation、surrogate、risk checks |
| `RetrievedTest` | `core/schema.py` | `file`, `name`, `code`, `score` | retrieval loading | seed ranking、HostContext recovery |
| `HostContext` | `core/schema.py` | `host_file`, `host_class`, `imports`, `fixtures`, `candidate_repo_path`, `test_command`, `runner_kind`, `selector` | `context/host_context.py` | generation、execution、formal eval parity |
| `ProtocolRecovery` | `core/schema.py` | `recovered`, `setup_steps`, `call_sequence`, `assertion_style`, `protocol_risks` | `execution/feedback.py` | generation、repair |
| `MutationPlan` | `core/schema.py` | `operators`, `rationale`, `target_api`, `risk`, `expected_failure` | mutation planning in `execution/feedback.py` | generation |
| `CandidateTest` | `core/schema.py` | `code`, `candidate_repo_path`, `test_command`, `selector`, `round` | `generation/generator.py`, repair loop | execution、selection |
| `ExecutionResult` | `core/schema.py` | `command`, `cwd`, `returncode`, `stdout`, `stderr`, `duration`, `timeout`, `status`, `error_reason` | `execution/executor.py` | verifier、repair、selection |
| `StrictVerifierResult` | `core/schema.py` | `decision`, `target_hit`, `issue_aligned`, `failure_class`, `reason`, `suggestions` | `validation/strict_semantic_verifier.py` | repair loop、selection |
| `DualVersionResult` | `core/schema.py` | `mode`, `status`, `buggy_execution`, `patched_execution`, `surrogate_patch`, `attempts` | `execution/dual_version.py`, `execution/feedback.py` | scoring、summary |
| `FinalResult` | `core/schema.py` | `instance_id`, `status`, `final_test_path`, `dual_version_result`, `selected_seed_index`, `seed_attempts_summary`, `final_oracle_risk` | `execution/feedback.py` | evaluation、export、analysis |
| `EvaluationResult` | `evaluation/direct_eval.py` JSON dict | `status`, `success`, `buggy`, `fixed`, `patch_apply`, `runner_parity`, `test_command` | `evaluation/direct_eval.py` | metrics、formal summary、analysis |
