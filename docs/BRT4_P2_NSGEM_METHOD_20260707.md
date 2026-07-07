# BRT4-P2: NS-GEM-BRT

英文全称：Neuro-Symbolic Gate-guided Evolutionary Mutation for Bug Reproduction Test Generation

中文名：神经符号门控引导的演化变异缺陷复现测试生成方法

## 1. 为什么 P1 Conservative 不如 P0

P1 conservative seed retention 将跨 seed 选择变得更保守，目标是减少 seed 误切。但 full276 formal eval 为 131/276，低于 P0 的 136/276。主要现象是 `FIXED_FAIL` 从 P0 的 113 增加到 P1 的 123，而 `BUGGY_PASS` 仍为 17。说明 P1 的保守选择没有明显提升 trigger 命中，也可能保留了 oracle/surrogate 不够稳的候选。

因此 P2 不继续在 P1 上堆规则，而是回到 P0 adaptive seed retry 和 risk-aware selection 的基础上，引入 Gemini 风格的程序分析约束和演化变异搜索。

## 2. 为什么 P2 吸收 Gemini 核心

P2 的核心不是简单调 prompt，而是把 BRT 生成从“LLM 直接写/修测试”推进到“程序分析约束下的测试侧行为变异搜索”：

- LLM 只做局部变异参数求解。
- AST skeleton 限定可变异区域。
- Issue Gate 固定缺陷触发门。
- Dynamic Trace 和 Fitness 用执行信号评价候选。
- Probe-first Oracle 优先先观测再断言。
- Delta Minimization 记录和压缩高风险变异。

## 3. 术语解释

- Neuro-Symbolic 神经符号：Neuro 指 LLM 理解 issue 并求解变异参数；Symbolic 指 AST、轻量数据流、执行轨迹和确定性 scoring。
- Issue Gate 缺陷触发门：从 issue 和 retrieval 中抽取必须触发的 API、状态、观测行为和禁止失败类型。
- AST 抽象语法树：Python 代码的结构化语法表示，用于识别调用、断言、赋值和可安全变异节点。
- DFG 数据流图：变量定义到使用的依赖关系。P2 MVP 只做 1-3 层轻量 def-use chain，不实现完整 DFG。
- Skeletonization 骨架化：把 seed test 分成 setup/action/oracle/target call/protected nodes。
- Mutation Operator 变异算子：形式化的测试侧变异动作，例如边界变异、参数注入、调用链变异。
- Parameter Solver 参数求解器：LLM 只输出 operator 参数 JSON，而不是完整测试文件。
- AST Transformation AST 变换：对 candidate 做局部、安全、可回退的 AST 级修改。
- Dynamic Trace 动态执行轨迹：用执行日志、traceback、verifier 信号和 candidate code 形成轻量 profile。
- Fitness 适应度分数：根据 target hit、observable channel、issue-aligned failure、setup/unrelated failure 等计算 ranking bonus。
- Evolution Loop 演化搜索循环：生成、执行、反馈、再变异、再评分的闭环。
- Probe-first Oracle 先观测后断言：target 命中但 oracle 不稳时优先调用已有 observation oracle。
- Delta Minimization 变异最小化：对高风险或多操作候选记录最小化信息，MVP 做保守元数据和轻量安全检查。

## 4. P2 端到端流程

输入：

- issue dataset
- cached BehaviorTarget
- code/test retrieval
- seed tests
- repository worktree

流程：

1. P0 adaptive seed retry。
2. HostContext recovery。
3. ProtocolRecovery。
4. IssueGate build。
5. TestSkeleton build。
6. OperatorApplicability build。
7. NS-GEM operator parameter solving。
8. Existing MutationPlan fallback/compatibility。
9. Candidate generation。
10. Safe AST Transformation attempt。
11. Execution。
12. Strict verifier。
13. Dynamic Trace + Fitness。
14. Observation oracle when repair_oracle path is needed.
15. Surrogate validation。
16. Oracle/surrogate risk scoring。
17. Delta minimization metadata。
18. Candidate ranking with `score_after_ns_gem`。
19. One final `final_test.py`。
20. Formal F2P evaluation unchanged.

输出：

- `issue_gate.json`
- `test_skeleton.json`
- `operator_applicability.json`
- `ns_gem_operator_plan.json`
- `ast_transform_result.json`
- `runtime_trace_profile_round_*.json`
- `delta_minimization.json`
- `summary.json` with `method_version=ns_gem_brt_p2`
- `candidate_ranking.json` with NS-GEM scores

## 5. 不使用真实 patch

P2 generation 不使用：

- golden_patch
- gold_patch
- test_patch
- golden_test
- FAIL_TO_PASS
- PASS_TO_PASS

formal eval 仍在 generation 结束后使用 true patch 判断 F2P，但这个信息不进入 generation。

## 6. 运行方式

```bash
RUN_NAME=run_p2_nsgem_full276_YYYYMMDD_HHMMSS \
MODEL=deepseek-v3 \
TEMPERATURE=0.1 \
WORKERS=6 \
SEED_WORKERS=2 \
bash scripts/run_full_pipeline.sh
```

查看 formal metrics：

```bash
cat results/runs/<RUN_NAME>/evaluation/formal/metrics.json
```

## 7. 可做消融

- 关闭 AST transform，只保留 IssueGate + fitness。
- 关闭 fitness bonus，只记录 trace。
- 只使用 IssueGate + skeleton，不调用 NS-GEM planner。
- 保留 P0 scoring，对比 `score_after_ns_gem`。
- 只启用 selected operators 子集，例如 `Mut_InjectArg` / `Mut_Boundary`。
