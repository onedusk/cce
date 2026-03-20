# Stage 3: Task Index — test-suite

> Master build plan for the CCE test suite. 8 milestones, 15 tasks, ~147 tests.

---

## Legend

- `[ ]` — Not started
- `[x]` — Complete
- **CREATE** — new file
- **MODIFY** — edit existing file
- Task IDs: `T-{milestone}.{sequence}` (e.g., T-01.03 = Milestone 1, task 3)

---

## Progress

| # | Milestone | File | Tasks | Done |
|---|-----------|------|:-----:|:----:|
| M01 | Test Infrastructure | [tasks_m01.md](tasks_m01.md) | 3 | 0 |
| M02 | Parsing & Models | [tasks_m02.md](tasks_m02.md) | 2 | 0 |
| M03 | Discovery Unit Tests | [tasks_m03.md](tasks_m03.md) | 1 | 0 |
| M04 | Quality Gate | [tasks_m04.md](tasks_m04.md) | 1 | 0 |
| M05 | Verifier & Writer | [tasks_m05.md](tasks_m05.md) | 2 | 0 |
| M06 | Evidence Storage | [tasks_m06.md](tasks_m06.md) | 1 | 0 |
| M07 | Config & Policy | [tasks_m07.md](tasks_m07.md) | 2 | 0 |
| M08 | Pipeline & Output Integration | [tasks_m08.md](tasks_m08.md) | 3 | 0 |
| | **Total** | | **15** | **0** |

---

## Milestone Dependencies

```
M01 (Infrastructure)
 │
 ├──► M02 (Parsing & Models)
 │
 ├──► M03 (Discovery Unit)  ──────────────┐
 │                                        │
 ├──► M04 (Quality Gate)                  │
 │                                        │
 ├──► M05 (Verifier & Writer)             ├──► M08 (Pipeline & Output Integration)
 │                                        │
 ├──► M06 (Evidence Storage) ─────────────┘
 │
 └──► M07 (Config & Policy)
```

**Critical path:** M01 → M03 → M08 (longest chain: infrastructure → discoverer unit tests → discoverer integration + pipeline + output)

**Parallelizable after M01:**
- M02, M03, M04, M05, M06, M07 can all run in parallel (each depends only on M01)
- M08 depends on M03 and M06 (discoverer integration extends M03's file; pipeline tests use sqlite_store from M06's patterns)

---

## Milestone File Map

### M01 — Test Infrastructure (3 tasks)

| Task | File | Action |
|------|------|--------|
| T-01.01 | `pyproject.toml` | MODIFY |
| T-01.02 | `tests/test_models/__init__.py` | CREATE |
| | `tests/test_config/__init__.py` | CREATE |
| | `tests/test_policy/__init__.py` | CREATE |
| | `tests/test_discovery/__init__.py` | CREATE |
| | `tests/test_evidence/__init__.py` | CREATE |
| | `tests/test_synthesis/__init__.py` | CREATE |
| | `tests/test_verification/__init__.py` | CREATE |
| | `tests/test_orchestrator/__init__.py` | CREATE |
| T-01.03 | `tests/conftest.py` | CREATE |

### M02 — Parsing & Models (2 tasks)

| Task | File | Action |
|------|------|--------|
| T-02.01 | `tests/test_parsing.py` | CREATE |
| T-02.02 | `tests/test_models/test_models.py` | CREATE |

### M03 — Discovery Unit Tests (1 task)

| Task | File | Action |
|------|------|--------|
| T-03.01 | `tests/test_discovery/test_discoverer.py` | CREATE |

### M04 — Quality Gate (1 task)

| Task | File | Action |
|------|------|--------|
| T-04.01 | `tests/test_verification/test_gate.py` | CREATE |

### M05 — Verifier & Writer (2 tasks)

| Task | File | Action |
|------|------|--------|
| T-05.01 | `tests/test_verification/test_verifier.py` | CREATE |
| T-05.02 | `tests/test_synthesis/test_writer.py` | CREATE |

### M06 — Evidence Storage (1 task)

| Task | File | Action |
|------|------|--------|
| T-06.01 | `tests/test_evidence/test_sqlite.py` | CREATE |

### M07 — Config & Policy (2 tasks)

| Task | File | Action |
|------|------|--------|
| T-07.01 | `tests/test_config/test_loader.py` | CREATE |
| T-07.02 | `tests/test_policy/test_loader.py` | CREATE |

### M08 — Pipeline & Output Integration (3 tasks)

| Task | File | Action |
|------|------|--------|
| T-08.01 | `tests/test_discovery/test_discoverer.py` | MODIFY |
| T-08.02 | `tests/test_orchestrator/test_pipeline.py` | CREATE |
| T-08.03 | `tests/test_output.py` | CREATE |

---

## Target Directory Tree

```
cce/
  pyproject.toml                              MODIFY (M01)
  tests/
    __init__.py                               (exists)
    conftest.py                               CREATE (M01)
    test_parsing.py                           CREATE (M02)
    test_output.py                            CREATE (M08)
    test_models/
      __init__.py                             CREATE (M01)
      test_models.py                          CREATE (M02)
    test_config/
      __init__.py                             CREATE (M01)
      test_loader.py                          CREATE (M07)
    test_policy/
      __init__.py                             CREATE (M01)
      test_loader.py                          CREATE (M07)
    test_discovery/
      __init__.py                             CREATE (M01)
      test_discoverer.py                      CREATE (M03), MODIFY (M08)
    test_evidence/
      __init__.py                             CREATE (M01)
      test_sqlite.py                          CREATE (M06)
    test_synthesis/
      __init__.py                             CREATE (M01)
      test_writer.py                          CREATE (M05)
    test_verification/
      __init__.py                             CREATE (M01)
      test_verifier.py                        CREATE (M05)
      test_gate.py                            CREATE (M04)
    test_orchestrator/
      __init__.py                             CREATE (M01)
      test_pipeline.py                        CREATE (M08)
```

**Totals:** 19 files created, 2 modifications (pyproject.toml + test_discoverer.py), 0 deletions

---

## Feature-to-Milestone Mapping

| Stage 1 Feature | Milestone(s) |
|-----------------|-------------|
| conftest.py with factories and mocks | M01 |
| Pytest markers in pyproject.toml | M01 |
| Test directory structure with __init__.py | M01 |
| test_parsing.py (14 tests) | M02 |
| test_models.py (5 tests) | M02 |
| test_discoverer.py unit tests (28 tests) | M03 |
| test_gate.py (20 tests) | M04 |
| test_verifier.py (13 tests) | M05 |
| test_writer.py (13 tests) | M05 |
| test_sqlite.py (19 tests) | M06 |
| test_loader.py config (8 tests) | M07 |
| test_loader.py policy (6 tests) | M07 |
| test_discoverer.py integration (5 tests) | M08 |
| test_pipeline.py (8 tests) | M08 |
| test_output.py (8 tests) | M08 |

## ADR/PDR-to-Milestone Mapping

| Decision | Milestone |
|----------|-----------|
| ADR-001: Mock at protocol level | M01 (MockLLMProvider in conftest) |
| ADR-002: Real SQLite via tmp_path | M01 (sqlite_store fixture), M06 (tests) |
| ADR-003: Factory functions over fixtures | M01 (all factories in conftest) |
| ADR-004: Mirror source layout | M01 (directory structure) |
| ADR-005: No additional test dependencies | M01 (no changes to pyproject deps) |
| PDR-001: Pipeline logic first | M02–M05 sequenced before M08 |
| PDR-002: Start with conftest.py | M01 is first milestone |
| PDR-003: Defer e2e tests | M01 (marker defined, no e2e tests written) |

---

## Before Moving On

Verify before proceeding to Stage 4:

- [x] Every file from Stage 2 skeletons appears in the directory tree (conftest.py + all test files + __init__.py files)
- [x] Every feature from Stage 1 maps to at least one milestone (see mapping table)
- [x] Every ADR/PDR is fulfilled by at least one milestone (see mapping table)
- [x] The dependency graph has no cycles (M01 → {M02..M07} → M08, strictly acyclic)
- [x] The critical path is identified (M01 → M03 → M08)
- [x] Parallel work is maximized (M02–M07 all parallelizable after M01)
- [x] Each milestone is independently testable (each produces passing tests that can run in isolation)
- [x] First milestone creates the foundation layer (M01 = conftest + markers + directory structure)
- [x] Last milestone is the most experimental/optional feature (M08 = integration tests, highest coupling)
