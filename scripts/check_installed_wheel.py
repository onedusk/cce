"""Check an installed cce wheel the way a consumer runs it.

Run with the Python of an environment that has cce installed from a wheel,
with no lockfile, from a directory that is not the repo (CI does this). Uses
no network and no API keys. It fails when:

- the packaged data files are missing (the engine cannot boot from an empty
  working directory), or
- the request AnthropicProvider builds does not bind to the installed SDK's
  ``messages.stream`` signature (audit OPS-01: SDK 1.x dropped ``temperature``
  and every call on a model that takes it raised TypeError).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
from pathlib import Path

import anthropic
from anthropic.resources.messages import AsyncMessages

import cce
from cce.components import ComponentOverrides
from cce.config.types import LLMConfig
from cce.engine import CurationEngine
from cce.llm.anthropic import AnthropicProvider
from cce.llm.base import LLMMessage

MODELS = ("claude-sonnet-4-6", "claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5")


class _Bound(Exception):
    """Raised once the request has bound, in place of sending it."""


async def _check_request_binds(model: str) -> None:
    provider = AnthropicProvider(
        LLMConfig(api_key="not-a-key", model=model, max_tokens=1024, effort="low")
    )
    signature = inspect.signature(AsyncMessages.stream)

    def stream(**kwargs: object) -> object:
        signature.bind(None, **kwargs)
        raise _Bound

    provider._client.messages.stream = stream  # type: ignore[method-assign]
    with contextlib.suppress(_Bound):
        await provider.complete(
            [LLMMessage(role="user", content="hi")],
            system="s",
            temperature=0.1,
            output_schema={"type": "object", "properties": {}},
        )
        raise AssertionError("the request was never built")


class _FakeLLM:
    async def complete(self, messages: object, **kwargs: object) -> object:
        raise RuntimeError("not called")


class _FakeCrawl:
    async def crawl(self, request: object) -> None: ...

    async def crawl_many(self, requests: object) -> list:
        return []

    async def search(self, query: str, limit: int = 10) -> list[str]:
        return []


async def main() -> None:
    assert "site-packages" in cce.__file__, f"cce is not installed: {cce.__file__}"
    assert not Path("config").exists(), "run from a directory with no config/"

    for model in MODELS:
        await _check_request_binds(model)

    engine = await CurationEngine.embedded(
        overrides=ComponentOverrides(llm=_FakeLLM(), crawl_adapter=_FakeCrawl())
    )
    try:
        pipeline = engine._pipeline
        assert pipeline is not None
        assert pipeline._scorer is not None, "humanization markers did not load"
        assert pipeline._pricing, "model price table did not load"
    finally:
        await engine.close()

    print(f"ok: cce wheel with anthropic {anthropic.__version__}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # one line for the CI log
        sys.exit(f"installed-wheel check failed: {type(e).__name__}: {e}")
