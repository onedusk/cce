# Security

What the Content Curation Engine (CCE) guarantees about hostile input, and
what a consumer must still do. Report suspected vulnerabilities privately to
the repository owner rather than in a public issue.

## Trust boundaries

- **Untrusted:** everything that comes from a crawled page (excerpt text,
  title, author, URL) and everything a model writes (drafts, verifier
  reports, extracted topics). A consumer that curates competitors' pages or
  forwarded third-party content should treat hostile input as the normal
  case.
- **Trusted operator input:** the curation request (topic, subtopics, paths,
  audience, pinned `context`), source policies, path configs, taxonomies and
  engine config. Pinned context still reaches prompts only as data, defanged
  like any excerpt, and is not written to the evidence store, so it can't
  surface in another job.
- **Fetching:** CCE does not fetch source pages itself. The crawl adapter
  (Firecrawl, a hosted service with a fixed base URL) does, so there is no
  local SSRF surface. Which hosts may be used is set by the source policy's
  `domains_allow` / `domains_deny`.

## MDX output (`emit-mdx`)

**Guarantee.** No MDX expression (`{...}`), JSX or HTML tag, or ES module
statement (`import` / `export`) in a `page.mdx` body comes from crawled or
model-written text:

- The body, including code spans and fences, is written with `{`, `}` and
  `<` as character references (`&#123;`, `&#125;`, `&lt;`), and a line
  starting with `import` or `export` gets its first letter as one (line
  endings are normalised first, so a lone CR can't start a line the check
  misses). MDX and CommonMark render these as the literal characters in
  text; inside code spans and fences references aren't decoded, so code
  containing those characters shows the reference. Clean prose is unchanged
  byte for byte.
- A crawled title in the rebuilt "Curated Resources" list is also kept to one
  line, with `\`, backticks, `*`, `[`, `]` and `>` escaped, so it can't
  close the bold around it or make a markdown link, code span or new block.
- The `export const metadata = {...}` block is a JSON literal
  (`json.dumps`), so its strings can't break out of their quotes.

Implementation: `src/cce/output/mdx/escape.py`.

**Still the consumer's job:**

- Markdown links written by the model are kept as links, and with GFM
  autolink literals enabled a `www.`, `http(s)://` or email address in body
  text or a crawled title becomes a link. Sanitise link and image
  destinations (for example `javascript:` URLs) in your renderer. A
  sanitising rehype step such as `rehype-sanitize` is recommended whatever
  CCE guarantees.
- The metadata values, `_evidence.json` and `meta.json` hold raw text: see
  below.

## Package, API and sidecar data

`PublishPackage` fields, the REST API responses (`/jobs/{id}/package`,
`/evidence/...`), `_evidence.json` and `meta.json` carry untrusted plain
text: unit content with `[ev:ID]` markers, raw excerpts, titles, authors,
verifier claims and feedback, writer gaps. Escape them for whatever context
you render them in (HTML, MDX, SQL, a shell).

## Prompt injection

**What CCE does.** Crawled and model-written text reaches a prompt only
inside an element the prompt builder writes itself: `<evidence id="...">`
for each excerpt (with its URL, title and author on one line) and `<draft>`
for a draft or draft fragment. The text is first defanged so it can't open
or close one of those elements or forge one of the prompts' `=== ... ===`
fences, which also keeps the prompt-cache split in place. The writer,
verifier, editor and implied-claim prompts each state that this text is
data, never instructions, and that only the `id` attributes of `<evidence>`
elements identify evidence. The delimiters are deterministic (no nonce), so
cached prompt prefixes stay byte-stable. Implementation:
`src/cce/evidence/formatting.py`.

**Deterministic guarantees** (they hold whatever a model does):

- A citation marker whose ID is not in the path's evidence (what the writer
  was shown) blocks a PASS at the quality gate.
- The writer keeps only `citations_used` and `evidence_map` IDs that
  resolve to the path's evidence, and `_evidence.json` lists only those.
- On the page, a marker whose ID is not in the run's evidence renders as
  `[^?]` and is left out of the page's citations. (`cce emit-mdx` emits a
  job that didn't complete only with `--job ... --force`.)
- An editor rewrite whose citation markers differ from the draft's is
  discarded; the writer's draft is kept.
- The quality gate reads the verifier's counts, the draft's markers, the
  evidence IDs and config, never excerpt or title text. For fixed evidence
  IDs and a fixed verifier reply, the decision is the same whatever the
  excerpts say.
- Writer and verifier replies are parsed as JSON of a fixed shape
  (structured outputs on models that support them); an unreadable one is
  retried once, then fails the job. The editor's reply is delimited
  markdown: unreadable, or with different citation markers, it is discarded
  and the writer's draft kept. The implied-claim checker gives no hint for
  an unreadable reply.

**Residual risk.** A model that follows injected text can still cite a real
but unrelated evidence ID, and a verifier can still mark an injected claim
as supported. No code can prove a model ignores an instruction. Mitigations:

- Run the verifier on a different model from the writer
  (`CCE_VERIFIER_MODEL`, `verifier.model`).
- Set `publish_policy: human` (`CCE_PUBLISH_POLICY=human`) so a passing job
  stops at `ready_for_approval` instead of being treated as publishable.
- Review the per-claim verdicts on `PublishPackage.verification`.
