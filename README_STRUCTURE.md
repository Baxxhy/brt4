# BRT4 Structure

```text
brt4/
  README.md
  README_RUN.md
  README_STRUCTURE.md
  config.py
  run.py

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

Root-level Python files such as `generator.py`, `executor.py`, `llm_client.py`,
and `issue_rewriter.py` are compatibility wrappers only. The implementation
lives in the functional packages above.

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

Old root-level result directories are cleaned through
`scripts/clean_old_results.sh`, which writes manifests before deleting or
archiving anything.
