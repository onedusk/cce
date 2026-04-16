"""Tests for API key generation, hashing, and auth dependency."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from cce.api.auth import generate_api_key, hash_api_key, make_auth_dependency
from cce.jobs.store import JobStore

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def test_generate_api_key_length():
    """32 bytes → 44-char URL-safe base64 (with padding)."""
    key = generate_api_key()
    assert len(key) == 44  # base64 of 32 bytes = ceil(32/3)*4 = 44


def test_generate_api_key_is_valid_base64():
    key = generate_api_key()
    decoded = base64.urlsafe_b64decode(key)
    assert len(decoded) == 32


def test_generate_api_key_unique():
    keys = {generate_api_key() for _ in range(10)}
    assert len(keys) == 10


# ---------------------------------------------------------------------------
# Key hashing
# ---------------------------------------------------------------------------


def test_hash_api_key_hex_length():
    """SHA-256 → 64-char hex string."""
    h = hash_api_key("test-key")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_api_key_deterministic():
    assert hash_api_key("same") == hash_api_key("same")


def test_hash_api_key_different_inputs():
    assert hash_api_key("key-a") != hash_api_key("key-b")


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


async def test_auth_disabled_returns_none():
    """require_auth=False → always returns None, no DB check."""
    store = AsyncMock(spec=JobStore)
    check = make_auth_dependency(require_auth=False, job_store=store)

    result = await check(credentials=None)
    assert result is None
    store.verify_api_key.assert_not_called()


async def test_auth_missing_token_raises_401():
    store = AsyncMock(spec=JobStore)
    check = make_auth_dependency(require_auth=True, job_store=store)

    with pytest.raises(HTTPException) as exc_info:
        await check(credentials=None)
    assert exc_info.value.status_code == 401
    assert "Missing" in exc_info.value.detail


async def test_auth_invalid_key_raises_401():
    store = AsyncMock(spec=JobStore)
    store.verify_api_key.return_value = False
    check = make_auth_dependency(require_auth=True, job_store=store)

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad-key")
    with pytest.raises(HTTPException) as exc_info:
        await check(credentials=creds)
    assert exc_info.value.status_code == 401
    assert "Invalid" in exc_info.value.detail


async def test_auth_valid_key_returns_hash():
    key = generate_api_key()
    expected_hash = hash_api_key(key)

    store = AsyncMock(spec=JobStore)
    store.verify_api_key.return_value = True
    check = make_auth_dependency(require_auth=True, job_store=store)

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)
    result = await check(credentials=creds)
    assert result == expected_hash
    store.verify_api_key.assert_called_once_with(expected_hash)


async def test_auth_full_roundtrip_with_real_store(tmp_path: Path):
    """Generate key → hash → store → verify via real JobStore."""
    store = JobStore(db_path=tmp_path / "auth_test.db")
    await store.connect()
    try:
        key = generate_api_key()
        key_hash = hash_api_key(key)
        await store.store_api_key(key_hash, label="roundtrip test")

        check = make_auth_dependency(require_auth=True, job_store=store)
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)
        result = await check(credentials=creds)
        assert result == key_hash
    finally:
        await store.close()
