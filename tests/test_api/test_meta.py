"""Tests for health and meta endpoints."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from cce.models.paths import PathConfig
from cce.models.taxonomy import Dimension, TaxonomyConfig
from cce.orchestrator.pipeline import Pipeline
from cce.tagging.wellbeing import WellBeingTaxonomy
from tests.conftest import MockCrawlAdapter, MockLLMProvider

pytestmark = pytest.mark.unit


async def test_health_returns_ok(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/health")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "ok"
    assert data["db_reachable"] is True
    assert "engine_version" in data


async def test_meta_returns_engine_version(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/meta")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "engine_version" in data
    assert isinstance(data["engine_version"], str)


async def test_meta_lists_policies(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/meta")
    data = resp.json()["data"]
    assert "test-policy" in data["policies"]


async def test_meta_queue_depth_zero_when_idle(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/meta")
    data = resp.json()["data"]
    assert data["queue_depth"] == 0


async def test_meta_shows_adapters(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/meta")
    data = resp.json()["data"]
    assert "crawl" in data["adapters"]
    assert "llm" in data["adapters"]
    assert "embedding" in data["adapters"]


async def test_meta_lists_nothing_when_none_are_loaded(client: httpx.AsyncClient):
    data = (await client.get("/v1/curate/meta")).json()["data"]
    assert data["path_configs"] == []
    assert data["taxonomies"] == []


async def test_meta_lists_loaded_path_configs(app: FastAPI, client: httpx.AsyncClient):
    """CR-15: a client can discover the valid path names."""
    app.state.path_configs = {
        name: PathConfig(id=name, name=name.capitalize())
        for name in ("summary", "blog", "faq")
    }
    data = (await client.get("/v1/curate/meta")).json()["data"]
    assert data["path_configs"] == ["blog", "faq", "summary"]


async def test_meta_lists_the_loaded_taxonomy(
    app: FastAPI, client: httpx.AsyncClient, test_config, evidence_store
):
    """CR-15: the id of the taxonomy the pipeline tags with."""
    taxonomy = TaxonomyConfig(
        id="finance-2d",
        name="Finance",
        dimensions=[Dimension(id="risk", name="Risk", values=["primary", "none"])],
    )
    app.state.pipeline = Pipeline(
        config=test_config,
        crawl_adapter=MockCrawlAdapter(),
        evidence_store=evidence_store,
        llm=MockLLMProvider(responses=[]),
        taxonomy_plugin=WellBeingTaxonomy(taxonomy),
    )
    data = (await client.get("/v1/curate/meta")).json()["data"]
    assert data["taxonomies"] == ["finance-2d"]
