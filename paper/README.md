# Paper

LaTeX source for the persona × sycophancy paper. Standalone — no external `.sty` dependencies beyond standard TeX Live.

## Build

```bash
cd paper
latexmk -pdf main.tex
```

`latexmk` runs `pdflatex` + `bibtex` + `pdflatex` (twice) automatically. Output: `main.pdf`.

To clean build artifacts:

```bash
latexmk -C
```

## Editing in VS Code

LaTeX Workshop auto-detects `main.tex` as the root. The default build recipe (`latexmk`) Just Works.

## Editing in Overleaf

`File → Import from GitHub` (point at this directory), or upload as a zip. Set `main.tex` as the main document if Overleaf doesn't auto-detect it.

## Files

- `main.tex` — paper skeleton with section stubs matching the planned outline (see `docs/internal/research/research_branch_tasks.md` step 8).
- `references.bib` — pre-populated BibTeX for the cited papers from `research_branch.md` and `research_branch_pt2.md`. Two entries (`shanmugam_helpfulness_2025`, `openai_gpt4o_sycophancy_2025`) have `TODO`s — fill in before the draft is final.

## Section ↔ source-doc mapping

The skeleton sections cite-comment the planned content. Existing CCE research docs that map to specific sections:

| Section | Source doc |
|---|---|
| §3 Hypothesis | `research_branch.md` (lines 49–67) |
| §4 CCE | `CLAUDE.md` (architecture); `docs/decompose/humanization/` |
| §5 Experimental Design | task #4 in `research_branch_tasks.md` |
| §7 Implied Claims | `contrastive_framing_as_implied_claims.md` |
| §8 Mitigation | `mitigations.md` |
| §9 Discussion | `evidence_synthesis_generalization.md` |

## Venue swap

When targeting a venue post-arXiv, swap `\documentclass{article}` for the venue's class file (e.g., `acl-style`, `neurips_2026`) and re-check the bibliography style — most cs.* venues prefer `acl-natbib` or numerical styles over `plainnat`.
