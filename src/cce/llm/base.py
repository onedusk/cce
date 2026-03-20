"""LLM provider protocol.

The writer and verifier agents call into this interface. They don't
know or care which provider is behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMMessage:
    """A single message in a conversation."""

    role: str  # "system", "user", "assistant"
    content: str


@dataclass(frozen=True)
class LLMResponse:
    """Response from an LLM call."""

    content: str
    model: str = ""
    usage: dict = field(default_factory=dict)  # token counts
    stop_reason: str = ""


@runtime_checkable
class LLMProvider(Protocol):
    """Interface for making LLM calls."""

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        """Send a conversation to the LLM and get a response.

        Args:
            messages: Conversation history (user/assistant turns).
            temperature: Override the default temperature for this call.
            max_tokens: Override the default max_tokens for this call.
            system: System prompt. Passed separately because some providers
                    handle it differently from user messages.
        """
        ...
