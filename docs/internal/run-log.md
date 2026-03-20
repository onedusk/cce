# Pipeline Run Log

Tracking all live pipeline runs with key metrics. Updated after each run.

---

## Summary

| # | Date | Run ID | Topic | Evidence | Conf. | Cov. | Div. | Iters | Result | Time | Notes |
|---|------|--------|-------|:--------:|:-----:|:----:|:----:|:-----:|--------|:----:|-------|
| 1 | 2026-03-17 | `run_b38b10b67614` | CBT-I | 794 | 0.0* | 0.0* | 0.01* | 2 | review | — | P0: verifier JSON parse failure on iter 2; P2: diversity formula bug |
| 2 | 2026-03-18 | `run_e95b5bc2b2fe` | CBT-I | 785 | 0.0* | 0.0* | 0.933 | 2 | review | — | P0: same parse failure; P2 fixed (diversity now correct) |
| 3 | 2026-03-19 | `run_2bbfadf44ed4` | CBT-I | 785 | 0.986 | 0.986 | 1.0 | 2 | pass | ~9.5m | P0 fixed; first successful completion. Iter 1 FAIL (density), iter 2 PASS |
| 4 | 2026-03-20 | `run_d23432a7e42a` | CBT-I | 75 | 0.965 | 0.965 | 1.0 | 1 | pass | ~4m | Evidence cap shipped (746→75). First single-iteration PASS |
| 5 | 2026-03-20 | `run_3f55a3c5cc95` | CBT-I | 75 | 0.810 | 0.900 | 0.933 | 1 | pass | ~3.5m | Stage tracking shipped. Per-iteration write/verify timing visible |
| 6 | 2026-03-20 | `run_cd9ebdc46a42` | Financial wellness | 75 | 0.942 | 0.942 | 0.867 | 1 | pass | ~5m | First non-CBT-I topic. 1,233 raw → 75 capped |
| 7 | 2026-03-20 | `run_204a94d1580b` | Social connectedness | 75 | 0.961 | 0.961 | 0.867 | 1 | pass | ~4.3m | Second new topic. 1,440 raw → 75 capped |

\* Scores are artifacts of bugs fixed in later runs, not real content quality.

---

## Stage Timing (runs with per-iteration tracking)

| Run # | Discover | Write (iter 1) | Verify (iter 1) | Write (iter 2) | Verify (iter 2) | Publish | Total |
|:-----:|:--------:|:--------------:|:---------------:|:--------------:|:---------------:|:-------:|:-----:|
| 5 | 2.8s | 92.2s | 123.2s | — | — | <0.1s | 218s |
| 6 | 51.5s | 118.3s | 139.9s | — | — | <0.1s | 310s |
| 7 | 27.0s | 104.1s | 126.5s | — | — | <0.1s | 258s |

---

## Observations

**Evidence capping impact (run 3 vs 4+):**
- Raw evidence: 746-1,440 → 75 (90-95% reduction)
- Pipeline time: ~9.5m → ~3.5-5m (~50% faster)
- Quality maintained: confidence 0.81-0.97 across all capped runs
- All capped runs pass on iteration 1 (no wasted rewrite cycles)

**Cross-topic generalization (runs 6-7):**
- Peer-reviewed policy works without modification across wellness dimensions
- Financial and social topics produce more raw evidence (1,233 and 1,440) than medical (746) — cap is even more critical
- Diversity slightly lower (0.867 vs 1.0) — writer cites 13/15 sources instead of 15/15
- Confidence and coverage consistently >0.80

**Verifier is the bottleneck:**
- Verify takes ~30% longer than write (120-140s vs 90-120s)
- Both scale with evidence count, but verifier does per-claim cross-referencing

---

## Policy Used

All runs use `policies/peer-reviewed.yaml`:
- Deny: reddit, quora, medium, pinterest, facebook, twitter, tiktok, youtube, amazon
- Trusted: .gov, .edu, pubmed, nih.gov, who.int, mayoclinic.org, hopkinsmedicine.org, sleepfoundation.org
- Max sources: 15
- Max age: 5 years
- Block marketing: true

---

## Engine Config

All runs use medium risk profile:
- Autopublish threshold: 0.70
- Min citations per paragraph: 1
- Max writer iterations: 2
- Evidence cap: 5 per source, 100 total (runs 4+ only)
