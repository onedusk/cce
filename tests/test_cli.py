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


# ---------------------------------------------------------------------------
# curate / status / jobs (M08, T-08.03)
# ---------------------------------------------------------------------------


def _seed_jobs(tmp_path, jobs_to_seed):
    """Insert pre-built jobs into the CLI's tmp store."""
    import asyncio as _asyncio

    from cce.jobs.store import JobStore

    async def _seed() -> None:
        store = JobStore(db_path=tmp_path / "cli_test.db")
        await store.connect()
        try:
            for job in jobs_to_seed:
                await store.create_job(job)
        finally:
            await store.close()

    _asyncio.run(_seed())


def _stub_curate_engine(monkeypatch, status, package=None):
    """Monkeypatch CurationEngine.embedded with a scripted engine for curate."""
    from cce.engine import CurationEngine

    class _Handle:
        def __init__(self, request):
            self._request = request
            self.job_id = "job_stub1234"

        async def wait(self, timeout: float = 600):
            from tests.conftest import make_job

            return make_job(request=self._request, status=status)

        async def package(self):
            return package

    class _Engine:
        async def curate(self, request):
            return _Handle(request)

        async def close(self) -> None:
            pass

    async def _fake_embedded(*args, **kwargs):
        return _Engine()

    monkeypatch.setattr(CurationEngine, "embedded", _fake_embedded)


def test_curate_completed_exits_0_with_package_summary(monkeypatch):
    from cce.models.job import JobStatus
    from tests.conftest import make_publish_package

    _stub_curate_engine(
        monkeypatch, JobStatus.COMPLETED, package=make_publish_package()
    )
    result = runner.invoke(
        app, ["curate", "sleep hygiene", "--policy-id", "peer-reviewed"]
    )
    assert result.exit_code == 0, result.output
    assert "Job: job_stub1234" in result.output
    assert "Status: completed" in result.output
    assert "citations" in result.output
    assert "scores:" in result.output


def test_curate_review_required_exits_2(monkeypatch):
    from cce.models.job import JobStatus

    _stub_curate_engine(monkeypatch, JobStatus.REVIEW_REQUIRED)
    result = runner.invoke(app, ["curate", "topic", "--policy-id", "peer-reviewed"])
    assert result.exit_code == 2, result.output
    assert "Status: review_required" in result.output


def test_curate_failed_exits_1(monkeypatch):
    from cce.models.job import JobStatus

    _stub_curate_engine(monkeypatch, JobStatus.FAILED)
    result = runner.invoke(app, ["curate", "topic", "--policy-id", "peer-reviewed"])
    assert result.exit_code == 1, result.output
    assert "Status: failed" in result.output


def test_curate_missing_api_key_exits_1(monkeypatch):
    """ConfigError from the embedded engine → one-line stderr error, exit 1."""
    for var in ("CCE_LLM_API_KEY", "CCE_CRAWL_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    result = runner.invoke(app, ["curate", "topic", "--policy-id", "peer-reviewed"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
    assert "Traceback" not in result.output


def test_status_unknown_job_id_exits_1():
    result = runner.invoke(app, ["status", "job_nope"])
    assert result.exit_code == 1
    assert "job not found: job_nope" in result.output


def test_status_shows_stages_metrics_and_budget_note(tmp_path):
    """Status prints stage records with metrics — token usage and the
    budget_exceeded note for a budget-stopped path (T-08.02 acceptance)."""
    from datetime import UTC, datetime

    from cce.models.job import JobStage, JobStatus, StageRecord
    from tests.conftest import make_job

    t = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    job = make_job(
        id="job_budget00001",
        status=JobStatus.REVIEW_REQUIRED,
        completed_at=t,
        stages=[
            StageRecord(
                stage=JobStage.WRITE,
                path="blog",
                started_at=t,
                completed_at=t,
                metrics={
                    "path": "blog",
                    "iterations": 1,
                    "tokens_input": 1200,
                    "tokens_output": 300,
                },
            ),
            StageRecord(
                stage=JobStage.VERIFY,
                path="blog",
                started_at=t,
                completed_at=t,
                metrics={"path": "blog", "confidence_score": 0.3, "pass_rate": 0.3},
            ),
            StageRecord(
                stage=JobStage.WRITE,
                path="blog",
                started_at=t,
                completed_at=t,
                metrics={
                    "path": "blog",
                    "budget_exceeded": True,
                    "stopped_before_iteration": 2,
                    "tokens_spent": 2000,
                    "max_tokens_per_job": 2000,
                },
            ),
            StageRecord(
                stage=JobStage.PUBLISH,
                started_at=t,
                completed_at=t,
                metrics={"token_usage": {"input_tokens": 1600, "output_tokens": 400}},
            ),
        ],
    )
    _seed_jobs(tmp_path, [job])

    result = runner.invoke(app, ["status", "job_budget00001"])
    assert result.exit_code == 0, result.output
    assert "job_budget00001  REVIEW_REQUIRED" in result.output
    assert "topic: test topic" in result.output
    assert "write [blog]" in result.output.replace("  ", " ")
    assert "tokens_input=1200" in result.output
    assert "budget_exceeded=True" in result.output
    assert "max_tokens_per_job=2000" in result.output
    assert "token_usage=" in result.output


def test_jobs_empty_store_exits_0():
    result = runner.invoke(app, ["jobs"])
    assert result.exit_code == 0
    assert "no jobs" in result.output


def test_jobs_table_newest_first_with_limit_and_status_filter(tmp_path):
    from datetime import UTC, datetime

    from cce.models.job import JobStatus
    from tests.conftest import make_curation_request, make_job

    seeded = [
        make_job(
            id=f"job_seed00000{i:03d}",
            request=make_curation_request(topic=f"topic {i}"),
            status=status,
            created_at=datetime(2026, 6, i + 1, tzinfo=UTC),
        )
        for i, status in enumerate(
            [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.REVIEW_REQUIRED]
        )
    ]
    _seed_jobs(tmp_path, seeded)

    # Newest first: topic 2 (June 3rd) before topic 0 (June 1st).
    result = runner.invoke(app, ["jobs"])
    assert result.exit_code == 0, result.output
    assert result.output.index("topic 2") < result.output.index("topic 0")
    assert "ID" in result.output and "STATUS" in result.output

    # --limit caps the table.
    result = runner.invoke(app, ["jobs", "--limit", "1"])
    assert "topic 2" in result.output
    assert "topic 0" not in result.output

    # --status filters.
    result = runner.invoke(app, ["jobs", "--status", "failed"])
    assert result.exit_code == 0
    assert "topic 1" in result.output
    assert "topic 0" not in result.output and "topic 2" not in result.output


def test_jobs_invalid_status_filter_exits_1():
    result = runner.invoke(app, ["jobs", "--status", "bogus"])
    assert result.exit_code == 1
    assert "unknown status 'bogus'" in result.output


# ---------------------------------------------------------------------------
# validate (M08, T-08.04 — PDR-003)
# ---------------------------------------------------------------------------

GOOD_POLICY_YAML = """\
id: good-policy
name: Good Policy
domains_deny: [example.com]
recency:
  max_age_days: 365
"""

TYPO_POLICY_YAML = """\
id: typo-policy
name: Typo Policy
recency:
  max_age_day: 365
"""

GOOD_PATH_CONFIG_YAML = """\
paths:
  - id: learn
    name: Learn
    tone: pedagogical
    max_words: 2000
"""

GOOD_TAXONOMY_YAML = """\
id: tiny
name: Tiny Taxonomy
dimensions:
  - id: physical
    name: Physical
    values: [primary, none]
"""


def _write_validate_tree(root, *, include_typo: bool = False) -> None:
    (root / "policies").mkdir()
    (root / "policies" / "good.yaml").write_text(GOOD_POLICY_YAML)
    if include_typo:
        (root / "policies" / "typo.yaml").write_text(TYPO_POLICY_YAML)
    (root / "path_configs").mkdir()
    (root / "path_configs" / "default.yaml").write_text(GOOD_PATH_CONFIG_YAML)
    (root / "taxonomies").mkdir()
    (root / "taxonomies" / "tiny.yaml").write_text(GOOD_TAXONOMY_YAML)


def test_validate_typo_policy_exits_1_with_suggestion(tmp_path):
    """A typo'd `max_age_day` names the file, the key, and suggests the fix."""
    _write_validate_tree(tmp_path, include_typo=True)

    result = runner.invoke(app, ["validate", "--root", str(tmp_path)])
    assert result.exit_code == 1
    typo_line = next(line for line in result.output.splitlines() if "typo.yaml" in line)
    assert "ERROR" in typo_line
    assert "max_age_day" in typo_line
    assert "did you mean 'max_age_days'?" in typo_line
    assert "1 error in 4 files" in result.output


def test_validate_all_good_tree_exits_0(tmp_path):
    _write_validate_tree(tmp_path)

    result = runner.invoke(app, ["validate", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.output.count(" OK") == 3
    assert "0 errors in 3 files" in result.output


def test_validate_missing_directory_noted_and_skipped(tmp_path):
    """A repo without taxonomies/ is legal — note + continue, exit 0."""
    _write_validate_tree(tmp_path)
    (tmp_path / "taxonomies" / "tiny.yaml").unlink()
    (tmp_path / "taxonomies").rmdir()

    result = runner.invoke(app, ["validate", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "taxonomies/ not found" in result.output
    assert "0 errors in 2 files" in result.output


def test_validate_real_repo_tree_passes():
    """Acceptance: the repo's real policies/, path_configs/, taxonomies/
    pass strict validation (T-08.04)."""
    from pathlib import Path as _Path

    repo_root = _Path(__file__).parents[1]
    result = runner.invoke(app, ["validate", "--root", str(repo_root)])
    assert result.exit_code == 0, result.output
    assert "0 errors" in result.output
