"""API key generation and validation.

Keys are 32 random bytes (via libsodium), URL-safe base64 encoded.
Only the SHA-256 hash is stored — the raw key is shown once at generation.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Coroutine
from typing import Any

import nacl.utils
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cce.jobs.store import JobStore

security = HTTPBearer(auto_error=False)


def generate_api_key() -> str:
    """Generate a 32-byte URL-safe base64 API key."""
    raw = nacl.utils.random(32)
    return base64.urlsafe_b64encode(raw).decode()


def hash_api_key(key: str) -> str:
    """SHA-256 hash of an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


def make_auth_dependency(
    require_auth: bool,
    job_store: JobStore,
) -> Callable[..., Coroutine[Any, Any, str | None]]:
    """Build a FastAPI-compatible auth dependency.

    Returns an async callable that:
    - If require_auth is False: always returns None (no check).
    - Otherwise: extracts bearer token, hashes it, validates against
      the job store. Returns the key hash on success, raises 401 on failure.
    """

    async def _check_auth(
        credentials: HTTPAuthorizationCredentials | None = security,  # type: ignore[assignment]
    ) -> str | None:
        if not require_auth:
            return None

        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="Missing authentication token",
            )

        key_hash = hash_api_key(credentials.credentials)
        if not await job_store.verify_api_key(key_hash):
            raise HTTPException(
                status_code=401,
                detail="Invalid API key",
            )
        return key_hash

    return _check_auth
