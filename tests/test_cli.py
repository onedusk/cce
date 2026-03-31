"""Tests for CLI commands — key management via typer.testing.CliRunner."""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from cce.cli import app

runner = CliRunner()

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Point the CLI at a temp DB so tests don't collide."""
    monkeypatch.setenv("CCE_EVIDENCE_SQLITE_PATH", str(tmp_path / "cli_test.db"))
    # Clear any other env vars that might interfere
    for var in ("ANTHROPIC_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_generate_key_outputs_key():
    result = runner.invoke(app, ["api", "key", "generate", "--label", "test"])
    assert result.exit_code == 0
    assert "API Key:" in result.output
    assert "Hash:" in result.output
    assert "Store this key securely" in result.output


def test_list_keys_empty():
    result = runner.invoke(app, ["api", "key", "list"])
    assert result.exit_code == 0
    assert "No API keys found" in result.output


def test_generate_then_list_shows_label():
    # Generate
    result = runner.invoke(app, ["api", "key", "generate", "--label", "my-key"])
    assert result.exit_code == 0

    # List
    result = runner.invoke(app, ["api", "key", "list"])
    assert result.exit_code == 0
    assert "my-key" in result.output


def test_generate_then_revoke():
    # Generate
    result = runner.invoke(app, ["api", "key", "generate", "--label", "doomed"])
    assert result.exit_code == 0
    # Extract hash prefix from output (format: "Hash:     abcdef0123456789...")
    for line in result.output.splitlines():
        if line.startswith("Hash:"):
            hash_prefix = line.split()[-1].rstrip(".")
            break

    # Revoke
    result = runner.invoke(app, ["api", "key", "revoke", hash_prefix])
    assert result.exit_code == 0
    assert "Revoked" in result.output

    # Verify gone
    result = runner.invoke(app, ["api", "key", "list"])
    assert "doomed" not in result.output


def test_revoke_nonexistent():
    result = runner.invoke(app, ["api", "key", "revoke", "nonexistent_prefix"])
    assert result.exit_code == 1
    assert "No key found" in result.output
