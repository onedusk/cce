"""Anthropic LLM adapter.

Wraps the Anthropic Python SDK. Phase 1 default provider.
"""

from __future__ import annotations

import logging

import anthropic

from cce.config.types import LLMConfig
from cce.llm.base import LLMMessage, LLMResponse

logger = logging.getLogger(__name__)


class AnthropicProvider:
    """Async Anthropic API client."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client = anthropic.AsyncAnthropic(api_key=config.api_key, max_retries=2)

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        """Call the Anthropic messages API."""
        # Convert to Anthropic message format
        api_messages = [
            {"role": msg.role, "content": msg.content}
            for msg in messages
            if msg.role != "system"
        ]

        kwargs: dict = {
            "model": self._config.model,
            "messages": api_messages,
            "temperature": temperature if temperature is not None else self._config.temperature,
            "max_tokens": max_tokens or self._config.max_tokens,
        }

        # System prompt: prefer explicit arg, fall back to any system message in the list
        sys_prompt = system
        if sys_prompt is None:
            sys_messages = [m for m in messages if m.role == "system"]
            if sys_messages:
                sys_prompt = sys_messages[0].content
        if sys_prompt:
            kwargs["system"] = sys_prompt

        logger.debug(
            "Anthropic call: model=%s, messages=%d, max_tokens=%d",
            kwargs["model"],
            len(api_messages),
            kwargs["max_tokens"],
        )

        response = await self._client.messages.create(**kwargs)

        # Extract text from response content blocks
        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        return LLMResponse(
            content=content,
            model=response.model,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            stop_reason=response.stop_reason or "",
        )
