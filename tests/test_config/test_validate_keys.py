"""Tests for cce.config.loader.validate_required_keys — fail-fast key validation."""

import pytest

from cce.config.loader import ConfigError, validate_required_keys
from cce.config.types import CrawlConfig, LLMConfig
from tests.conftest import make_engine_config

pytestmark = pytest.mark.unit


def test_missing_llm_key_raises_with_var_name():
    config = make_engine_config(llm=LLMConfig(api_key=""))
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        validate_required_keys(config)


def test_missing_llm_key_message_points_to_env_example():
    config = make_engine_config(llm=LLMConfig(api_key=""))
    with pytest.raises(ConfigError, match=r"see \.env\.example"):
        validate_required_keys(config)


def test_missing_crawl_key_raises_with_var_name():
    config = make_engine_config(crawl=CrawlConfig(api_key=None))
    with pytest.raises(ConfigError, match="FIRECRAWL_API_KEY"):
        validate_required_keys(config)


def test_missing_crawl_key_ok_when_crawl_not_required():
    config = make_engine_config(crawl=CrawlConfig(api_key=None))
    assert validate_required_keys(config, require_crawl=False) is None


def test_missing_llm_key_raises_even_when_crawl_not_required():
    config = make_engine_config(llm=LLMConfig(api_key=""))
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        validate_required_keys(config, require_crawl=False)


def test_both_keys_present_returns_none():
    config = make_engine_config()
    assert validate_required_keys(config) is None
