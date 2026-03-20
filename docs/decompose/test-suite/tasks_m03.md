# Stage 4: Task Specifications — Milestone 03: Discovery Unit Tests

> Unit tests for all static/pure methods on `Discoverer`. Highest-value test file — protects domain logic that runs on every pipeline execution.
>
> Fulfills: PDR-001 (pipeline logic first)

---

- [ ] **T-03.01 — Write test_discoverer.py unit tests (28 tests)**
  - **File:** `tests/test_discovery/test_discoverer.py` (CREATE)
  - **Depends on:** T-01.03
  - **Outline:**
    - Import `Discoverer` from `cce.discovery.discoverer`
    - Import factories from conftest: `make_curation_request`, `make_source_policy`, `make_crawl_result`, `make_evidence`
    - Import types: `CrawlResult`, `ReputationRule`, `RecencyRule`, `TopicOverride`, `SourcePolicy`
    - All tests are `@pytest.mark.unit`, synchronous (static methods don't need async)
    - **Query building** (`Discoverer._build_queries`):
      - `test_build_queries_topic_only` — `CurationRequest(topic="ai safety", subtopics=[])` → `["ai safety"]`
      - `test_build_queries_with_subtopics` — `subtopics=["alignment", "interpretability"]` → `["ai safety", "ai safety alignment", "ai safety interpretability"]`
    - **Policy filtering** (`Discoverer._passes_policy`):
      - `test_passes_policy_allow_all` — empty allow + empty deny → any URL returns `True`
      - `test_passes_policy_deny_blocks` — `domains_deny=["spam.com"]`, URL `https://spam.com/page` → `False`
      - `test_passes_policy_deny_takes_priority` — URL domain in both allow AND deny → `False`
      - `test_passes_policy_allow_list_gates` — `domains_allow=["trusted.org"]`, URL `https://other.com` → `False`
      - `test_passes_policy_allow_list_passes` — `domains_allow=["trusted.org"]`, URL `https://trusted.org/paper` → `True`
      - `test_passes_policy_no_domain` — malformed URL `"not-a-url"` → `False`
      - `test_passes_policy_case_insensitive` — `domains_deny=["SPAM.COM"]`, URL `https://spam.com/page` → `False`
    - **Topic override resolution** (`Discoverer._resolve_overrides`):
      - `test_resolve_overrides_no_match` — topic `"cooking"`, override pattern `"medical"` → original policy returned unchanged
      - `test_resolve_overrides_match_merges` — topic `"medical research"`, override with `topic_pattern="medical"`, `domains_allow=["nih.gov"]`, custom reputation → merged policy has `nih.gov` in allow list, override reputation applied
      - `test_resolve_overrides_no_recursion` — returned policy has `topic_overrides == []`
    - **Content chunking** (`Discoverer._chunk_content`):
      - `test_chunk_content_empty` — `""` → `[]`
      - `test_chunk_content_single_paragraph` — short text under 1500 chars → `[text]`
      - `test_chunk_content_paragraph_split` — two paragraphs separated by `\n\n` → 2 chunks
      - `test_chunk_content_long_paragraph` — single paragraph >1500 chars → splits on `\n` boundaries, each chunk ≤1500
    - **Quality heuristics**:
      - `test_looks_peer_reviewed_doi` — `CrawlResult(url="https://doi.org/10.1234")` → `True`
      - `test_looks_peer_reviewed_pubmed` — `CrawlResult(url="https://pubmed.ncbi.nlm.nih.gov/123")` → `True`
      - `test_looks_peer_reviewed_normal` — `CrawlResult(url="https://blog.com/post")` → `False`
      - `test_looks_primary_gov_edu_org` — `.gov`, `.edu`, `.org` URLs → `True` for each
      - `test_looks_primary_com` — `.com` URL → `False`
      - `test_assess_reputation_trusted` — `ReputationRule(trusted_institutions=["nih.gov"])`, URL `https://nih.gov/study` → `"trusted"`
      - `test_assess_reputation_institutional` — `.edu` URL not in trusted list → `"institutional"`
      - `test_assess_reputation_unknown` — `.com` URL → `"unknown"`
      - `test_looks_marketing_positive` — markdown containing `"buy now"` or `"limited time offer"` → `True`
      - `test_looks_marketing_negative` — academic text without marketing keywords → `False`
    - **Evidence extraction** (`Discoverer._extract_evidence`):
      - `test_extract_evidence_creates_objects` — `CrawlResult` with 2 paragraphs → list of `Evidence` objects with correct `url`, `title`, `author`, `excerpt_hash` (SHA-256 of excerpt), `locator` format `"chunk:N"`, `source_quality` populated
      - `test_extract_evidence_published_at_parsing` — `published_date="2024-01-15T00:00:00Z"` → `published_at` is a `datetime`. Invalid date `"not-a-date"` → `published_at is None`
  - **Acceptance:**
    - `uv run pytest tests/test_discovery/test_discoverer.py` passes all 28 tests
    - All tests are synchronous (no `async def`) since they test static methods
    - `uv run pytest tests/test_discovery/test_discoverer.py -m unit` runs all 28
    - Each test exercises exactly one static method with one scenario
