# BRT4 Self Check

时间：2026-06-18

## 1. 已完成的主要文件

```text
/root/Baxxhy/BugReproduce/brt4/__init__.py
/root/Baxxhy/BugReproduce/brt4/config.py
/root/Baxxhy/BugReproduce/brt4/schema.py
/root/Baxxhy/BugReproduce/brt4/utils.py
/root/Baxxhy/BugReproduce/brt4/io_utils.py
/root/Baxxhy/BugReproduce/brt4/llm_client.py
/root/Baxxhy/BugReproduce/brt4/prompts.py
/root/Baxxhy/BugReproduce/brt4/issue_rewriter.py
/root/Baxxhy/BugReproduce/brt4/host_context.py
/root/Baxxhy/BugReproduce/brt4/generator.py
/root/Baxxhy/BugReproduce/brt4/oracle.py
/root/Baxxhy/BugReproduce/brt4/executor.py
/root/Baxxhy/BugReproduce/brt4/verifier.py
/root/Baxxhy/BugReproduce/brt4/feedback.py
/root/Baxxhy/BugReproduce/brt4/dual_version.py
/root/Baxxhy/BugReproduce/brt4/patch_utils.py
/root/Baxxhy/BugReproduce/brt4/run_issue_rewrite.py
/root/Baxxhy/BugReproduce/brt4/run.py
/root/Baxxhy/BugReproduce/brt4/README.md
/root/Baxxhy/BugReproduce/brt4/SELF_CHECK.md
```

目录中已有 `retrieval_results/` 数据和 `swt.txt` 保留未动。

## 2. 目录结构检查

命令：

```bash
find /root/Baxxhy/BugReproduce/brt4 -maxdepth 2 -type f | sort
```

结果：命令已执行。主项目文件均存在；由于目录下已有 retrieval JSON/graph 数据，完整输出较长。

## 3. Python 语法检查

命令：

```bash
cd /root/Baxxhy/BugReproduce
python -m compileall brt4
```

结果：通过，退出码 0。

## 4. 禁止依赖检查

命令：

```bash
grep -R "import .*iCoRe\|from .*iCoRe\|import .*Echo\|from .*Echo\|iCoRe-base-ise" -n /root/Baxxhy/BugReproduce/brt4 || true
```

结果：

- Python 代码无命中。
- README 和 SELF_CHECK 中出现 iCoRe/Echo/iCoRe-base-ise 仅作为说明文字或检查命令文本，不是运行时 import 或内部路径依赖。

## 5. CLI help 检查

命令：

```bash
cd /root/Baxxhy/BugReproduce
python -m brt4.run_issue_rewrite --help
python -m brt4.run --help
```

结果：通过，两个命令均能输出 help，退出码 0。

## 6. Fake 输入测试

命令：使用临时 JSONL issue、源码检索 JSON、测试检索 JSON，并用 MockLLM 返回固定 JSON，不调用真实 API，直接调用：

```python
from brt4.io_utils import load_issue_data, build_instance_context
from brt4.issue_rewriter import rewrite_issue
```

结果：

```text
FAKE_TEST_OK /tmp/tmpqqklhh8z/out/behavior_target.json
```

验证点：

- `io_utils` 可以读取 jsonl issue。
- `io_utils` 可以按 instance_id 对齐源码和测试检索 JSON。
- `rewrite_issue` 可以保存 `prompt.txt`、`response.txt`、`behavior_target.json`、`meta.json`。
- 全部输出使用 UTF-8。

## 7. 默认参数检查

命令：解析 `brt4.run` 默认参数。

结果：

```text
DEFAULT_MAX_WORKERS 6
DEFAULT_MAX_FEEDBACK_ROUNDS 3
num_candidates 1
validation_mode buggy_only
max_workers 6
max_feedback_rounds 3
```

## 8. 异常处理检查

结果：

- `run_issue_rewrite.py` 中每个 instance 独立 try/except，单个失败写入 summary，不中断批量。
- `run.py` 中每个 instance 独立 try/except，单个失败写入对应 `summary.json`，不影响其他 future。
- `feedback.py` 中 instance 内异常会写入该 instance 的 `summary.json`。

## 9. 已完成能力

- 读取多格式 issue 数据：list、dict、jsonl。
- 读取并去重源码/测试检索结果。
- DeepSeek/OpenAI-compatible client，默认 `deepseek-v3`，最多重试 3 次。
- 第一阶段 issue 增强，保存 prompt/response/meta/behavior。
- HostContext 恢复：imports、pytestmark、class、fixtures、setup、相邻测试、seed nodeid。
- conda 执行器：默认 `conda run -n <env> bash -lc ...`，支持 `--no_conda`。
- 1 个候选 BRT 生成。
- buggy-only 执行反馈，最多 3 轮。
- observation probe 和 assert synthesis。
- 可选 patched_repo / patch_file 最终验证。
- 输出目录结构和中间文件保存。

## 10. 暂时 stub 或保守实现

- `surrogate_patch` 是安全 stub：不会调用 Echo，也不会生成真实复杂补丁。
- 生成测试默认写入 buggy repo 中相似测试同目录的临时 `test_brt_<instance>.py`，避免改原测试文件。
- HostContext 使用 Python AST 做静态恢复，复杂动态 pytest fixture 或非标准测试生成可能只能部分恢复。
- conda 环境名默认由用户传入；当前版本不自动从 SWT-bench 元数据推断每个 instance 的 conda env。

## 11. 第一阶段运行方式

```bash
cd /root/Baxxhy/BugReproduce

python -m brt4.run_issue_rewrite \
  --instances_path <原始issue数据文件> \
  --code_retrieval_path <iCoRe DeepSeek-V3源码检索结果json> \
  --test_retrieval_path <iCoRe DeepSeek-V3测试检索结果json> \
  --output_dir /root/Baxxhy/BugReproduce/brt4/outputs_issue_rewrite \
  --model deepseek-v3 \
  --max_workers 6 \
  --top_code 6 \
  --top_tests 5
```

## 12. 完整 pipeline 运行方式

```bash
cd /root/Baxxhy/BugReproduce

python -m brt4.run \
  --instances_path <原始issue数据文件> \
  --code_retrieval_path <iCoRe DeepSeek-V3源码检索结果json> \
  --test_retrieval_path <iCoRe DeepSeek-V3测试检索结果json> \
  --repo_root_base /root/Baxxhy/BugReproduce/swe_repos \
  --output_dir /root/Baxxhy/BugReproduce/brt4/outputs \
  --model deepseek-v3 \
  --conda_env <env_name> \
  --max_workers 6 \
  --max_feedback_rounds 3 \
  --num_candidates 1
```

默认：

```text
validation_mode=buggy_only
num_candidates=1
max_feedback_rounds=3
max_workers=6
```
