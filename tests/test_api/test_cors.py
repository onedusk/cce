"""CORS middleware hardening — wildcard origins must never ship with credentials.

Regression for the 2026-04-22 security review hardening recommendation:
Starlette's CORSMiddleware reflects the inbound Origin + emits
Access-Control-Allow-Credentials: true when allow_origins=["*"] is paired
with allow_credentials=True, defeating the browser's wildcard-vs-credentials
safety rule. Bearer-header auth doesn't travel on cross-origin fetches today,
but cookie auth would be exposed if ever added.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from cce.api.app import create_app
from cce.config.types import APIConfig, EvidenceStoreConfig
from cce.evidence.sqlite import SQLiteEvidenceStore
from cce.jobs.store import JobStore
from cce.orchestrator.pipeline import Pipeline
from tests.conftest import make_engine_config, make_source_policy

pytestmark = pytest.mark.unit


async def _app_with_cors(
    *,
    cors_origins: list[str],
    tmp_path: Path,
    job_store: JobStore,
    evidence_store: SQLiteEvidenceStore,
    mock_pipeline: Pipeline,
) -> AsyncGenerator[FastAPI, None]:
    config = make_engine_config(
        evidence_store=EvidenceStoreConfig(sqlite_path=tmp_path / "cors.db"),
        api=APIConfig(require_auth=False, cors_origins=cors_origins),
    )
    app = create_app(
        config=config,
        job_store=job_store,
        evidence_store=evidence_store,
        pipeline=mock_pipeline,
        policies={"test-policy": make_source_policy()},
    )
    async with app.router.lifespan_context(app):
        yield app


async def test_wildcard_origin_disables_credentials(
    tmp_path: Path,
    job_store: JobStore,
    evidence_store: SQLiteEvidenceStore,
    mock_pipeline: Pipeline,
) -> None:
    """allow_origins=["*"] ⇒ no Access-Control-Allow-Credentials header."""
    async for app in _app_with_cors(
        cors_origins=["*"],
        tmp_path=tmp_path,
        job_store=job_store,
        evidence_store=evidence_store,
        mock_pipeline=mock_pipeline,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/curate/health",
                headers={"Origin": "https://evil.example"},
            )
        assert resp.status_code == 200
        # Starlette only emits allow-credentials when the middleware is
        # configured with allow_credentials=True. With wildcard origins we
        # now disable it, so the header must be absent.
        assert "access-control-allow-credentials" not in {
            k.lower() for k in resp.headers
        }


async def test_explicit_origin_keeps_credentials(
    tmp_path: Path,
    job_store: JobStore,
    evidence_store: SQLiteEvidenceStore,
    mock_pipeline: Pipeline,
) -> None:
    """Explicit allowlist ⇒ Access-Control-Allow-Credentials: true."""
    allowed = "https://app.example.com"
    async for app in _app_with_cors(
        cors_origins=[allowed],
        tmp_path=tmp_path,
        job_store=job_store,
        evidence_store=evidence_store,
        mock_pipeline=mock_pipeline,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/curate/health",
                headers={"Origin": allowed},
            )
        assert resp.status_code == 200
        # With an explicit allowlist, credentials stay enabled so a
        # properly configured first-party front-end can still call the API
        # with cookies/authz headers.
        assert resp.headers.get("access-control-allow-credentials") == "true"
