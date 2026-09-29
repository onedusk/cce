"""Tests for AnthropicProvider with mocked SDK."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cce.config.loader import ConfigError
from cce.config.types import LLMConfig
from cce.llm.anthropic import AnthropicProvider
from cce.llm.base import LLMMessage
from tests.conftest import route_stream_to_create

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config() -> LLMConfig:
    return LLMConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key="test-key",
        temperature=0.5,
        max_tokens=1024,
    )


def _mock_response(
    text: str = "Hello, world!",
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 10,
    output_tokens: int = 20,
    stop_reason: str = "end_turn",
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> MagicMock:
    """Build a mock Anthropic Message response object."""
    block = MagicMock()
    block.text = text

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = cache_creation_input_tokens
    usage.cache_read_input_tokens = cache_read_input_tokens

    response = MagicMock()
    response.content = [block]
    response.model = model
    response.usage = usage
    response.stop_reason = stop_reason
    return response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_complete_success(mock_cls: MagicMock) -> None:
    """complete() returns LLMResponse with correct content, model, usage."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(
        return_value=_mock_response(
            text="Test output",
            model="claude-sonnet-4-6",
            input_tokens=15,
            output_tokens=25,
        )
    )
    mock_cls.return_value = mock_client

    provider = AnthropicProvider(_config())
    result = await provider.complete(
        [LLMMessage(role="user", content="Say hello")],
    )

    assert result.content == "Test output"
    assert result.model == "claude-sonnet-4-6"
    assert result.usage["input_tokens"] == 15
    assert result.usage["output_tokens"] == 25
    assert result.usage["cache_creation_input_tokens"] == 0
    assert result.usage["cache_read_input_tokens"] == 0
    assert result.stop_reason == "end_turn"


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_system_prompt_passed(mock_cls: MagicMock) -> None:
    """Explicit system kwarg is forwarded to the SDK call."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    provider = AnthropicProvider(_config())
    await provider.complete(
        [LLMMessage(role="user", content="Hi")],
        system="You are a helpful assistant.",
    )

    call_kwargs = mock_client.messages.create.call_args[1]
    # System prompt is now a list of content blocks with cache_control
    assert isinstance(call_kwargs["system"], list)
    assert len(call_kwargs["system"]) == 1
    assert call_kwargs["system"][0]["text"] == "You are a helpful assistant."
    assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_temperature_override(mock_cls: MagicMock) -> None:
    """Explicit temperature overrides the config default."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    config = _config()
    assert config.temperature == 0.5  # sanity check

    provider = AnthropicProvider(config)
    await provider.complete(
        [LLMMessage(role="user", content="Hi")],
        temperature=0.9,
    )

    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["temperature"] == 0.9


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_config_defaults_used(mock_cls: MagicMock) -> None:
    """When no overrides are given, temp and max_tokens come from config."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    config = _config()
    provider = AnthropicProvider(config)
    await provider.complete(
        [LLMMessage(role="user", content="Hi")],
    )

    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["temperature"] == config.temperature
    assert call_kwargs["max_tokens"] == config.max_tokens
    assert call_kwargs["model"] == config.model


@pytest.mark.parametrize(
    ("model", "sends_temperature"),
    [
        ("claude-sonnet-4-6", True),
        ("claude-opus-4-6", True),
        ("claude-haiku-4-5", True),
        ("claude-sonnet-4-5", True),
        ("claude-sonnet-5", False),
        ("claude-opus-5", False),
        ("claude-opus-5-5", False),
        ("claude-opus-4-7", False),
        ("claude-opus-4-8", False),
        ("claude-fable-5-1", False),
    ],
)
@pytest.mark.parametrize("explicit_temperature", [0.3, None])
@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_sampling_params_follow_model_capability(
    mock_cls: MagicMock,
    explicit_temperature: float | None,
    model: str,
    sends_temperature: bool,
) -> None:
    """B1: temperature is sent only to models that accept sampling params.

    Covers both the caller-supplied value and the LLMConfig fallback (None),
    so the rule applies to the final kwarg, not just the caller's value.
    """
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    config = _config().model_copy(update={"model": model})
    provider = AnthropicProvider(config)
    await provider.complete(
        [LLMMessage(role="user", content="Hi")],
        temperature=explicit_temperature,
    )

    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["model"] == model
    if sends_temperature:
        expected = (
            explicit_temperature
            if explicit_temperature is not None
            else config.temperature
        )
        assert call_kwargs["temperature"] == expected
    else:
        assert "temperature" not in call_kwargs
        assert "top_p" not in call_kwargs


@pytest.mark.parametrize(
    ("model", "accepts_adaptive"),
    [
        ("claude-sonnet-5", True),
        ("claude-opus-5", True),
        ("claude-opus-4-7", True),
        ("claude-sonnet-4-6", True),
        ("claude-opus-4-6", True),
        ("claude-haiku-4-5", False),
        ("claude-sonnet-4-5", False),
    ],
)
@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_thinking_and_effort_follow_model_capability(
    mock_cls: MagicMock, model: str, accepts_adaptive: bool
) -> None:
    """B2: explicit thinking/effort are sent only to models that support
    adaptive thinking and effort; Haiku 4.5 and older get neither."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    config = _config().model_copy(
        update={"model": model, "thinking": "adaptive", "effort": "medium"}
    )
    await AnthropicProvider(config).complete([LLMMessage(role="user", content="Hi")])

    call_kwargs = mock_client.messages.create.call_args[1]
    if accepts_adaptive:
        assert call_kwargs["thinking"] == {"type": "adaptive"}
        assert call_kwargs["output_config"] == {"effort": "medium"}
    else:
        assert "thinking" not in call_kwargs
        assert "output_config" not in call_kwargs


@pytest.mark.parametrize("model", ["claude-opus-4-5", "claude-opus-4-5-20251101"])
@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_opus_4_5_gets_effort_but_not_thinking(
    mock_cls: MagicMock, model: str
) -> None:
    """Opus 4.5 accepts output_config.effort but not adaptive thinking, so
    effort is sent and thinking stays omitted."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    config = _config().model_copy(
        update={"model": model, "thinking": "adaptive", "effort": "medium"}
    )
    await AnthropicProvider(config).complete([LLMMessage(role="user", content="Hi")])

    call_kwargs = mock_client.messages.create.call_args[1]
    assert "thinking" not in call_kwargs
    assert call_kwargs["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize("effort", ["xhigh", "max"])
@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
def test_opus_4_5_rejects_xhigh_and_max_effort(
    mock_cls: MagicMock, effort: str
) -> None:
    """Now that effort reaches Opus 4.5, the levels it rejects fail fast."""
    config = _config().model_copy(update={"model": "claude-opus-4-5", "effort": effort})
    with pytest.raises(ConfigError, match=f"effort: {effort}.*claude-opus-4-5"):
        AnthropicProvider(config)


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
def test_opus_4_5_accepts_low_to_high_effort(mock_cls: MagicMock, effort: str) -> None:
    config = _config().model_copy(update={"model": "claude-opus-4-5", "effort": effort})
    AnthropicProvider(config)


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-sonnet-4-6"])
@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_thinking_and_effort_omitted_by_default(
    mock_cls: MagicMock, model: str
) -> None:
    """B2: unset thinking/effort leave the params out (model default), so
    the 4.6 models keep today's no-thinking behaviour."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    config = _config().model_copy(update={"model": model})
    await AnthropicProvider(config).complete([LLMMessage(role="user", content="Hi")])

    call_kwargs = mock_client.messages.create.call_args[1]
    assert "thinking" not in call_kwargs
    assert "output_config" not in call_kwargs


@pytest.mark.parametrize(
    ("thinking", "sends_temperature"),
    [("adaptive", False), ("disabled", True), (None, True)],
)
@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_adaptive_thinking_drops_temperature_on_4_6(
    mock_cls: MagicMock, thinking: str | None, sends_temperature: bool
) -> None:
    """With thinking on, the API rejects any temperature but 1 even on the
    4.6 models (live 400, 2026-09-23), so the provider omits it there too."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    config = _config().model_copy(
        update={"model": "claude-sonnet-4-6", "thinking": thinking}
    )
    await AnthropicProvider(config).complete(
        [LLMMessage(role="user", content="Hi")], temperature=0.2
    )

    assert ("temperature" in mock_client.messages.create.call_args[1]) is (
        sends_temperature
    )


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_disabled_thinking_passes_through(mock_cls: MagicMock) -> None:
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    config = _config().model_copy(
        update={"model": "claude-sonnet-5", "thinking": "disabled"}
    )
    await AnthropicProvider(config).complete([LLMMessage(role="user", content="Hi")])

    assert mock_client.messages.create.call_args[1]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        ("claude-fable-5", None),
        ("claude-fable-5-1", "low"),
        ("claude-mythos-5-1", None),
        ("claude-opus-5-5", "low"),
        ("claude-opus-5", "xhigh"),
        ("claude-opus-5", "max"),
    ],
)
@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
def test_disabled_thinking_rejected_at_construction(
    mock_cls: MagicMock, model: str, effort: str | None
) -> None:
    """Models whose API rejects `thinking: disabled` fail fast with a
    ConfigError naming the model and the setting, not a 400 per call."""
    config = _config().model_copy(
        update={"model": model, "thinking": "disabled", "effort": effort}
    )
    with pytest.raises(ConfigError, match=model) as exc:
        AnthropicProvider(config)
    assert "thinking: disabled" in str(exc.value)


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        ("claude-opus-5", None),
        ("claude-opus-5", "high"),
        ("claude-opus-4-8", "max"),
        ("claude-sonnet-5", "xhigh"),
        ("claude-future-9", "max"),
    ],
)
@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
def test_disabled_thinking_allowed_elsewhere(
    mock_cls: MagicMock, model: str, effort: str | None
) -> None:
    """Opus 5 at effort high or below, and models on no list, keep it."""
    config = _config().model_copy(
        update={"model": model, "thinking": "disabled", "effort": effort}
    )
    AnthropicProvider(config)


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_sdk_exception_propagates(mock_cls: MagicMock) -> None:
    """RuntimeError raised by the SDK propagates to the caller."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(side_effect=RuntimeError("API down"))
    mock_cls.return_value = mock_client

    provider = AnthropicProvider(_config())
    with pytest.raises(RuntimeError, match="API down"):
        await provider.complete(
            [LLMMessage(role="user", content="Hi")],
        )


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_system_message_extracted_from_list(mock_cls: MagicMock) -> None:
    """LLMMessage with role='system' becomes the system kwarg and is excluded from api_messages."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    provider = AnthropicProvider(_config())
    await provider.complete(
        [
            LLMMessage(role="system", content="Be concise."),
            LLMMessage(role="user", content="What is 2+2?"),
        ],
    )

    call_kwargs = mock_client.messages.create.call_args[1]
    # System message extracted into the system kwarg as cached content block
    assert isinstance(call_kwargs["system"], list)
    assert call_kwargs["system"][0]["text"] == "Be concise."
    assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    # System message excluded from the messages list
    api_messages = call_kwargs["messages"]
    assert len(api_messages) == 1
    assert api_messages[0]["role"] == "user"
    # Content is now a list of content blocks
    assert isinstance(api_messages[0]["content"], list)
    assert api_messages[0]["content"][0]["text"] == "What is 2+2?"


# ---------------------------------------------------------------------------
# Prompt caching: _split_for_cache
# ---------------------------------------------------------------------------


def test_split_for_cache_no_marker() -> None:
    """Content without evidence markers returns a single uncached block."""
    blocks = AnthropicProvider._split_for_cache("Just a plain message.")
    assert len(blocks) == 1
    assert blocks[0]["text"] == "Just a plain message."
    assert "cache_control" not in blocks[0]


def test_split_for_cache_writer_marker() -> None:
    """Writer-style evidence end marker splits into cached prefix + uncached suffix."""
    content = (
        "Topic: sleep\n"
        "=== EVIDENCE START ===\n"
        "[ev_001] Some evidence\n"
        "=== EVIDENCE END ===\n"
        "\n"
        "=== VERIFIER FEEDBACK ===\nFix claim 3.\n=== END FEEDBACK ==="
    )
    blocks = AnthropicProvider._split_for_cache(content)
    assert len(blocks) == 2
    # Prefix: everything up to and including the evidence end marker
    assert blocks[0]["text"].endswith("=== EVIDENCE END ===")
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    # Suffix: feedback portion, no cache_control
    assert "VERIFIER FEEDBACK" in blocks[1]["text"]
    assert "cache_control" not in blocks[1]


def test_split_for_cache_verifier_marker() -> None:
    """Verifier-style evidence end marker also splits correctly."""
    content = (
        "=== DRAFT CONTENT ===\nSome draft\n=== END DRAFT ===\n\n"
        "=== EVIDENCE AVAILABLE ===\n[ev_001] Evidence\n=== END EVIDENCE ===\n\n"
        "Verify every factual claim."
    )
    blocks = AnthropicProvider._split_for_cache(content)
    assert len(blocks) == 2
    assert blocks[0]["text"].endswith("=== END EVIDENCE ===")
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "Verify every factual claim" in blocks[1]["text"]
    assert "cache_control" not in blocks[1]


def test_split_for_cache_no_suffix() -> None:
    """When evidence marker is at the end, only one cached block is returned."""
    content = "Evidence here\n=== EVIDENCE END ==="
    blocks = AnthropicProvider._split_for_cache(content)
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_cache_tokens_reported(mock_cls: MagicMock) -> None:
    """Cache token fields are included in the usage dict."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(
        return_value=_mock_response(
            cache_creation_input_tokens=500,
            cache_read_input_tokens=1200,
        )
    )
    mock_cls.return_value = mock_client

    provider = AnthropicProvider(_config())
    result = await provider.complete(
        [LLMMessage(role="user", content="Hi")],
    )

    assert result.usage["cache_creation_input_tokens"] == 500
    assert result.usage["cache_read_input_tokens"] == 1200


_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("model", "sends_format"),
    [
        ("claude-sonnet-5", True),
        ("claude-opus-5", True),
        ("claude-sonnet-4-6", True),
        ("claude-haiku-4-5", True),
        ("claude-sonnet-4-5-20250929", True),
        ("claude-3-5-sonnet-latest", False),
        ("claude-sonnet-4-20250514", False),
    ],
)
@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_output_schema_sent_as_structured_output(
    mock_cls: MagicMock, model: str, sends_format: bool
) -> None:
    """output_schema becomes output_config.format on models with structured
    outputs (every model the Models API lists); retired ones get none."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    config = _config().model_copy(update={"model": model})
    await AnthropicProvider(config).complete(
        [LLMMessage(role="user", content="Hi")], output_schema=_SCHEMA
    )

    call_kwargs = mock_client.messages.create.call_args[1]
    if sends_format:
        assert call_kwargs["output_config"] == {
            "format": {"type": "json_schema", "schema": _SCHEMA}
        }
    else:
        assert "output_config" not in call_kwargs


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_output_schema_merges_with_effort(mock_cls: MagicMock) -> None:
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    config = _config().model_copy(update={"model": "claude-sonnet-5", "effort": "low"})
    await AnthropicProvider(config).complete(
        [LLMMessage(role="user", content="Hi")], output_schema=_SCHEMA
    )

    assert mock_client.messages.create.call_args[1]["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": _SCHEMA},
    }


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_no_output_config_without_schema_or_effort(mock_cls: MagicMock) -> None:
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    config = _config().model_copy(update={"model": "claude-sonnet-5"})
    await AnthropicProvider(config).complete([LLMMessage(role="user", content="Hi")])

    assert "output_config" not in mock_client.messages.create.call_args[1]


# ---------------------------------------------------------------------------
# Streaming and the model's own max_tokens
# ---------------------------------------------------------------------------


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_requests_are_streamed(mock_cls: MagicMock) -> None:
    """Non-streaming requests above ~21,333 max_tokens are refused by the SDK,
    so every request goes through messages.stream."""
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client
    config = _config().model_copy(update={"max_tokens": 64000})

    result = await AnthropicProvider(config).complete(
        [LLMMessage(role="user", content="hi")]
    )

    assert result.content == "Hello, world!"
    assert mock_client.messages.create.call_args[1]["max_tokens"] == 64000


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_unset_max_tokens_uses_the_models_maximum(mock_cls: MagicMock) -> None:
    from cce.llm import anthropic as provider_module

    provider_module._MODEL_MAX_TOKENS.clear()
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_client.models.retrieve = AsyncMock(return_value=MagicMock(max_tokens=128000))
    mock_cls.return_value = mock_client
    provider = AnthropicProvider(_config().model_copy(update={"max_tokens": None}))

    for _ in range(2):
        await provider.complete([LLMMessage(role="user", content="hi")])

    assert mock_client.messages.create.call_args[1]["max_tokens"] == 128000
    mock_client.models.retrieve.assert_awaited_once_with("claude-sonnet-4-6")
    provider_module._MODEL_MAX_TOKENS.clear()


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_failed_model_lookup_falls_back(mock_cls: MagicMock, caplog) -> None:
    from cce.llm import anthropic as provider_module

    provider_module._MODEL_MAX_TOKENS.clear()
    mock_client = MagicMock()
    route_stream_to_create(mock_client)
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_client.models.retrieve = AsyncMock(side_effect=RuntimeError("offline"))
    mock_cls.return_value = mock_client
    provider = AnthropicProvider(_config().model_copy(update={"max_tokens": None}))

    await provider.complete([LLMMessage(role="user", content="hi")])

    assert (
        mock_client.messages.create.call_args[1]["max_tokens"]
        == provider_module.FALLBACK_MAX_TOKENS
    )
    assert "Could not read max_tokens" in caplog.text
    provider_module._MODEL_MAX_TOKENS.clear()
