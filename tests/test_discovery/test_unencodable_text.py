"""SEC-11: a page with a character UTF-8 cannot encode (a lone surrogate,
which ``json.loads`` accepts) must not fail the job.

In the markdown it raised UnicodeEncodeError out of ``discover()`` while
hashing the chunk; in the title or author it failed the job downstream.
"""

from __future__ import annotations

import pytest

from cce.config.types import CrawlConfig
from cce.discovery.discoverer import Discoverer
from tests.conftest import (
    MockCrawlAdapter,
    make_crawl_result,
    make_curation_request,
    make_source_policy,
)

pytestmark = pytest.mark.unit

OK = "https://ok.example/1"
HOSTILE = "https://hostile.example/2"
BODY = "A substantial paragraph with real content that clears the fragment floor."
SURROGATE = "\ud800"


async def _discover(**hostile_kw):
    adapter = MockCrawlAdapter(
        search_map={"test topic": [OK, HOSTILE]},
        url_map={
            OK: make_crawl_result(url=OK, markdown=BODY + " Fine page."),
            HOSTILE: make_crawl_result(url=HOSTILE, **hostile_kw),
        },
    )
    discoverer = Discoverer(adapter=adapter, config=CrawlConfig(api_key="k"))
    return await discoverer.discover(make_curation_request(), make_source_policy())


@pytest.mark.parametrize(
    "hostile_kw",
    [
        {"markdown": f"{BODY} x {SURROGATE} y"},
        {"markdown": BODY, "title": f"t {SURROGATE}"},
        {"markdown": BODY, "author": f"a {SURROGATE}"},
        {"markdown": BODY, "title": ["t", SURROGATE], "author": [SURROGATE]},
    ],
    ids=["markdown", "title", "author", "list-metadata"],
)
async def test_lone_surrogate_in_one_page_does_not_fail_discovery(hostile_kw):
    result = await _discover(**hostile_kw)

    assert {ev.url for ev in result.evidence} == {OK, HOSTILE}
    assert result.metrics["crawl_failed"] == 0
    for ev in result.evidence:
        ev.model_dump_json().encode("utf-8")  # every field is encodable
    [hostile] = [ev for ev in result.evidence if ev.url == HOSTILE]
    assert SURROGATE not in f"{hostile.excerpt}{hostile.title}{hostile.author}"
    assert hostile.excerpt.startswith(BODY)


async def test_encodable_text_is_stored_verbatim():
    text = f"{BODY} Café — naïve \U0001f600 \x00 kept as crawled."
    result = await _discover(markdown=text, title="Café \U0001f600")

    [hostile] = [ev for ev in result.evidence if ev.url == HOSTILE]
    assert hostile.excerpt == text
    assert hostile.title == "Café \U0001f600"
