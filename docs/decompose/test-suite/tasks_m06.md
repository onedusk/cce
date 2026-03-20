# Stage 4: Task Specifications — Milestone 06: Evidence Storage

> Integration tests for `SQLiteEvidenceStore` using real SQLite via `tmp_path`. Validates CRUD, deduplication, search, and serialization roundtrips.
>
> Fulfills: ADR-002 (real SQLite via tmp_path)

---

- [ ] **T-06.01 — Write test_sqlite.py (19 tests)**
  - **File:** `tests/test_evidence/test_sqlite.py` (CREATE)
  - **Depends on:** T-01.03
  - **Outline:**
    - Import `SQLiteEvidenceStore` from `cce.evidence.sqlite`
    - Import `make_evidence` from conftest
    - Use the `sqlite_store` fixture from conftest (real SQLite DB per test)
    - All tests are `@pytest.mark.integration`, `async def`
    - **Schema**:
      - `test_connect_creates_schema` — after `sqlite_store` connects, query `sqlite_master` for `evidence` and `_meta` tables. Verify both exist. Query `_meta` for schema_version = `"1"`
    - **put / get**:
      - `test_put_and_get` — `await store.put(ev)`, then `await store.get(ev.id)` → returned evidence has matching `id`, `url`, `title`, `author`, `excerpt`, `excerpt_hash`
      - `test_put_returns_true` — `await store.put(ev)` → returns `True`
      - `test_put_duplicate_hash_returns_false` — two evidence with same `excerpt_hash` but different `id`, second `put()` → `False`
    - **put_many**:
      - `test_put_many` — 5 unique evidence → returns `5`
      - `test_put_many_dedup` — 3 unique + 2 with duplicate hashes → returns `3`
    - **get / get_many**:
      - `test_get_nonexistent` — `await store.get("no-such-id")` → `None`
      - `test_get_many` — store 3 evidence, `get_many([id1, id2])` → returns 2
      - `test_get_many_empty_list` — `await store.get_many([])` → `[]`
      - `test_get_many_partial_miss` — request 3 IDs, only 2 exist → returns 2 (missing silently skipped)
    - **search**:
      - `test_search_by_url` — store evidence with different URLs, `search(url="https://example.com")` → only matching (prefix match)
      - `test_search_by_topic` — store evidence with "machine learning" in title, `search(topic="machine learning")` → finds it (LIKE match on title and excerpt)
      - `test_search_limit` — store 10 items, `search(limit=3)` → exactly 3 returned
      - `test_search_no_filters` — `search()` with no args → returns all (up to default limit 50)
    - **exists_by_hash**:
      - `test_exists_by_hash_true` — store evidence, `exists_by_hash(ev.excerpt_hash)` → `True`
      - `test_exists_by_hash_false` — `exists_by_hash("nonexistent_hash")` → `False`
    - **count**:
      - `test_count` — store 7 items, `count()` → `7`
    - **Serialization roundtrip**:
      - `test_serialization_roundtrip` — store evidence with all fields populated (including `SourceQuality` with all fields set). Retrieve it. Assert every field matches exactly, including `source_quality.is_peer_reviewed`, `.domain_reputation`, `.conflict_of_interest`
      - `test_serialization_nullable_fields` — evidence with `title=None, author=None, published_at=None, source_quality=None`. Roundtrip preserves all `None` values
  - **Acceptance:**
    - `uv run pytest tests/test_evidence/test_sqlite.py` passes all 19 tests
    - All tests use the `sqlite_store` fixture (real SQLite, auto-cleaned)
    - `test_put_duplicate_hash_returns_false` creates two different evidence objects with the same excerpt (and thus same hash)
    - `test_serialization_roundtrip` compares `SourceQuality` sub-object field by field
    - No test depends on execution order (each gets a fresh DB)
