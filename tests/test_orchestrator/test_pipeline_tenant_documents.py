"""Tenant document adapters never cross tenants in a shared ComponentSet (SEC-01).

Companion to the B6 store-isolation test. Each tenant reads its own
documents through a ``CompositeCrawlAdapter`` (its ``local://`` adapter plus
the web adapter). ``CrawlAdapter.search`` takes no tenant, so putting one
tenant's composite in the shared set fed its documents into every tenant's
run. ``build_pipeline(..., crawl_adapter=...)`` gives each tenant its own
adapter while the LLM providers stay shared. The policy is allow-all
(empty ``domains_allow``) on purpose: it must not be what keeps them apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cce.components import ComponentOverrides, build_components, build_pipeline
from cce.config.registry import ConfigRegistry
from cce.config.types import (
    CrawlConfig,
    EmbeddingConfig,
    EngineConfig,
    EvidenceStoreConfig,
    HumanizationConfig,
    LLMConfig,
    QualityGateConfig,
)
from cce.discovery.adapters.composite import CompositeCrawlAdapter
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.llm.base import LLMResponse
from tests.conftest import (
    MockCrawlAdapter,
    MockLLMProvider,
    make_crawl_result,
    make_curation_request,
    make_source_policy,
)
from tests.test_discovery.test_composite_adapter import FakeLocalAdapter
from tests.test_orchestrator.conftest import verifier_json

pytestmark = pytest.mark.integration

WEB_URL = "https://example.com/sleep"
DOCS = {
    "a": ("local://tenant-a/plan.md", "TENANTA-SECRET the Q3 plan covers sleep."),
    "b": ("local://tenant-b/plan.md", "TENANTB-SECRET the Q4 plan covers sleep."),
}


def _composite(tenant: str, web: MockCrawlAdapter) -> CompositeCrawlAdapter:
    url, text = DOCS[tenant]
    return CompositeCrawlAdapter(
        {"local": FakeLocalAdapter({url: text + " " + "x" * 60})}, default=web
    )


def _one_run_script() -> list[LLMResponse]:
    writer = json.dumps(
        {
            "content": "Sleep matters [ev:ev_001].",
            "citations_used": ["ev_001"],
            "evidence_map": [{"claim": "c", "evidence_ids": ["ev_001"]}],
            "gaps": [],
        }
    )
    return [
        LLMResponse(content=writer, stop_reason="end_turn"),
        LLMResponse(
            content=verifier_json(supported=10, total=10, gaps=0),
            stop_reason="end_turn",
        ),
    ]


def _calls_text(calls: list[dict]) -> str:
    return "\n".join(
        (c["system"] or "") + "\n" + "\n".join(m.content for m in c["messages"])
        for c in calls
    )


async def test_tenant_document_adapters_stay_with_their_tenant(tmp_path: Path):
    config = EngineConfig(
        llm=LLMConfig(api_key=""),
        crawl=CrawlConfig(api_key=None),
        embedding=EmbeddingConfig(enabled=False),
        humanization=HumanizationConfig(enabled=False),
        quality_gate={"medium": QualityGateConfig(max_writer_iterations=1)},
    )
    registry = ConfigRegistry.load(
        Path("."),
        engine=config,
        taxonomies_dir=tmp_path / "no-taxonomies",
        path_configs_path=tmp_path / "no-path-configs.yaml",
    )
    web = MockCrawlAdapter(
        search_map={"test topic": [WEB_URL]},
        url_map={
            WEB_URL: make_crawl_result(
                url=WEB_URL,
                markdown="A regular schedule is the habit the sleep literature "
                "recommends most consistently.",
            )
        },
    )
    fake_llm = MockLLMProvider(
        _one_run_script() + _one_run_script(), cite_placeholders=True
    )
    # One shared set: tenant-neutral web adapter, one shared LLM.
    shared = build_components(
        config, registry, overrides=ComponentOverrides(llm=fake_llm, crawl_adapter=web)
    )
    stores = {
        t: SQLiteEvidenceStore(EvidenceStoreConfig(sqlite_path=tmp_path / f"{t}.db"))
        for t in DOCS
    }
    for store in stores.values():
        await store.connect()
    pipes = {
        t: build_pipeline(
            config, registry, stores[t], shared, crawl_adapter=_composite(t, web)
        )
        for t in DOCS
    }
    policy = make_source_policy()
    assert policy.domains_allow == []  # allow-all: the policy separates nothing

    try:
        for tenant, other in (("a", "b"), ("b", "a")):
            before = len(fake_llm.calls)
            result = await pipes[tenant].run(make_curation_request(), policy)
            text = _calls_text(fake_llm.calls[before:])
            own_url, own_text = DOCS[tenant]
            other_url, other_text = DOCS[other]
            secret, other_secret = own_text.split()[0], other_text.split()[0]

            assert result.package is not None
            urls = {ev.url for ev in result.package.evidence}
            # Positive control: the tenant reads its own document...
            assert own_url in urls and secret in text
            assert await stores[tenant].search(url=own_url)
            # ...and never the other tenant's: prompts, package or store.
            assert other_url not in urls
            assert other_secret not in text and other_url not in text
            assert await stores[tenant].search(url=other_url) == []
        # Both tenants' runs went through the one shared LLM provider.
        assert len(fake_llm.calls) == 4
    finally:
        for store in stores.values():
            await store.close()


def test_crawl_adapter_and_an_override_adapter_together_raise(tmp_path: Path):
    config = EngineConfig(llm=LLMConfig(api_key=""), crawl=CrawlConfig(api_key=None))
    with pytest.raises(ValueError, match="crawl_adapter or in overrides"):
        build_pipeline(
            config,
            ConfigRegistry.load(Path("."), engine=config),
            SQLiteEvidenceStore(EvidenceStoreConfig(sqlite_path=tmp_path / "e.db")),
            overrides=ComponentOverrides(crawl_adapter=MockCrawlAdapter()),
            crawl_adapter=MockCrawlAdapter(),
        )
