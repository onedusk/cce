"""CR-03: request constraints travel through the API create route.

JobCreateRequest had no constraint fields and the route built constraints
from jurisdiction only, so date bounds and domain lists sent over REST (or by
a remote CurationEngine) were dropped before the job was stored.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.unit

BODY = {"topic": "test topic", "paths": ["blog"], "policy_id": "test-policy"}
CONSTRAINTS = {
    "date_from": "2020-01-01",
    "date_to": "2024-12-31T00:00:00Z",
    "domains_allow": ["good.example"],
    "domains_deny": ["competitor.example"],
    "jurisdiction": "EU",
}


async def _stored_constraints(client: httpx.AsyncClient, body: dict):
    resp = await client.post("/v1/curate/jobs", json=body)
    assert resp.status_code == 202, resp.text
    job = (await client.get(f"/v1/curate/jobs/{resp.json()['data']['id']}")).json()
    return job["data"]["request"]["constraints"]


async def test_constraints_are_stored_on_the_job(client: httpx.AsyncClient):
    assert await _stored_constraints(client, BODY | {"constraints": CONSTRAINTS}) == (
        CONSTRAINTS
    )


async def test_top_level_jurisdiction_still_works(client: httpx.AsyncClient):
    stored = await _stored_constraints(client, BODY | {"jurisdiction": "US"})
    assert stored["jurisdiction"] == "US"
    assert stored["domains_deny"] == []


async def test_top_level_jurisdiction_fills_an_unset_one(client: httpx.AsyncClient):
    body = BODY | {
        "jurisdiction": "US",
        "constraints": {"domains_deny": ["competitor.example"]},
    }
    stored = await _stored_constraints(client, body)
    assert stored["jurisdiction"] == "US"
    assert stored["domains_deny"] == ["competitor.example"]


async def test_no_constraints_stays_none(client: httpx.AsyncClient):
    assert await _stored_constraints(client, BODY) is None


async def test_conflicting_jurisdictions_are_a_422(client: httpx.AsyncClient):
    resp = await client.post(
        "/v1/curate/jobs",
        json=BODY | {"jurisdiction": "US", "constraints": {"jurisdiction": "EU"}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


async def test_unparseable_date_bound_is_a_422(client: httpx.AsyncClient):
    resp = await client.post(
        "/v1/curate/jobs",
        json=BODY | {"constraints": {"date_from": "last year"}},
    )
    assert resp.status_code == 422
