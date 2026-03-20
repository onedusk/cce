# Stage 0: Development Standards

> CCE project standards for a small team (2–4 developers).

---

## Code Change Checklist

### 1. Plan

- [ ] Define scope in one sentence (what and why)
- [ ] Map affected files and their dependencies
- [ ] Determine change severity (see Escalation Guidance)

### 2. Implement

- [ ] Execute the plan, one logical unit at a time
- [ ] Keep changes scoped — avoid drive-by fixes

### 3. Test

- [ ] Write or update tests for changed behavior
- [ ] Cover at least one happy path and one failure path
- [ ] Test boundary conditions (empty inputs, nulls, limits)
- [ ] Run: `uv run pytest` (full suite) or targeted: `uv run pytest tests/test_<module>/`

Self-check:
- Do tests describe *behavior*, not implementation details?
- Am I testing at the right level? (unit for pure logic, integration for I/O boundaries)

### 4. Lint & Format

- [ ] `uv run ruff check src/` — fix all lint errors
- [ ] `uv run ruff format src/` — ensure consistent formatting

### 5. Commit

- [ ] Write a clear commit message: imperative mood, one-line summary, optional body for why
- [ ] Keep commits atomic — one logical change per commit

### 6. Review

- [ ] Self-review the diff before pushing
- [ ] For medium+ severity: get a second pair of eyes (async review is fine)
- [ ] Verify all tests pass, no lint errors

---

## Changeset Format

Lightweight — no formal changelog files yet. Communicate changes through clear git commit messages.

**Commit message format:**

```
<type>: <one-line summary>

<optional body explaining why, not what>
```

**Types:** `feat`, `fix`, `refactor`, `test`, `docs`, `chore`

**Example:**

```
feat: add citation density check to quality gate

The verifier was passing content with sparse citations. Gate now
counts citations per substantive paragraph and fails drafts below
the configured threshold.
```

---

## Escalation Guidance

| Severity | Examples | Approval |
|----------|----------|----------|
| **Low** | Test additions, doc updates, internal refactors, formatting | Self-review |
| **Medium** | New features, non-breaking behavior changes, bug fixes | Self-review + async team review |
| **High** | Breaking changes, data model changes, protocol changes, config schema changes | Team review before merge |

**Rule:** When in doubt, escalate up one level.

---

## Testing Guidance

### Priority order

1. **Pipeline logic** — discovery filtering, quality gate decisions, write-verify loop. Bugs here produce wrong output.
2. **Data integrity** — evidence storage, serialization roundtrips, hash deduplication. Bugs here corrupt state.
3. **LLM response parsing** — JSON extraction from model outputs. Fragile by nature; edge cases are the norm.
4. **Configuration & policy loading** — YAML parsing, env var precedence. Wrong config = wrong behavior silently.

### Test levels

- **Unit tests** for pure logic: static methods, parsing, decision logic, model validation. Fast, no I/O.
- **Integration tests** for I/O boundaries: SQLite store (real DB via `tmp_path`), config loading (real filesystem), pipeline orchestration (mocked externals).
- **E2E tests** sparingly: real API calls to Anthropic/Firecrawl. Expensive, slow, non-deterministic. CI excludes these by default.

### CCE-specific conventions

- All I/O is async — use `async def test_*()` with `asyncio_mode = "auto"`
- Mock at the **protocol level** (`LLMProvider`, `CrawlAdapter`, `EvidenceStore`), not at SDK internals
- Use **real SQLite** via `tmp_path` — mocking SQL gives false confidence
- Use `monkeypatch.setenv()` for env var tests, never set globals
- Constructor-based DI makes mocking straightforward — inject mocks as constructor args

### What makes a good test

- Tests behavior, not implementation
- One assertion per concept
- Readable as documentation
- Independent — no shared mutable state, no execution order dependency
