"""Shared fixtures for API tests.

The ``job_store`` and ``sqlite_store`` fixtures come from the root conftest.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from cce.api.app import create_app
from cce.api.auth import generate_api_key, hash_api_key
from cce.config.types import APIConfig, EvidenceStoreConfig
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.jobs.store import JobStore
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import (
    MockCrawlAdapter,
    MockLLMProvider,
    make_engine_config,
    make_source_policy,
)


@pytest.fixture
def test_config(tmp_path: Path):
    """Minimal EngineConfig with auth disabled and temp DB paths."""
    return make_engine_config(
        evidence_store=EvidenceStoreConfig(
            sqlite_path=tmp_path / "test.db",
        ),
        api=APIConfig(require_auth=False),
    )


@pytest.fixture
async def evidence_store(tmp_path: Path) -> AsyncGenerator[SQLiteEvidenceStore, None]:
    """Real EvidenceStore on temp DB (separate from sqlite_store for API tests)."""
    config = EvidenceStoreConfig(sqlite_path=tmp_path / "test_evidence.db")
    store = SQLiteEvidenceStore(config)
    await store.connect()
    yield store
    await store.close()


@pytest.fixture
def mock_pipeline(test_config, evidence_store: SQLiteEvidenceStore) -> Pipeline:
    """Real Pipeline with mock LLM + mock crawl adapter."""
    return Pipeline(
        config=test_config,
        crawl_adapter=MockCrawlAdapter(),
        evidence_store=evidence_store,
        llm=MockLLMProvider(responses=[]),
    )


@pytest.fixture
def test_policies():
    """Minimal policy set for tests."""
    return {"test-policy": make_source_policy()}


@pytest.fixture
async def app(
    test_config,
    job_store: JobStore,
    evidence_store: SQLiteEvidenceStore,
    mock_pipeline: Pipeline,
    test_policies,
) -> AsyncGenerator[FastAPI, None]:
    """FastAPI app wired with test mocks. Lifespan is triggered."""
    _app = create_app(
        config=test_config,
        job_store=job_store,
        evidence_store=evidence_store,
        pipeline=mock_pipeline,
        policies=test_policies,
    )
    async with _app.router.lifespan_context(_app):
        yield _app


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client for the test app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.fixture
async def auth_client(
    app: FastAPI,
    job_store: JobStore,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client with a valid API key in the Authorization header."""
    key = generate_api_key()
    key_hash = hash_api_key(key)
    await job_store.store_api_key(key_hash, label="test-fixture")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {key}"},
    ) as c:
        yield c
