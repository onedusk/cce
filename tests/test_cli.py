"""Tests for CLI commands — key management via typer.testing.CliRunner."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cce.cli import app

runner = CliRunner()

# Every test runs under the autouse _use_tmp_db fixture (monkeypatched env +
# real SQLite via CliRunner), which is integration tier per Stage 0.
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Point the CLI at a temp DB so tests don't collide.

    Also redirects Path.home() to tmp_path so `api key generate`'s default
    output path (`~/.cce/api-key`) writes into the test sandbox instead of
    the real user home directory.
    """
    from pathlib import Path as _Path

    monkeypatch.setenv("CCE_EVIDENCE_SQLITE_PATH", str(tmp_path / "cli_test.db"))
    monkeypatch.setattr(_Path, "home", classmethod(lambda cls: tmp_path))
    # Clear any other env vars that might interfere
    for var in ("ANTHROPIC_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_load_env_file_sets_missing_keys(tmp_path, monkeypatch):
    """load_env_file sets keys that aren't already in os.environ."""
    from cce import load_env_file

    monkeypatch.delenv("CCE_TEST_KEY_A", raising=False)
    monkeypatch.delenv("CCE_TEST_KEY_B", raising=False)
    env = tmp_path / ".env"
    env.write_text('CCE_TEST_KEY_A=value-a\nCCE_TEST_KEY_B="quoted-b"\n')

    load_env_file(env)

    import os

    assert os.environ.get("CCE_TEST_KEY_A") == "value-a"
    assert os.environ.get("CCE_TEST_KEY_B") == "quoted-b"


def test_load_env_file_does_not_overwrite(tmp_path, monkeypatch):
    """Existing env vars win; load_env_file uses setdefault semantics."""
    from cce import load_env_file

    monkeypatch.setenv("CCE_TEST_PRESET", "original")
    env = tmp_path / ".env"
    env.write_text("CCE_TEST_PRESET=from-file\n")

    load_env_file(env)

    import os

    assert os.environ.get("CCE_TEST_PRESET") == "original"


def test_load_env_file_ignores_comments_and_blanks(tmp_path, monkeypatch):
    from cce import load_env_file

    monkeypatch.delenv("CCE_TEST_REAL", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# this is a comment\n"
        "\n"
        "CCE_TEST_REAL=real-value\n"
        "malformed line with no equals sign\n"
    )

    load_env_file(env)

    import os

    assert os.environ.get("CCE_TEST_REAL") == "real-value"


def test_load_env_file_missing_file_is_noop(tmp_path):
    """load_env_file on a nonexistent path just returns — doesn't raise."""
    from cce import load_env_file

    load_env_file(tmp_path / "does-not-exist.env")
    # No assertion needed; reaching this line means no exception.


def test_batch_command_rejects_missing_topics_file(tmp_path):
    """--topics-file pointing at a nonexistent file exits with non-zero."""
    result = runner.invoke(
        app,
        [
            "batch",
            "--topics-file",
            str(tmp_path / "nope.yaml"),
            "--policy-id",
            "peer-reviewed",
        ],
    )
    assert result.exit_code != 0


def test_batch_command_rejects_non_list_yaml(tmp_path):
    """The YAML must be a top-level list of topic entries."""
    topics_file = tmp_path / "topics.yaml"
    topics_file.write_text("key: value\n")  # dict, not list

    result = runner.invoke(
        app,
        [
            "batch",
            "--topics-file",
            str(topics_file),
            "--policy-id",
            "peer-reviewed",
        ],
    )
    assert result.exit_code != 0
    assert "top-level YAML list" in result.output


def test_batch_command_missing_api_key_exits_1(tmp_path, monkeypatch):
    """`cce batch` without ANTHROPIC_API_KEY exits 1 with a one-line error (T-01.02).

    The autouse fixture already scrubs ANTHROPIC_API_KEY/FIRECRAWL_API_KEY;
    also scrub the CCE_* aliases so load_config sees no key at all.
    """
    for var in ("CCE_LLM_API_KEY", "CCE_CRAWL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    topics_file = tmp_path / "topics.yaml"
    topics_file.write_text("- topic: test\n  paths: [blog]\n")

    result = runner.invoke(
        app,
        [
            "batch",
            "--topics-file",
            str(topics_file),
            "--policy-id",
            "peer-reviewed",
        ],
    )
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
    assert "Traceback" not in result.output


def test_generate_key_writes_file_by_default(tmp_path):
    """Default `api key generate` writes a 0600-mode file and doesn't print the key (audit U3)."""
    import os
    import stat

    result = runner.invoke(app, ["api", "key", "generate", "--label", "test"])
    assert result.exit_code == 0, result.output
    # Key is NOT in stdout under the default.
    assert "API Key:" not in result.output
    # Output message points to the file.
    assert "Wrote API key to" in result.output
    assert "mode 0600" in result.output
    # File actually exists and has 0600 permissions.
    key_path = tmp_path / ".cce" / "api-key"
    assert key_path.exists()
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
    # And contains a non-empty key.
    assert len(key_path.read_text().strip()) > 0


def test_generate_key_print_flag_echoes_to_stdout():
    """--print opts into legacy stdout behavior."""
    result = runner.invoke(app, ["api", "key", "generate", "--print", "--label", "t"])
    assert result.exit_code == 0, result.output
    assert "API Key:" in result.output
    assert "Hash:" in result.output
    assert "Store this key securely" in result.output


def test_generate_key_custom_output_path(tmp_path):
    """--output writes to an explicit path (parent dirs created as needed)."""
    import os
    import stat

    target = tmp_path / "deep" / "nested" / "dir" / "key.txt"
    result = runner.invoke(app, ["api", "key", "generate", "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert target.exists()
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600


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
    hash_prefix = None
    for line in result.output.splitlines():
        if line.startswith("Hash:"):
            hash_prefix = line.split()[-1].rstrip(".")
            break
    assert hash_prefix is not None, "no 'Hash:' line in output"

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


def test_revoke_ambiguous_prefix_errors(tmp_path):
    """Two keys sharing a hash prefix → revoke with that prefix refuses (T-04.05)."""
    import asyncio as _asyncio

    from cce.jobs.store import JobStore

    async def _seed() -> None:
        store = JobStore(db_path=tmp_path / "cli_test.db")
        await store.connect()
        try:
            await store.store_api_key("deadbeef" + "0" * 56, label="one")
            await store.store_api_key("deadbeef" + "1" * 56, label="two")
        finally:
            await store.close()

    _asyncio.run(_seed())

    result = runner.invoke(app, ["api", "key", "revoke", "deadbeef"])
    assert result.exit_code == 1
    assert "Ambiguous prefix" in result.output
    assert "2 keys match" in result.output

    # Neither key was revoked.
    result = runner.invoke(app, ["api", "key", "list"])
    assert "one" in result.output
    assert "two" in result.output


# ---------------------------------------------------------------------------
# batch — happy path (T-04.05)
# ---------------------------------------------------------------------------


def test_batch_happy_path_skips_malformed_entry(tmp_path, monkeypatch):
    """2 valid entries submitted, 1 malformed entry skipped, exit 0."""
    from cce.engine import CurationEngine
    from cce.models.job import JobStatus
    from tests.conftest import make_job

    submitted = []

    class _StubHandle:
        def __init__(self, request):
            self._request = request

        async def wait(self, timeout: float = 600):
            return make_job(request=self._request, status=JobStatus.COMPLETED)

    class _StubEngine:
        async def curate(self, request):
            submitted.append(request)
            return _StubHandle(request)

        async def close(self) -> None:
            pass

    async def _fake_embedded(*args, **kwargs):
        return _StubEngine()

    monkeypatch.setattr(CurationEngine, "embedded", _fake_embedded)

    topics_file = tmp_path / "topics.yaml"
    topics_file.write_text(
        "- topic: first topic\n"
        "  paths: [blog]\n"
        "- not-a-dict-entry\n"
        "- topic: second topic\n"
        "  paths: [blog, guide]\n"
        "  audience: experts\n"
    )

    result = runner.invoke(
        app,
        ["batch", "--topics-file", str(topics_file), "--policy-id", "test-policy"],
    )
    assert result.exit_code == 0, result.output

    assert len(submitted) == 2
    assert submitted[0].topic == "first topic"
    assert submitted[0].paths == ["blog"]
    assert submitted[0].policy_id == "test-policy"
    assert submitted[1].topic == "second topic"
    assert submitted[1].paths == ["blog", "guide"]
    assert submitted[1].audience == "experts"

    assert "SKIP (not a dict)" in result.output
    assert "completed: first topic" in result.output
    assert "completed: second topic" in result.output


# ---------------------------------------------------------------------------
# emit-mdx — error branches (T-04.05)
# ---------------------------------------------------------------------------


def test_emit_mdx_all_with_no_completed_jobs(tmp_path):
    target = tmp_path / "content"
    target.mkdir()
    result = runner.invoke(app, ["emit-mdx", "--all", "--target", str(target)])
    assert result.exit_code == 1
    assert "no completed jobs found" in result.output


def test_emit_mdx_job_without_package(tmp_path):
    target = tmp_path / "content"
    target.mkdir()
    result = runner.invoke(
        app, ["emit-mdx", "--job", "job_missing", "--target", str(target)]
    )
    assert result.exit_code == 1
    assert "no package found for job job_missing" in result.output


def test_emit_mdx_topic_with_no_completed_jobs(tmp_path):
    target = tmp_path / "content"
    target.mkdir()
    result = runner.invoke(
        app, ["emit-mdx", "--topic", "ghost-topic", "--target", str(target)]
    )
    assert result.exit_code == 1
    assert "no completed jobs for topic 'ghost-topic'" in result.output
