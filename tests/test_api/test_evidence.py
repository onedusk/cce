"""Tests for evidence retrieval endpoints."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import make_evidence

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — seed evidence store before tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _seed_evidence(evidence_store):
    """Pre-populate the evidence store with test data."""
    await evidence_store.put(
        make_evidence(
            id="ev_alpha",
            url="https://example.com/alpha",
            title="Alpha Article",
            excerpt="Alpha content about sleep research with enough length to pass checks.",
        )
    )
    await evidence_store.put(
        make_evidence(
            id="ev_beta",
            url="https://example.com/beta",
            title="Beta Article",
            excerpt="Beta content about stress and anxiety with enough length to pass checks.",
        )
    )
    await evidence_store.put(
        make_evidence(
            id="ev_gamma",
            url="https://other.org/gamma",
            title="Gamma Study",
            excerpt="Gamma content about sleep patterns and circadian rhythms for testing.",
        )
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_evidence_by_id(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/evidence/ev_alpha")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["id"] == "ev_alpha"
    assert data["url"] == "https://example.com/alpha"
    assert data["title"] == "Alpha Article"


async def test_get_evidence_not_found(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/evidence/nonexistent")
    assert resp.status_code == 404


async def test_search_evidence_by_url(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/evidence?url=https://example.com")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2
    urls = {ev["url"] for ev in data}
    assert "https://example.com/alpha" in urls
    assert "https://example.com/beta" in urls


async def test_search_evidence_by_topic(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/evidence?topic=sleep")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) >= 1
    # Both alpha and gamma mention sleep
    ids = {ev["id"] for ev in data}
    assert "ev_alpha" in ids


async def test_search_evidence_with_limit(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/evidence?limit=1")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1


async def test_search_evidence_no_filters(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/evidence")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 3
