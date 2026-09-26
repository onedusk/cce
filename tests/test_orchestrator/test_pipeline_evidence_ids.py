"""Every evidence ID a run cites exists in the store (B6).

Before B6 the store was UNIQUE on excerpt_hash alone: an excerpt already
stored under another URL (syndicated text) was silently not stored, yet the
run cited it under a fresh in-memory ID that GET /evidence/{id} couldn't find.
"""

from __future__ import annotations

import pytest

from cce.models.job import JobStatus
from cce.orchestrator.pipeline import Pipeline
from cce.parsing import EV_MARKER_RE, resolve_evidence_id
from tests.conftest import (
    MockCrawlAdapter,
    make_crawl_result,
    make_curation_request,
    make_engine_config,
    make_source_policy,
)
from tests.test_orchestrator.conftest import llm as _llm
from tests.test_orchestrator.conftest import verifier_json, writer_json

pytestmark = pytest.mark.integration

_SYNDICATED_PAGE = (
    "# Wire story\n\n"
    "This syndicated paragraph was published word for word by two different "
    "outlets, so both URLs carry exactly the same verbatim excerpt text."
)


def _adapter(url: str) -> MockCrawlAdapter:
    return MockCrawlAdapter(
        search_map={"test topic": [url]},
        url_map={url: make_crawl_result(url=url, markdown=_SYNDICATED_PAGE)},
    )


def _pipeline(store, adapter) -> Pipeline:
    return Pipeline(
        config=make_engine_config(),
        crawl_adapter=adapter,
        evidence_store=store,
        llm=_llm(writer_json(), verifier_json(supported=10, total=10, gaps=0)),
    )


async def _assert_every_cited_id_is_stored(result, store) -> list[str]:
    assert result.package is not None
    by_id = {ev.id: ev for ev in result.package.evidence}
    ids: set[str] = {ev.id for ev in result.package.evidence}
    for unit in result.package.units:
        ids.update(c.evidence_id for c in unit.citations)
        for match in EV_MARKER_RE.finditer(unit.content):
            resolved_id, _ = resolve_evidence_id(
                match.group(1) or match.group(2), by_id
            )
            ids.add(resolved_id)
    for ev_id in ids:
        assert await store.get(ev_id) is not None, f"{ev_id} is cited but not stored"
    return sorted(ids)


async def test_syndicated_excerpt_is_stored_under_the_cited_id(sqlite_store):
    """Run 1 stores the excerpt at URL A; run 2 finds the same text at URL B.
    Run 2's citations must resolve, and point at URL B."""
    first = await _pipeline(sqlite_store, _adapter("https://a.example/story")).run(
        make_curation_request(), make_source_policy()
    )
    assert first.job.status == JobStatus.COMPLETED

    second = await _pipeline(sqlite_store, _adapter("https://b.example/story")).run(
        make_curation_request(), make_source_policy()
    )

    assert second.job.status == JobStatus.COMPLETED
    cited = await _assert_every_cited_id_is_stored(second, sqlite_store)
    assert cited
    for ev_id in cited:
        assert (await sqlite_store.get(ev_id)).url == "https://b.example/story"


class _BlindToStoredUrls:
    """Store wrapper that hides stored URLs from discovery, like a concurrent
    job that crawled the same URL but hadn't stored it yet."""

    def __init__(self, real) -> None:
        self._real = real

    async def get_existing_urls(self, candidates: list[str]) -> set[str]:
        return set()

    def __getattr__(self, name):
        return getattr(self._real, name)


async def test_same_url_race_remaps_to_the_stored_ids(sqlite_store):
    """Run 1 stores URL X. Run 2 re-crawls X as if fresh (fresh in-memory IDs);
    its inserts are skipped as duplicates, so it must cite run 1's IDs."""
    url = "https://x.example/story"
    first = await _pipeline(sqlite_store, _adapter(url)).run(
        make_curation_request(), make_source_policy()
    )
    stored_ids = {ev.id for ev in first.package.evidence}

    second = await _pipeline(_BlindToStoredUrls(sqlite_store), _adapter(url)).run(
        make_curation_request(), make_source_policy()
    )

    assert second.job.status == JobStatus.COMPLETED
    assert {ev.id for ev in second.package.evidence} == stored_ids
    await _assert_every_cited_id_is_stored(second, sqlite_store)
    assert await sqlite_store.count() == len(stored_ids)
