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
                "(thinking included). Lower llm.effort / CCE_LLM_EFFORT so "
                "thinking takes less of the cap, or raise llm.max_tokens / "
                "CCE_LLM_MAX_TOKENS (verifier: CCE_VERIFIER_MAX_TOKENS) up to "
                "the SDK's non-streaming ceiling (~21,333; beyond it needs "
                "streaming)"
            )
        else:
            detail = f"ended with stop_reason={response.stop_reason!r}"
        super().__init__(
            f"{role} reply from {response.model or 'unknown model'} {detail}; "
            "reply not parsed"
        )


class UnparseableResponseError(ValueError):
    """A complete LLM reply could not be parsed into the expected JSON.

    Raised by the writer and verifier instead of falling back to raw
    markdown or a zero-score verdict. A ValueError, so ``with_llm_retry``
    resends; both callers allow one resend, then the error propagates and
    the pipeline fails the job.

    The reply text is kept on ``raw_response`` for the caller to persist
    where it sees fit: direct Writer/Verifier callers catch the error, and
    ``Pipeline.run`` hands it back in memory on ``PipelineResult.error``. It
    is never put in the message, which is logged and stored on the job
    record: a consumer's replies can hold confidential content.
    """

    def __init__(self, role: str, response: LLMResponse) -> None:
        self.role = role
        self.stop_reason = response.stop_reason
        self.model = response.model
        self.raw_response = response.content
        super().__init__(
            f"{role} reply from {response.model or 'unknown model'} could not "
            f"be parsed as a JSON object (stop_reason={response.stop_reason!r}, "
            f"{len(response.content)} chars); reply text is not logged or "
            "stored (in memory on the exception's raw_response)"
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
        output_schema: dict | None = None,
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
            output_schema: JSON schema the reply must follow. Providers that
                    can constrain output to it (structured outputs) do so;
                    others ignore it, and the caller parses as before.
        """
        ...
