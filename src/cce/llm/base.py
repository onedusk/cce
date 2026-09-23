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


# Stop reasons after which the reply is incomplete and must not be parsed.
_INCOMPLETE_STOP_REASONS = frozenset({"max_tokens", "refusal"})


class IncompleteResponseError(RuntimeError):
    """An LLM reply stopped before the model finished its answer (B2).

    Raised by every caller that parses a reply (writer, verifier, editor,
    implied-claim checker) instead of parsing a truncated or refused reply,
    which used to degrade silently into uncited markdown or a zero-score
    verdict. Deliberately not a ValueError: ``with_llm_retry`` retries those,
    and resending with the same token budget would most likely truncate
    again. The pipeline records it as a FAILED job whose error names the role.
    """

    def __init__(self, role: str, response: LLMResponse) -> None:
        self.role = role
        self.stop_reason = response.stop_reason
        self.model = response.model
        if response.stop_reason == "max_tokens":
            detail = (
                f"stopped at max_tokens after "
                f"{response.usage.get('output_tokens', 0)} output tokens "
                "(thinking included). Raise llm.max_tokens / CCE_LLM_MAX_TOKENS "
                "(verifier: verifier.max_tokens / CCE_VERIFIER_MAX_TOKENS); the "
                "SDK refuses non-streaming max_tokens above ~21,333"
            )
        else:
            detail = f"ended with stop_reason={response.stop_reason!r}"
        super().__init__(
            f"{role} reply from {response.model or 'unknown model'} {detail}; "
            "reply not parsed"
        )


def ensure_complete(response: LLMResponse, *, role: str) -> None:
    """Raise IncompleteResponseError if ``response`` stopped early (B2)."""
    if response.stop_reason in _INCOMPLETE_STOP_REASONS:
        raise IncompleteResponseError(role, response)


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
                    Providers omit it for models that reject sampling
                    parameters.
            max_tokens: Override the default max_tokens for this call.
            system: System prompt. Passed separately because some providers
                    handle it differently from user messages.
        """
        ...
