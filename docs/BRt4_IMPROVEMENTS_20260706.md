# BRT4 20260706 改进总结

## 1. 本次版本目标

这版的目标是对 brt4 做小而明确的高收益改进，而不是改变 BRT 的基本评测定义或引入新的大方法。

主要问题来自三个风险点：

- 当前流程整体仍是 one issue -> one final test，原先过度依赖单个 seed test，seed 选错时容易生成不到真实缺陷触发路径。
- `BUGGY_PASS` 往往说明 trigger 不足，生成测试没有在 buggy version 上触发失败。
- `FIXED_FAIL` 往往说明 oracle 过强、surrogate patch 和 true patch 不一致，或者 generation runner 与 formal eval runner/placement 不一致。

本次版本只做 P0 级别的小改：

- 不做 all-seed 全量扩展。
- 不新增多 aspect、多 cause、issue morph 或 prompt mask。
- 不大改 prompt。
- 不改变 formal F2P evaluation 定义。
- 不让 golden patch/test 进入 generation。

## 2. 核心改进点

### 2.1 Adaptive Seed Retry

原先 brt4 不是 all-seed，主流程围绕一个 seed/candidate 链推进。启用 protocol recovery 时虽然存在 seed fallback，但最终仍主要受一个 seed 的 scaffold、trigger 和 oracle 影响。

当前版本在 `execution/feedback.py` 中加入 adaptive top-3 seed retry：

- 最多尝试 rank 后前 3 个 seed。
- 不是无条件跑 3 个 seed；只有当前 seed 明显失败时才尝试下一个 seed。
- 每个 seed 内仍运行原有流程：HostContext recovery、ProtocolRecovery、MutationPlan、generation、execution、strict verifier、repair、observation oracle、surrogate validation。
- 外部契约仍保持 one issue -> one `final_test.py`，不把 num_candidates 扩展为多输出池。

会考虑切 seed 的情况包括：

- 当前 seed 最终是 `PASS` 或 `BUGGY_PASS`，说明没有触发 buggy failure。
- 当前 seed 是 `UNRELATED_FAIL`，说明失败和 issue 不对齐。
- setup/collect/syntax/import 等 scaffold 明显不稳定，并且还有下一个 seed。
- strict verifier 或 checkpoint ranking 显示没有 verifier accept、surrogate success 或可执行的 issue-aligned buggy fail。

不会立即切 seed的情况包括：

- 已经得到 `SURROGATE_F2P_SUCCESS`。
- verifier accept 且 issue-aligned。
- observation oracle 已经 rebind 且 buggy fail。
- 当前失败更像 oracle 问题，优先走 oracle repair。

每个 seed 的产物保存到：

```text
generation/<instance_id>/seed_candidates/seed_<idx>/
```

顶层实例目录仍输出：

```text
generation/<instance_id>/final_test.py
generation/<instance_id>/summary.json
generation/<instance_id>/seed_attempts_summary.json
generation/<instance_id>/selected_seed_summary.json
generation/<instance_id>/candidate_ranking.json
```

跨 seed 选择使用原有 checkpoint score 的风险感知版本：

- `SURROGATE_F2P_SUCCESS` 最高，但会受到 oracle/surrogate risk 软扣分。
- verifier accept + buggy fail 次之。
- executable buggy fail 次之。
- `PASS` / `BUGGY_PASS` 不能压过任何 buggy fail。
- setup/collect/syntax/import 类失败最低。
- 同分优先更早 seed、更早 checkpoint。

新增或强化的字段包括：

- `seed_mode`
- `selected_seed_index`
- `selected_seed_file`
- `selected_seed_name`
- `seed_attempts_count`
- `seed_attempts_summary`
- `seed_switch_reasons`
- `selected_seed_reason`

### 2.2 Oracle / Surrogate Soft Risk Guard

当前版本新增 `validation/oracle_risk.py`，在 candidate scoring 中加入 oracle 和 surrogate 的轻量风险检查。

这不是 hard reject。高风险 candidate 仍可输出，尤其在只有一个可用 candidate 时不会被直接删除。风险只用于记录和软扣分，避免 surrogate success 被完全盲信。

oracle 高风险信号包括：

- 断言完整 repr/str/SQL 长字符串。
- 断言私有属性或内部 cache/internal state/ordering。
- 断言 exact warning message，但 issue 没明确要求 exact warning。
- 断言过长 exception message。
- 断言 mock 调用次数或内部函数调用次数。
- `assert True` 或恒真检查。
- `pytest.raises(Exception)` / `pytest.raises(BaseException)`。
- broad try/except 吞异常。
- issue 只要求 not crash，但测试要求具体返回值或完整字符串。

surrogate risk 的轻量判断包括：

- surrogate patched execution pass 但 oracle risk high。
- patch touched paths 与 retrieved/effective source context 不一致。
- surrogate patch 只满足过窄内部细节时提高风险。

新增字段包括：

- `oracle_risk`
- `surrogate_risk`
- `selector_score_before_risk`
- `selector_score_after_risk`
- `selector_penalty_reasons`
- `final_oracle_risk`
- `final_surrogate_risk`

### 2.3 Runner Parity Check

generation 阶段能运行并不等于 formal eval 阶段一定以同样 placement 和 runner 执行。当前版本在 `evaluation/direct_eval.py` 中记录 runner parity 信息，用于比较 generation 与 formal eval 的测试放置和命令。

记录字段包括：

- `generation_candidate_repo_path`
- `formal_direct_test_repo_path`
- `generation_command`
- `formal_command`
- `generation_selector`
- `formal_selector`
- `host_file`
- `same_dir`
- `same_selector`
- `warnings`

runner parity 只记录一致性和 warning，不改变 F2P 定义。formal eval 的 `F2P_SUCCESS` 仍然由 true patch 下的 buggy fail + fixed pass 决定。

### 2.4 工程结构整理

这版也保留了前期工程结构整理：

- prompt 从 Python 中拆到 `prompts/`，通过 prompt loader 读取。
- 代码按功能整理到 `core/`、`llm/`、`issue/`、`retrieval/`、`context/`、`generation/`、`mutation/`、`execution/`、`validation/`、`evaluation/`、`io/`、`runtime/`、`pipeline/`。
- `scripts/` 下提供统一入口脚本。
- 新运行结果统一写入 `results/runs/<run_name>/`。
- 根目录保留 `run.py`、`run_issue_rewrite.py`、`direct_eval.py` 等兼容入口。
- 旧路径需要兼容的文件保持薄 wrapper，不复制方法逻辑。

## 3. 实验结果

最新 run：

```text
/root/Baxxhy/BugReproduce/brt4/results/runs/run_p0_adaptive_seed_full276_20260706_013545
```

formal eval 结果：

| metric | value |
|---|---:|
| total_instances | 276 |
| F2P_SUCCESS | 136 |
| F2P@1 | 49.28% |
| FIXED_FAIL | 113 |
| BUGGY_PASS | 17 |
| BUGGY_SETUP_ERROR | 5 |
| ERROR | 5 |

对比历史 best125：

- only-success vs best125 = 32
- lost-success vs best125 = 21
- only-success 中有 adaptive seed retry 或多 seed attempt 证据 = 15/32
- 未发现 patch leakage 进入 final_test。

## 4. 可信性检查

只读验收结果显示：

- formal eval total = 276。
- `merged_results.json` 有 276 个唯一 `instance_id`。
- 没有 `MISSING_GENERATED_TEST`。
- 276/276 均有 `final_test.py`。
- `F2P_SUCCESS` 满足 buggy fail + fixed pass。
- patch apply 异常不会被算成 success。
- 未发现 golden/test patch 进入 `final_test.py`。
- `export.done` 缺失，但 formal eval 的 `formal_eval_summary.json`、`metrics.json`、`merged_results.json` 完整存在。

## 5. 当前仍然存在的问题

- 相对 best125 仍有 21 个 lost-success。
- adaptive seed retry 可能误切，尤其当当前 seed 的 failure 被错误归类为 unrelated 或 scaffold unstable 时。
- oracle risk 只是 soft guard，不会保证完全避免过强 oracle。
- surrogate patch 与 true patch 仍可能不一致；surrogate success 只是 generation 阶段的选择信号，不等于 formal F2P。
- runner parity 目前主要是记录和 warning，不直接修复所有 placement/runner 问题。
- 建议再跑一次同参数 276 复现实验，确认 136 的稳定性。
