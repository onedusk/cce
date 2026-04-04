"""Tests for AnthropicProvider with mocked SDK."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cce.config.types import LLMConfig
from cce.llm.anthropic import AnthropicProvider
from cce.llm.base import LLMMessage

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
) -> MagicMock:
    """Build a mock Anthropic Message response object."""
    block = MagicMock()
    block.text = text

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

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
    assert result.usage == {"input_tokens": 15, "output_tokens": 25}
    assert result.stop_reason == "end_turn"


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_system_prompt_passed(mock_cls: MagicMock) -> None:
    """Explicit system kwarg is forwarded to the SDK call."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_response())
    mock_cls.return_value = mock_client

    provider = AnthropicProvider(_config())
    await provider.complete(
        [LLMMessage(role="user", content="Hi")],
        system="You are a helpful assistant.",
    )

    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["system"] == "You are a helpful assistant."


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_temperature_override(mock_cls: MagicMock) -> None:
    """Explicit temperature overrides the config default."""
    mock_client = MagicMock()
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


@patch("cce.llm.anthropic.anthropic.AsyncAnthropic")
async def test_sdk_exception_propagates(mock_cls: MagicMock) -> None:
    """RuntimeError raised by the SDK propagates to the caller."""
    mock_client = MagicMock()
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
    # System message extracted into the system kwarg
    assert call_kwargs["system"] == "Be concise."
    # System message excluded from the messages list
    api_messages = call_kwargs["messages"]
    assert len(api_messages) == 1
    assert api_messages[0]["role"] == "user"
    assert api_messages[0]["content"] == "What is 2+2?"
