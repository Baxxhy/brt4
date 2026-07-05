# BRT4 Run Guide

All new runs write under `results/` with timestamped directories. The scripts
auto-detect the project root and do not create `outputs_*`, `formal_f2p_*`, or
`direct_eval_*` in the repository root.

## Common Commands

```bash
cd /root/Baxxhy/BugReproduce/brt4
```

Issue rewrite only:

```bash
bash scripts/run_issue_rewrite.sh
```

Smoke generation with 5 instances:

```bash
bash scripts/run_smoke.sh
```

Full 276 generation:

```bash
bash scripts/run_generate.sh
```

Evaluate an existing run:

```bash
bash scripts/run_evaluate.sh results/runs/<run_name>
```

Formal F2P evaluation:

```bash
bash scripts/run_formal_eval.sh results/runs/<run_name>
```

Full pipeline:

```bash
bash scripts/run_full_pipeline.sh
```

Show latest results:

```bash
bash scripts/show_latest_results.sh
```

Dry-run old result cleanup:

```bash
bash scripts/clean_old_results.sh
```

Confirm cleanup:

```bash
bash scripts/clean_old_results.sh --confirm-delete
```

## Script Parameters

Most parameters are environment variables so scripts stay simple:

- `RUN_NAME`: run directory name under `results/runs/`.
- `RUN_DIR`: explicit run directory.
- `INSTANCES_PATH`: issue dataset JSON/JSONL.
- `CODE_RETRIEVAL_PATH`: iCoRe code retrieval JSON.
- `TEST_RETRIEVAL_PATH`: iCoRe test retrieval JSON.
- `REPO_ROOT_BASE`: SWE repository root.
- `MODEL`: default `deepseek-v3`.
- `TEMPERATURE`: default `0.1`.
- `WORKERS`: default `6` for generation/evaluation, `10` for issue rewrite.
- `LIMIT`: optional generation limit, used by smoke.

Each run directory contains `run_config.json`, `command.txt`, `logs/`,
`generation/`, `evaluation/`, `exports/`, `tmp/`, and done markers.
