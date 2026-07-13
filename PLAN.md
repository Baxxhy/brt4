# ATS-BRT SWT-Lite Main Experiment Plan

## 1. Objective

- run id: `run_swtlite276_ats_brt_20260714`
- selected idea: Replace the default 2x2 counterfactual path with executable behavioral contracts, protocol-preserving typed search, and a duplicate-aware online candidate archive. Keep surrogate validation and a frozen, generation-only Selector V2 as supporting evidence.
- user's core requirements: implement ATS-BRT, run a five-instance SWT-Lite smoke, push the code, stop TDD work, then launch a clean SWT-Lite 276 generation and formal evaluation pipeline.
- non-negotiable constraints: no historical candidates or formal labels during generation; no golden patch before formal evaluation; do not overwrite historical runs; count missing generated tests as failures.
- research question: Does archive-guided adaptive typed search produce better BRT candidates without relying on 2x2 counterfactual evidence?
- null hypothesis: The adaptive search produces no useful candidate-quality improvement over the current P0 search.
- alternative hypothesis: Contract-guided trigger/oracle search and duplicate redirection improve candidate quality while keeping runtime within roughly 1.2-1.5x.

## 2. Baseline And Comparability

- baseline id: current P0 three-stage pipeline on `exp/p0-baseline-ca41`.
- baseline variant: DeepSeek-v3, fixed iCoRe retrieval, surrogate validation.
- dataset / split: SWT-Bench Lite, 276 unique instances.
- primary metric: formal `F2P_SUCCESS / 276`.
- required metric keys: `F2P_SUCCESS`, `F2P@1`, `FIXED_FAIL`, `BUGGY_PASS`, setup/error counts.
- comparability risks: ATS-BRT adds up to three unique candidates only for hard instances; no historical generation artifacts are reused.

## 3. Code Translation Plan

| Path | Current role | Planned change | Why this is needed | Risk |
|---|---|---|---|---|
| `core/schema.py` | pipeline dataclasses | add behavioral contracts, segments, archive/search records, ATS config | backward-compatible artifacts | low |
| `generation/segmenter.py` | new | AST scaffold/trigger/oracle segmentation and safe recomposition | protocol-preserving transformations | medium |
| `generation/archive.py` | new | normalized AST/behavior dedup and archive persistence | avoid repeated execution and redirect budget | medium |
| `generation/structured_observation.py` | new | fixed observation schema and bounded normalization | minimal public oracle search | medium |
| `generation/adaptive_search.py` | new | explainable controller and online Selector V2 | adaptive branch selection | medium |
| `execution/feedback.py` | feedback loop | integrate archive, controller, typed branches, selector | main ATS-BRT behavior | high |
| `pipeline/run.py`, `scripts/run_generate.sh` | CLI/config | add flags and disable 2x2 defaults | reproducible run contract | medium |
| `prompts/*` | LLM contracts | behavioral contract, trigger/oracle branch instructions | direct the search without golden data | low |
| `scripts/run_ats_brt_pipeline.sh` | new | generation completeness, export, full formal eval | clean TDD/SWT entry point | medium |

## 4. Execution Design

- minimal experiment: five fixed risk-category instances selected from existing IDs, without reading historical candidates or formal status during generation.
- smoke / pilot plan: fresh output directory, `LIMIT`/instance subset, 2x2 disabled, ATS enabled; verify archive, segmenter, branch trace, selector and one final test entry.
- full run plan: clean SWT-Lite 276 run, retry missing generation once, freeze export, then formal evaluation with missing tests counted as failures.
- expected outputs: generation artifacts, archive/search traces, selection manifest, formal metrics and model-call summary.
- stop condition: generation and formal evaluation finish, or a reproducibility/environment blocker is durably logged.
- abandonment condition: smoke crashes or more than 20% of early instances fail from environment setup.
- strongest alternative hypothesis: retrieval/seed mismatch, not typed search, is the dominant bottleneck.

## 5. Runtime Strategy

- command for smoke: recorded under the smoke run directory.
- command for main run: recorded under the SWT-Lite run directory and printed in the handoff.
- expected runtime / budget: generation roughly 1.2-1.5x current pipeline; formal evaluation follows generation.
- log / artifact locations: `results/runs/swtlite/<run_name>/logs` and per-instance generation directories.
- safe efficiency levers: 4 generation workers, 1 seed worker, 4 issue-rewrite workers, 8 formal workers, per-environment locks, duplicate execution reuse.
- existing tooling: conda/worktree cache is reused; candidate and historical evaluation artifacts are not.
- monitoring: inspect after 1, 2, 5 and 10 minutes, then every 30 minutes; stop/relaunch only on a concrete crash, invalid config, or stalled log with no live process.

## 6. Fallbacks And Recovery

- model endpoint failure: preserve logs and retry through existing bounded LLM retry policy.
- resource pressure: reduce generation workers without changing method settings.
- smoke code-path failure: fix the smallest implementation defect and rerun the same smoke subset.
- non-comparable full run: stop before formal evaluation and preserve the run as partial.

## 7. Checklist Link

- checklist path: `CHECKLIST.md`
- next unchecked item: launch and monitor the clean SWT-Lite 276 main run.

## 8. Revision Log

| Time | What changed | Why it changed | Impact |
|---|---|---|---|
| 2026-07-14 01:51 CST | TDD 449 initially chosen as the launch dataset | an earlier request named TDD before the SWT template | superseded before main launch |
| 2026-07-14 02:17 CST | Main dataset corrected to SWT-Lite 276 and TDD smoke stopped | latest user instruction explicitly requires SWT only | fresh SWT artifacts; no TDD process remains |
| 2026-07-14 02:18 CST | Added AST typed-segment transplant fallback | whole-file LLM proposals changed frozen regions in smoke | frozen regions remain hash-verified while permitted edits survive |
| 2026-07-14 02:44 CST | Shared the duplicate archive and extra-candidate budget across all seeds | per-seed archives could repeat the same candidate and exceed the instance-level budget | duplicate execution is reusable across seeds and ATS extras remain capped at three per instance |
| 2026-07-14 02:55 CST | Isolated environment requirement files and pinned Python before the first conda solve | concurrent workers raced on a shared requirements file and an unpinned Matplotlib solve selected Python 3.14 | worker setup is isolated and legacy Python constraints are applied before dependency resolution |
| 2026-07-14 03:13 CST | Added a consecutive behavior-duplicate stop and completed the final guard smoke | behavior-equivalent candidates could consume branch attempts after AST deduplication | the guard stopped after the second behavior duplicate and retained one final test entry |
