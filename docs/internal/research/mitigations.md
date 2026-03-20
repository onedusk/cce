# Writer Agent Constraints (synthesis module)

The biggest wins come from what you tell the writer to do *during* generation, not from fixing things after.

**Vocabulary suppression.** The research identified concrete blocklists. You can maintain a `SUPPRESSED_TOKENS` or `AVOID_WORDS` set in the writer config — the 21 focal words from the Juzek/Ward study are the highest-signal ones: delve, intricate, nuanced, underscore, showcasing, comprehensive, multifaceted, pivotal, commendable, meticulous, realm, landscape, tapestry, crucial, robust. Plus the Stanford top-10 markers: across, additionally, enhancing, exhibited, insights, notably, particularly, within. The writer prompt should explicitly instruct the model to avoid these and their close synonyms. This isn't a guarantee — the model may still gravitate toward them — but it measurably reduces frequency.

**Sentence length variance.** This is the burstiness problem. Human writing mixes 4-word fragments with 30+ word compound sentences. The writer prompt should explicitly request varied sentence lengths and include examples of what that looks like. You could also add a post-generation check that measures sentence length standard deviation and rejects drafts below a threshold. Something like: if the standard deviation of sentence lengths in a paragraph is below 4 words, flag it.

**Structural variation rules.** The research showed AI paragraphs almost always follow topic-sentence, evidence, summary. The writer prompt should specify multiple paragraph opening strategies: start with a concrete example, start with a question, start with a counterpoint, start mid-argument. You could even randomize which strategy the writer is told to use per section, so the output doesn't settle into a pattern.

**Kill the hedging.** The writer prompt should instruct the model to make declarative statements where the evidence supports them. Instead of "It should be noted that research suggests CBT-I may be effective," just say "CBT-I works." The evidence store already backs the claim — the hedge adds nothing except an AI fingerprint. Reserve qualifiers for genuinely uncertain claims.

**First person and specificity.** If the content platform has an editorial voice or persona, the writer should be told to use it. Even a simple instruction like "write as if you're explaining this to a colleague, not writing a textbook" shifts the output away from the encyclopedic tone that screams AI. The CBT-I output you showed me is a textbook example of this problem — it reads like a Wikipedia article, not like something a person would write.

## A Concrete Example From Your Output

Look at this from the CBT-I piece:

> "Cognitive behavioral therapy for insomnia (CBT-I) is a short, structured, and evidence-based approach to treating insomnia — the difficulty falling asleep, staying asleep, or waking too early. Unlike sleeping pills, CBT-I addresses the underlying causes of sleep problems rather than just relieving symptoms."

That's the "it's not X, it's Y" contrastive framing in the second sentence, a definitional appositive, and the em dash parenthetical — three AI tells in two sentences. A more human version using the same evidence:

> "If you've tried sleeping pills and they stopped working — or you never wanted to start them — CBT-I is worth knowing about. It's a structured therapy, usually six to eight sessions, that goes after the habits and thought patterns keeping you awake instead of just sedating you past them."

Same facts, same evidence backing, but it has a voice. It varies sentence structure. It addresses the reader directly. It doesn't define insomnia (the reader already knows what it is if they're reading this).

## New Pipeline Stage: Humanization Verifier

This sits between the writer and the existing citation verifier, or runs in parallel. Its job is pattern detection against the known AI markers.

**What it checks:**

- Token-level: scan for suppressed vocabulary, flag any that slipped through
- Sentence-level: measure length variance, flag paragraphs with low standard deviation
- Paragraph-level: check opening words/phrases against a formulaic-transitions list ("Furthermore," "Additionally," "In conclusion," etc.)
- Section-level: check if consecutive sections follow the same structural template
- Document-level: measure lexical diversity (type-token ratio), flag if below threshold
- Rhetorical: count contrastive frames, rule-of-three instances, hedging phrases per 1000 words — flag if density exceeds thresholds

**What it does with flags:** Same pattern as the citation verifier — it sends flagged sections back to the writer with specific rewrite instructions. "Paragraph 3 opens with 'Furthermore' and follows the same topic-evidence-summary structure as paragraphs 1 and 2. Rewrite with a different opening strategy and vary the internal structure."

## Quality Gate Expansion

The existing quality gate enforces "no citation, no ship." You add a parallel rule: **"no humanization pass, no ship."** The gate checks:

1. Citation coverage (existing)
2. AI marker density below threshold (new)
3. Burstiness score above threshold (new)
4. Lexical diversity above threshold (new)
5. Structural variation score (new — measures how different consecutive paragraphs are from each other)

The thresholds need calibration. You'd want to run a few batches, score them, have the client's team read them blind alongside human-written content, and tune from there. There's no universal number — it depends on the domain and the client's editorial voice.

## What's Measurable vs. What's Not

The vocabulary and statistical stuff (perplexity, burstiness, lexical diversity, n-gram frequency) is straightforward to compute programmatically. You could add a lightweight scoring module that runs these metrics without needing an LLM call.

The structural and rhetorical patterns are harder to automate. Detecting "contrastive framing" or "semantic repetition where the same claim is restated six ways" requires either regex patterns for common forms (fragile, high maintenance) or a separate LLM pass acting as a style critic (more robust but adds latency and cost).

Given CCE already has a writer-verifier loop, I'd lean toward making the humanization verifier an LLM-based critic with a specific prompt that encodes the patterns from the research. It reviews the draft and returns structured feedback — which flags fired, where, and what to fix. The writer then gets one revision pass. If it still fails, it goes to human review.

## The Coevolution Problem

One thing worth flagging to the client: the research showed that once specific AI markers become publicly known, models start suppressing them and new ones emerge. The word "delve" is already declining in AI output because it got so much attention. This means the suppressed vocabulary list and the rhetorical pattern checks need periodic updates. Building this as config (YAML, JSON) rather than hardcoded logic is the right move — which CCE's config module already supports.

--- 

Let's sketch out the actual data contracts for the humanization verifier, and map these changes to specific files in the CCE package structure
