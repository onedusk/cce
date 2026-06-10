# scripts/

One-off runners and research artifacts. **The supported front door is the
`cce` CLI** (`uv run cce --help`) — see the README quick start. Nothing in
this directory is required to operate the engine; the scripts are kept
because they encode live-run lore, not because they are maintained surface
(PDR-001, audit-2026-06-09).

## Operational runners

| Script | Classification |
|--------|----------------|
| `run_live.py` | Superseded by `cce batch`. Kept as a hardcoded-config example of driving `CurationEngine.embedded()` directly from Python. |
| `run_batch.py` | Superseded by `cce batch --topics-file`. Kept as a hardcoded-config example (topic list lives in the source). |
| `run_emit_mdx.py` | Superseded by `cce emit-mdx`. Reads `output/run_*/result.json` files rather than the job store. |
| `run_live_humanization.py` | Live harness for the humanization stack: loads `config/humanization_live.yaml` (scorer + editor + implied-claim checker active) and runs its `TOPICS` list, one job per topic, for baseline-vs-humanized MDX comparison. Costs real LLM/crawl spend. |

## research/ — calibration and diagnostic artifacts

| Script | Purpose |
|--------|---------|
| `research/run_score_sweep.py` | Scores existing MDX output against the humanization thresholds and prints per-metric distributions. Produced the 2026-04-17 threshold calibration against 36 archival drafts (see CLAUDE.md). $0 — no network, no LLM. |
| `research/run_contrastive_census.py` | Contrastive-frame subtype census over `output/mdx` (2026-04-22). Informed the `by contrast` marker addition and the Phase A/B scoping decision. $0 — no network, no LLM. |
| `research/run_parasitic_prompt_test.py` | Diagnostic 3 of the Phase A/B scoping: re-runs the three worst parasitic-frame topics with a learn-path prompt addendum to measure writer-time reduction. Live run — costs real LLM/crawl spend. See its module docstring. |
