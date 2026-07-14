# P0 + Post-hoc Selector V2 Recovery

This branch restores the generation implementation at `ca41aa6` and keeps
Selector V2 outside the generation process.

## Baseline identity

- Documented run: `results/runs/run_p0_adaptive_seed_full276_20260706_013545`
- Documented metric: `136 / 276` (`49.2754%`)
- Most likely source revision: `ca41aa6a0a85071a35219bf91c3c39a25bd0e127`
- Confidence: high, based on the adaptive top-3 implementation commit and its
  contemporaneous result document. The historical run directory is no longer
  present, so a run-local Git revision cannot be independently verified.

The `137 / 276` result is an offline Selector V2 development result from a
different candidate pool. It is not treated as the P0 generation baseline.

## Behavioral boundary

`posthoc/selector_v2.py` is not imported from the P0 pipeline. The exporter
requires `generation.done` and one `summary.json` per dataset instance before
it reads rankings. It never executes candidates and never opens evaluation
outputs. Missing old-schema fields remain unknown and cause conservative
fallback to the Legacy candidate.

The two immutable views are written to:

- `exports/legacy_selection/`
- `exports/selector_v2_selection/`

The exporter refuses to overwrite either directory. Each export contains a
JSONL manifest and its SHA-256 digest.

## Defaults

- `enable_selector_v2_posthoc=true`
- `selector_v2_fallback_to_legacy=true`
- ATS adaptive search features are absent from the restored P0 source.
- Bidirectional counterfactual features are absent from the restored P0 source.

The future orchestration script is `scripts/run_p0_selector_v2_pipeline.sh`.
Its P0 settings retain DeepSeek-v3, temperature `0.1`, six generation workers,
two seed workers, and the existing fixed SWT-Lite retrieval files. Set
`EVAL_PYTHON` to an evaluator environment containing `datasets` and `socksio`.
