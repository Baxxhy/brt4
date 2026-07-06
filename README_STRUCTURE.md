# BRT4 Structure

```text
brt4/
  README.md
  README_RUN.md
  README_STRUCTURE.md
  SELF_CHECK.md
  __init__.py
  config.py
  run.py
  run_issue_rewrite.py
  direct_eval.py

  core/          shared schema, config wrappers, prompt constants, utilities
  llm/           LLM client and API pool
  issue/         issue rewrite stage
  retrieval/     iCoRe-derived environment and execution specs
  context/       host context and protocol recovery
  generation/    BRT generation and oracle synthesis
  mutation/      seed mutation planning
  execution/     command execution, dual-version helpers, surrogate patch loop
  validation/    verifier, strict verifier, semantic guard
  evaluation/    direct/formal evaluation implementation
  io/            input and retrieval loading helpers
  runtime/       compatibility wrappers for old runtime imports
  pipeline/      pipeline orchestration and compatibility wrappers
  prompts/       markdown prompt files by stage
  scripts/       supported shell entry points and utility scripts
  data/          input data, preserved
  retrieval_results/ preserved retrieval inputs
  results/
    runs/
    issue_rewrite/
    evaluation/
    smoke/
    logs/
    archive/
    cleanup_manifests/
```

The root directory intentionally keeps only README/config files, package
initialization, root CLI entry points, functional package directories, and
input/output top-level directories. Legacy root-level compatibility wrappers
such as `generator.py`, `executor.py`, `llm_client.py`, and `issue_rewriter.py`
have been removed from the working tree once current code paths no longer
depended on them. Use package imports such as `brt4.generation.generator`,
`brt4.execution.executor`, and `brt4.validation.verifier`.

## Source Mapping

- `llm/`: `api_pool.py`, `llm_client.py`
- `issue/`: `issue_rewriter.py`
- `retrieval/`: `icore_env_constants.py`, `icore_env_utils.py`, `icore_exec_spec.py`, `icore_runtime.py`
- `context/`: `host_context.py`, `protocol_recovery.py`
- `generation/`: `generator.py`, `observation_oracle.py`, `oracle.py`
- `execution/`: `executor.py`, `feedback.py` compatibility remains in `pipeline/`, `dual_version.py`, `patch_utils.py`
- `validation/`: `verifier.py`, `strict_semantic_verifier.py`, `semantic_guard.py`
- `evaluation/`: `direct_eval.py`
- `io/`: `io_utils.py`
- `core/`: `schema.py`, `config.py`, `utils.py`, prompt compatibility constants

## Prompt Layout

Prompt text is no longer hardcoded in Python. Files live under `prompts/` by
stage, with `system.md` and `user.md` where applicable. Python loads them
through `prompts/loader.py`; `core/prompts.py` preserves the old constant names.

## Results

New run outputs go to:

- `results/runs/<run_name>/generation/`
- `results/runs/<run_name>/evaluation/`
- `results/issue_rewrite/<timestamp>/`
- `results/smoke/<timestamp>/`
- `results/logs/<timestamp>/`

Historical root-level issue/input snippets live under:

- `data/issues/legacy/`

Historical root-level result directories and logs live under:

- `results/archive/`
- `results/logs/legacy/`

All cleanup and archival operations must write manifests under
`results/cleanup_manifests/` before removing or moving files. New runs should
not write `outputs*`, `formal_f2p*`, `direct_eval*`, logs, pid files, or
temporary run scripts into the repository root.
