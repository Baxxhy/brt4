# Prompt Layout

Prompts are stored as markdown text files by pipeline stage. Python code loads
them with `load_prompt(stage, name)` from `prompts/loader.py`; `core/prompts.py`
keeps the previous constant names as compatibility aliases.

- `issue_rewrite/`: raw issue plus retrieval context to structured behavior target.
- `behavior_target/`: host/test context recovery around a behavior target.
- `generation/`: initial BRT generation from a seed test.
- `mutation_plan/`: seed mutation planning.
- `repair_setup/`: setup/import/collection repair.
- `repair_trigger/`: trigger-path repair.
- `repair_oracle/`: oracle/assertion repair.
- `verifier/`: buggy-only verifier.
- `strict_semantic_verifier/`: strict semantic verifier.
- `surrogate_patch/`: temporary surrogate source patch prompt.
- `protocol_recovery/`: recovered seed-test protocol audit.
- `observation_probe/`: probe-test generation for runtime observations.
- `assert_synthesis/`: final assertion synthesis from observations.
- `observation_oracle/`: observation-oracle probe and rebind templates.
