# BRT4

BRT4 从 issue、iCoRe 已生成的源码/测试检索结果和 buggy 仓库出发，生成单个完整 Bug Reproduction Test，并在独立阶段进行真实补丁 F2P 评测。

## 快速运行

完整命令、恢复方式、tmux 和日志说明见 [README_RUN.md](README_RUN.md)。

## 项目结构

目录、代码文件和主调用链见 [README_STRUCTURE.md](README_STRUCTURE.md)。

## 最近结果

保留的 42% 和 40% 正式结果位于 `results/preserved/`。

## 新实验输出

所有新实验统一写入 `results/runs/run_YYYYMMDD_HHMMSS/`，不再写到项目根目录。

## 核心导出

每次 run 的完整逐实例结果位于 `exports/all_outputs.json`；另有 JSONL、测试代码简表和总指标摘要。
