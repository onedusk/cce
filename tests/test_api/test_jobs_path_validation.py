"""Tests for early `body.paths` validation in POST /v1/curate/jobs (audit U2, T-07.03).

When app.state.path_configs is populated, the handler validates each path
in the request body against the registered set BEFORE engine dispatch.
Unknown paths produce a 400 with error.code="unknown_paths" and a list of
the unknown + known paths in `meta` — clean operator UX rather than a
500 from deep inside the pipeline.

When no path_configs are loaded (runner use case), the handler accepts any
path name and lets the pipeline route it.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from cce.models.paths import PathConfig
from tests.test_api.test_jobs_lifecycle import _make_lifecycle_app

pytestmark = pytest.mark.integration


def _path_configs_dict(known_paths: list[str]) -> dict:
    return {name: PathConfig(id=name, name=name.capitalize()) for name in known_paths}


async def test_unknown_paths_returns_400_with_code(tmp_path):
    app, job_store, evidence_store = await _make_lifecycle_app(tmp_path)
    try:
        async with app.router.lifespan_context(app):
            # Lifespan startup sets path_configs from the pipeline;
            # override here so the handler validates against our set.
            app.state.path_configs = _path_configs_dict(["blog", "summary", "faq"])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/curate/jobs",
                    json={
                        "topic": "test",
                        "paths": ["blog", "nonexistent"],
                        "policy_id": "test-policy",
                    },
                )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "unknown_paths"
        assert body["meta"]["unknown"] == ["nonexistent"]
        assert body["meta"]["known"] == ["blog", "faq", "summary"]
    finally:
        await job_store.close()
        await evidence_store.close()


async def test_multiple_unknown_paths_all_reported(tmp_path):
    app, job_store, evidence_store = await _make_lifecycle_app(tmp_path)
    try:
        async with app.router.lifespan_context(app):
            app.state.path_configs = _path_configs_dict(["blog"])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/curate/jobs",
                    json={
                        "topic": "test",
                        "paths": ["alpha", "beta", "gamma"],
                        "policy_id": "test-policy",
                    },
                )
        assert resp.status_code == 400
        body = resp.json()
        assert body["meta"]["unknown"] == ["alpha", "beta", "gamma"]
    finally:
        await job_store.close()
        await evidence_store.close()


async def test_all_known_paths_proceed(tmp_path):
    """When every path is registered, request proceeds to engine dispatch."""
    app, job_store, evidence_store = await _make_lifecycle_app(tmp_path)
    try:
        async with app.router.lifespan_context(app):
            app.state.path_configs = _path_configs_dict(["blog", "summary"])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/curate/jobs",
                    json={
                        "topic": "test",
                        "paths": ["blog"],
                        "policy_id": "test-policy",
                    },
                )
        assert resp.status_code == 202
    finally:
        await job_store.close()
        await evidence_store.close()


async def test_no_path_configs_skips_validation(tmp_path):
    """When app.state.path_configs is None, any path name is accepted."""
    app, job_store, evidence_store = await _make_lifecycle_app(tmp_path)
    # Explicitly clear path_configs to simulate the no-config-loaded case.
    app.state.path_configs = None
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/curate/jobs",
                    json={
                        "topic": "test",
                        "paths": ["any-arbitrary-path"],
                        "policy_id": "test-policy",
                    },
                )
        # Request was accepted — validation was skipped because no path_configs
        # were loaded.
        assert resp.status_code == 202
    finally:
        await job_store.close()
        await evidence_store.close()
