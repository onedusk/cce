"""Anthropic LLM adapter.

Wraps the Anthropic Python SDK. Phase 1 default provider.

Prompt caching: the provider automatically detects evidence block markers
in user messages and splits them into cached (static evidence) and uncached
(dynamic draft/feedback) content blocks. The system prompt is also cached.
This reduces input token costs by ~70% on multi-iteration write-verify loops.
See docs/internal/prompt-caching.md for design details.
"""

from __future__ import annotations

import logging

import anthropic

from cce.config.types import LLMConfig
from cce.llm.base import LLMMessage, LLMResponse

logger = logging.getLogger(__name__)

# Evidence block end markers used by writer and verifier prompts.
# The provider splits user messages at these boundaries for caching.
_EVIDENCE_END_MARKERS = [
    "=== EVIDENCE END ===",
    "=== END EVIDENCE ===",
]

# Model capability rules, keyed by model-ID prefix. Each lists the finite
# legacy set, so any model not matched is treated as current and a new
# release needs no code change.
#
# Models without adaptive thinking or effort (Haiku 4.5 and older): the
# `thinking` / `output_config.effort` settings are never sent to them (B2).
_PRE_ADAPTIVE_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-3",
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-opus-4-2025",
    "claude-sonnet-4-0",
    "claude-sonnet-4-2025",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
)
# Models that still accept sampling parameters (B1): the pre-adaptive set
# plus the 4.6 family. Every model from Opus 4.7 on (Opus 4.7/4.8/5,
# Sonnet 5, Fable 5) rejects `temperature` with a 400.
_SAMPLING_MODEL_PREFIXES: tuple[str, ...] = _PRE_ADAPTIVE_MODEL_PREFIXES + (
    "claude-opus-4-6",
    "claude-sonnet-4-6",
)
# Models without structured outputs (`output_config.format`): only retired
# ones. Every model the Models API listed on 2026-09-23 supports it, from
# Haiku 4.5 / Sonnet 4.5 / Opus 4.5 up.
_NO_STRUCTURED_OUTPUT_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-3",
    "claude-opus-4-0",
    "claude-opus-4-2025",
    "claude-sonnet-4-0",
    "claude-sonnet-4-2025",
)


class AnthropicProvider:
    """Async Anthropic API client with automatic prompt caching."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client = anthropic.AsyncAnthropic(api_key=config.api_key, max_retries=2)
        self._accepts_adaptive = not config.model.startswith(
            _PRE_ADAPTIVE_MODEL_PREFIXES
        )
        # With thinking on, even the 4.6 models reject any temperature but 1.
        thinking_on = self._accepts_adaptive and config.thinking == "adaptive"
        self._accepts_sampling = (
            config.model.startswith(_SAMPLING_MODEL_PREFIXES) and not thinking_on
        )
        self._accepts_output_schema = not config.model.startswith(
            _NO_STRUCTURED_OUTPUT_MODEL_PREFIXES
        )

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
        output_schema: dict | None = None,
    ) -> LLMResponse:
        """Call the Anthropic messages API with automatic prompt caching.

        The system prompt and the evidence block within user messages are
        marked for caching. On subsequent calls with the same prefix, the
        API charges ~10% of the normal input token cost for cached tokens.
        """
        # Convert to Anthropic message format with cache-aware content blocks
        api_messages = [
            {"role": msg.role, "content": self._split_for_cache(msg.content)}
            for msg in messages
            if msg.role != "system"
        ]

        kwargs: dict = {
            "model": self._config.model,
            "messages": api_messages,
            "max_tokens": max_tokens or self._config.max_tokens,
        }
        if self._accepts_sampling:
            kwargs["temperature"] = (
                temperature if temperature is not None else self._config.temperature
            )
        output_config: dict = {}
        if self._accepts_adaptive:
            if self._config.thinking is not None:
                kwargs["thinking"] = {"type": self._config.thinking}
            if self._config.effort is not None:
                output_config["effort"] = self._config.effort
        if output_schema is not None and self._accepts_output_schema:
            # Structured outputs: the reply is constrained to valid JSON
            # matching the schema, so it always parses.
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        if output_config:
            kwargs["output_config"] = output_config

        # System prompt: prefer explicit arg, fall back to any system message in the list
        sys_prompt = system
        if sys_prompt is None:
            sys_messages = [m for m in messages if m.role == "system"]
            if sys_messages:
                sys_prompt = sys_messages[0].content
        if sys_prompt:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": sys_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

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
                "cache_creation_input_tokens": getattr(
                    response.usage, "cache_creation_input_tokens", 0
                ),
                "cache_read_input_tokens": getattr(
                    response.usage, "cache_read_input_tokens", 0
                ),
            },
            stop_reason=response.stop_reason or "",
        )

    @staticmethod
    def _split_for_cache(content: str) -> list[dict]:
        """Split a message into cached (evidence) and uncached (dynamic) blocks.

        Detects evidence end markers used by the writer and verifier prompts.
        Everything up to and including the marker becomes a cached block;
        everything after (feedback, instructions) stays uncached.

        If no marker is found, returns the content as a single uncached block.
        """
        for marker in _EVIDENCE_END_MARKERS:
            idx = content.find(marker)
            if idx != -1:
                split_at = idx + len(marker)
                prefix = content[:split_at].strip()
                suffix = content[split_at:].strip()

                blocks: list[dict] = [
                    {
                        "type": "text",
                        "text": prefix,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                if suffix:
                    blocks.append({"type": "text", "text": suffix})
                return blocks

        # No evidence marker found -- return as plain content
        return [{"type": "text", "text": content}]
