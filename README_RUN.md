# BRT4 运行说明

## 1. 项目运行总览

完整实验分成两个隔离阶段：

```text
输入实例列表
→ issue 增强与检索上下文读取
→ 测试协议恢复和 BRT 生成
→ buggy 版本执行反馈
→ verifier 判断
→ setup / trigger / oracle 修复
→ surrogate patch 验证与最终测试选择
→ generation.done
→ 真实 patch 双版本正式 F2P 评测
→ evaluation.done
→ JSON 汇总导出
```

生成阶段不读取真实 patch。只有正式评测阶段会从 SWE-bench Lite 数据读取真实 patch，并在 buggy 与 patched 两侧运行同一个最终 BRT。

## 2. 一键完整运行

推荐在 tmux 中运行：

```bash
cd /root/Baxxhy/BugReproduce/brt4
tmux new -s brt4_full
bash scripts/run_latest_full_pipeline.sh
```

显式配置示例：

```bash
bash scripts/run_latest_full_pipeline.sh \
  --instances-file /root/Baxxhy/BugReproduce/brt2/data/issues/swt276_issues.json \
  --model deepseek-v3 \
  --temperature 0.1 \
  --timeout 1800 \
  --max-workers 6
```

默认会依次执行 generation、formal evaluation 和 export。当前本地 API 轮转池配置在 `api_pool.py`，共 7 个账户；manifest 和日志只记录账户数量，不记录 key。

## 3. Smoke 测试

```bash
bash scripts/run_latest_full_pipeline.sh --smoke --smoke-n 5
```

Smoke 走完整两阶段流程，用来检查输入解析、API、Conda、项目环境、正式评测和 JSON 导出。输出仍在独立的 `results/runs/run_*_smoke/` 下。

只检查五条生成：

```bash
bash scripts/run_latest_full_pipeline.sh --generation-only --smoke --smoke-n 5
```

## 4. 只生成，不正式评测

```bash
bash scripts/run_latest_full_pipeline.sh --generation-only
```

生成结果位于本次 run 的 `generation/<instance_id>/`，阶段日志为 `logs/generation.log`，成功结束后创建 `generation.done`。该模式仍执行 buggy 反馈和 surrogate patch 验证，但不读取真实 patch。

## 5. 只评测已有生成结果

```bash
bash scripts/run_latest_full_pipeline.sh \
  --evaluation-only \
  --run-dir /root/Baxxhy/BugReproduce/brt4/results/runs/run_YYYYMMDD_HHMMSS
```

适用于 generation 已完成但 formal F2P 未启动或中断的情况。评测结果写入 `evaluation/`，日志写入 `logs/evaluation.log`。正式评测始终启用 resume、SWE-bench Lite patch 和生成阶段 worktree。

## 6. 只重新导出 JSON

```bash
bash scripts/run_latest_full_pipeline.sh \
  --export-only \
  --run-dir /root/Baxxhy/BugReproduce/brt4/results/runs/run_YYYYMMDD_HHMMSS
```

这不会重新生成或评测，适用于导出缺失、字段更新或坏 JSON 修复后的重新汇总。

## 7. 中断后继续

```bash
bash scripts/run_latest_full_pipeline.sh \
  --run-dir /root/Baxxhy/BugReproduce/brt4/results/runs/run_YYYYMMDD_HHMMSS \
  --resume
```

生成阶段依据每个 instance 的 `summary.json` 跳过已完成项；正式评测依据 `evaluation/worker_*/results.json` 跳过已完成项。不要更换 run 目录，否则无法复用 checkpoint。

如果仅正式评测中断，优先使用 `--evaluation-only` 指向原 run；如果只是导出失败，使用 `--export-only`。

## 8. tmux 常用操作

```bash
tmux ls
tmux attach -t brt4_full
```

在 tmux 内按 `Ctrl-b d` 可退出会话但不中断任务。创建带时间戳的会话：

```bash
tmux new -s brt4_rerun_$(date +%Y%m%d_%H%M%S)
```

## 9. 日志和监控

直接看日志：

```bash
tail -f results/runs/run_YYYYMMDD_HHMMSS/logs/generation.log
tail -f results/runs/run_YYYYMMDD_HHMMSS/logs/evaluation.log
tail -f results/runs/run_YYYYMMDD_HHMMSS/logs/export.log
```

只打印一次进度：

```bash
python scripts/monitor_run.py \
  --run-dir results/runs/run_YYYYMMDD_HHMMSS \
  --once
```

每 60 秒持续监控并保存监控日志：

```bash
python scripts/monitor_run.py \
  --run-dir results/runs/run_YYYYMMDD_HHMMSS \
  --interval 60 \
  --log-path results/runs/run_YYYYMMDD_HHMMSS/logs/monitor.log
```

`generation.log` 记录各实例生成和反馈；`evaluation.log` 记录每个 worker 的正式分类；`export.log` 记录汇总计数和导出异常。

## 10. 参数说明

| 参数名 | 默认值 | 作用 | 示例 |
|---|---:|---|---|
| `--instances-file` | 正式 276 JSON | JSON/JSONL 输入，或每行一个 instance ID 的 TXT | `--instances-file data/ids.txt` |
| `--model` | `deepseek-v3` | 生成模型名 | `--model deepseek-v3` |
| `--max-workers` | `6` | 生成和正式评测并发数 | `--max-workers 6` |
| `--run-id` | 自动时间戳 | 指定 `results/runs/` 下的目录名 | `--run-id run_paper_v3` |
| `--run-dir` | 空 | 指定完整 run 目录，优先于 run-id | `--run-dir results/runs/run_x` |
| `--generation-only` | 关闭 | 只生成和导出，不跑正式评测 | `--generation-only` |
| `--evaluation-only` | 关闭 | 只对已有生成结果正式评测并导出 | `--evaluation-only` |
| `--export-only` | 关闭 | 只重建四类 JSON 导出 | `--export-only` |
| `--resume` | 关闭 | 复用原 run 的 summary/checkpoint/worker 结果 | `--resume` |
| `--smoke` | 关闭 | 只取解析后输入的前 N 条 | `--smoke` |
| `--smoke-n` | `5` | Smoke 实例数 | `--smoke-n 10` |
| `--temperature` | `0.1` | DeepSeek 生成温度 | `--temperature 0.1` |
| `--timeout` | `1800` | 单次 setup/test 超时秒数 | `--timeout 1800` |
| `--help` | - | 打印帮助 | `--help` |

`--generation-only`、`--evaluation-only`、`--export-only` 互斥。已有 run 目录只有在 `--resume`、evaluation-only 或 export-only 模式下允许复用。

TXT 输入会从正式 276 JSON 中按 ID 筛选，并在 run 根目录生成 `resolved_instances.json`。JSON/JSONL 输入则直接规范化后写入该文件。

## 11. 输出文件说明

```text
results/runs/run_YYYYMMDD_HHMMSS/
  generation/              每个 instance 的完整生成记录与 worktree
  evaluation/              worker 分片、merged_results.json、metrics.json
  logs/                    generation/evaluation/export/monitor 日志及命令
  checkpoints/             导出阶段复制的最终选中 checkpoint
  exports/
    all_outputs.json       完整 run 元数据和逐实例记录
    all_outputs.jsonl      每行一个实例
    all_tests_only.json    instance、完整测试、formal 状态
    final_summary.json     成功数、成功率和失败统计
  resolved_instances.json 本次实际输入
  run_config.json          机器可读配置，不含 API key
  manifest.txt             人类可读运行配置
  generation.done
  evaluation.done
  export.done
  SELF_CHECK_RUN.md
```

新 run 的 worktree 在正式评测完成后也不会自动删除，以便中断恢复；需要释放空间时应在结果确认和清单记录后手动清理。

## 12. `all_outputs.json` 字段

- `instance_id`：SWE-bench 实例 ID。
- `generated` / `evaluated`：是否找到最终测试、是否找到正式评测记录。
- `selected_test.test_content`：完整最终 Python 测试内容；未生成时为 `null`。
- `selected_test.test_file_path`：评测时的同级新测试路径或生成结果路径。
- `generation.status`：生成阶段最终状态。
- `generation.verifier_decision`：严格 verifier 最终决定。
- `generation.buggy_execution_status`：生成阶段 buggy 执行分类。
- `formal_evaluation.status`：`F2P_SUCCESS`、`BUGGY_PASS`、`FIXED_FAIL` 或错误状态。
- `formal_evaluation.is_f2p_success`：buggy fail 且 patched pass 时为 `true`。
- `formal_evaluation.failure_category`：统一失败分类。
- `paths`：对应 summary、正式记录、测试和日志的 run 内相对路径。
- `error`：生成或正式评测的显式错误；没有则为 `null`。

## 13. 状态码含义

生成阶段状态码出现在 `generation.status` 和每个实例的 `generation/<instance_id>/summary.json` 中：

| 状态码 | 含义 | 是否等于真实 F2P |
|---|---|---|
| `SURROGATE_F2P_SUCCESS` | 生成阶段中，BRT 在 buggy 上失败，并且 BRT4 自己生成的临时代码修补能让它通过。说明这个测试在生成阶段看起来很像有效复现。 | 不是。它没有使用真实 patch，只是生成阶段的代理验证。 |
| `ISSUE_ALIGNED_FAIL` | BRT 在 buggy 上失败，严格 verifier 判断失败原因和 issue 语义对齐，但代理修补没有验证成功或未形成通过。 | 不是。还需要正式 patch 评测确认。 |
| `UNRELATED_FAIL` | 测试失败了，但失败原因不像 issue 本身，可能是 setup、路径、断言或无关异常。 | 不是。通常正式评测会是 `FIXED_FAIL` 或环境失败。 |
| `PASS` | 测试在 buggy 上通过，说明没有触发缺陷。 | 不是。正式评测通常是 `BUGGY_PASS`。 |
| `ENV_UNRESOLVED` | 环境资格循环 3 轮后仍没有让测试正常收集、导入或执行。 | 不是。正式评测多半是环境类失败。 |
| `ERROR` | 生成流程本身异常，例如 API、解析、路径或上下文过长等系统错误。 | 不是。需要看实例日志并 resume。 |

正式评测状态码出现在 `formal_evaluation.status`、`evaluation/merged_results.json` 和 `exports/final_summary.json` 中：

| 状态码 | 含义 | 是否计入论文 F2P 成功 |
|---|---|---|
| `F2P_SUCCESS` | 同一个最终 BRT 在 buggy 上失败，在真实 patch 后通过。 | 是。 |
| `BUGGY_PASS` | BRT 在 buggy 上就通过了，没有复现缺陷。 | 否。 |
| `FIXED_FAIL` | BRT 在 buggy 上失败，但真实 patch 后仍失败。可能是断言太强、失败原因无关、测试依赖了错误环境，或真实 patch 不会改变该行为。 | 否。 |
| `BUGGY_SETUP_ERROR` | buggy 侧测试没有正常跑起来，通常是导入、fixture、Django 配置、编译扩展、pytest 收集等问题。 | 否。导出时统一映射到 `SETUP_ERROR`。 |
| `PATCH_APPLY_ERROR` | 正式评测阶段应用真实 patch 失败，或 patch 后仓库状态无法进入 patched 执行。 | 否。 |
| `TIMEOUT` | 执行超时。 | 否。 |
| `ERROR` | formal wrapper 或 direct eval 的非预期错误。 | 否。 |

本次全量 run `run_20260624_013010` 的正式统计：

```text
F2P_SUCCESS      75
FIXED_FAIL      103
BUGGY_SETUP_ERROR 48
BUGGY_PASS       39
PATCH_APPLY_ERROR 11
总数            276
F2P             75/276 = 27.1739%
```

导出文件里 `failure_category` 是给后续分析用的归一化分类，常见映射是：

```text
BUGGY_SETUP_ERROR -> SETUP_ERROR
BUGGY_PASS        -> BUGGY_PASS
FIXED_FAIL        -> FIXED_FAIL
PATCH_APPLY_ERROR -> PATCH_APPLY_ERROR
```

## 14. 常见错误

- `ImportError` / `ModuleNotFoundError`：先看实例 `summary.json`、生成日志和 Conda 环境是否指向当前 worktree；修复通用环境后用同一 run-dir `--resume`。
- 输出目录错误：确认使用统一脚本；新结果只能写入 `results/runs/`。不要直接调用旧 legacy 脚本。
- API key / API 失败或 429：检查 `api_pool.py` 的账户数量和服务状态，不要在日志打印 key；等待限流恢复后 `--resume`。
- Conda 环境错误：检查 `conda env list`、实例环境名和 generation 日志。共享环境由 BRT4 锁串行，不应手工并行修改同一环境。
- pytest collection error：查看实例的 `host_context.json`、最终测试位置和执行命令；从原 run 恢复以保留环境反馈记录。
- formal F2P 没跑完：用 `--evaluation-only --run-dir <原目录>`，worker 结果会继续累积。
- JSON 导出失败：修复导出器后用 `--export-only`，不需要重跑生成和评测。
- tmux 断开：用 `tmux ls` 找会话，再 `tmux attach -t <名称>`；SSH 断开通常不会停止 tmux 内任务。
