# P0 + Post-hoc Selector V2 Recovery Plan

- Stable baseline: `ca41aa6` (`run_p0_adaptive_seed_full276_20260706_013545`, documented 136/276; run directory no longer present).
- Recovery branch: `recovery/p0-selector-v2`.
- Generation contract: preserve all files and behavior from `ca41aa6` unchanged.
- Selector contract: run only after generation, read all top-level and seed-level checkpoints, deduplicate by normalized AST, and export independently from Legacy.
- Forbidden selector inputs: golden data, fixed execution, formal results, instance lists, and all 2x2/counterfactual evidence.
- Verification: static search, `git diff --check`, Python compilation, and shell syntax only.
- No experiment, smoke, API, tmux, candidate, surrogate, or formal execution is permitted during recovery.
