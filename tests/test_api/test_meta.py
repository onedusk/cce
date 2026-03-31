"""Tests for health and meta endpoints."""

from __future__ import annotations

import httpx
import pytest


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
