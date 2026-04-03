"""Tests for the emit-mdx CLI command."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cce.cli import app
from cce.jobs.store import JobStore
from cce.models.content import Citation
from cce.models.job import JobStatus
from tests.conftest import (
    make_content_unit,
    make_curation_request,
    make_evidence,
    make_job,
    make_publish_package,
)

runner = CliRunner()


async def _seed_store(db_path: Path) -> tuple[str, str]:
    """Seed a job store with a completed job + package. Returns (job_id, topic)."""
    store = JobStore(db_path=db_path)
    await store.connect()
    try:
        ev = make_evidence(id="ev_cli_1", url="https://example.com/1")
        unit = make_content_unit(
            path="learn",
            content="## CLI Test\n\nA claim [ev:ev_cli_1].",
            citations=[Citation(evidence_id="ev_cli_1", url="https://example.com/1")],
        )
        job = make_job(
            status=JobStatus.COMPLETED,
            request=make_curation_request(
                topic="cli test topic",
                paths=["learn"],
                policy_id="test-policy",
            ),
        )
        pkg = make_publish_package(
            job_id=job.id,
            units=[unit],
            evidence=[ev],
        )
        await store.create_job(job)
        await store.store_package(job.id, pkg)
        return job.id, "cli test topic"
    finally:
        await store.close()


def _run_emit(*args: str, db_path: Path, target: Path) -> object:
    """Invoke the emit-mdx CLI with a config pointing to the test DB."""
    # Write a minimal config YAML that points to our test DB
    config_path = db_path.parent / "config.yaml"
    config_path.write_text(
        f"evidence_store:\n  sqlite_path: {db_path}\n"
    )
    return runner.invoke(
        app,
        ["emit-mdx", *args, "--target", str(target), "--config", str(config_path)],
    )


class TestEmitMdxCli:
    def test_happy_path_by_job(self, tmp_path):
        db_path = tmp_path / "test.db"
        target = tmp_path / "content"
        target.mkdir()

        job_id, _ = asyncio.run(_seed_store(db_path))

        result = _run_emit("--job", job_id, db_path=db_path, target=target)

        assert result.exit_code == 0, result.output
        assert "Emitted:" in result.output
        assert "learn/page.mdx" in result.output
        # Verify files exist
        topic_dirs = [d for d in target.iterdir() if d.is_dir()]
        assert len(topic_dirs) == 1
        assert (topic_dirs[0] / "learn" / "page.mdx").exists()

    def test_happy_path_by_topic(self, tmp_path):
        db_path = tmp_path / "test.db"
        target = tmp_path / "content"
        target.mkdir()

        _, topic = asyncio.run(_seed_store(db_path))

        result = _run_emit("--topic", topic, db_path=db_path, target=target)

        assert result.exit_code == 0, result.output
        assert "Emitted:" in result.output

    def test_missing_job_and_topic(self, tmp_path):
        target = tmp_path / "content"
        target.mkdir()
        config_path = tmp_path / "config.yaml"
        config_path.write_text("evidence_store:\n  sqlite_path: dummy.db\n")

        result = runner.invoke(
            app,
            ["emit-mdx", "--target", str(target), "--config", str(config_path)],
        )

        assert result.exit_code == 1
        assert "provide --job, --topic, or --all" in result.output

    def test_nonexistent_job(self, tmp_path):
        db_path = tmp_path / "test.db"
        target = tmp_path / "content"
        target.mkdir()

        # Seed to create the DB schema, but use a fake job ID
        asyncio.run(_seed_store(db_path))

        result = _run_emit("--job", "job_nonexistent", db_path=db_path, target=target)

        assert result.exit_code == 1
        assert "no package found" in result.output

    def test_nonexistent_target(self, tmp_path):
        db_path = tmp_path / "test.db"
        asyncio.run(_seed_store(db_path))

        result = _run_emit(
            "--job", "job_any",
            db_path=db_path,
            target=tmp_path / "nonexistent",
        )

        assert result.exit_code == 1
        assert "does not exist" in result.output

    def test_no_completed_jobs_for_topic(self, tmp_path):
        db_path = tmp_path / "test.db"
        target = tmp_path / "content"
        target.mkdir()

        # Seed DB with schema but search for a topic that doesn't exist
        asyncio.run(_seed_store(db_path))

        result = _run_emit("--topic", "unknown-topic", db_path=db_path, target=target)

        assert result.exit_code == 1
        assert "no completed jobs" in result.output

    def test_all_flag(self, tmp_path):
        db_path = tmp_path / "test.db"
        target = tmp_path / "content"
        target.mkdir()

        asyncio.run(_seed_store(db_path))

        result = _run_emit("--all", db_path=db_path, target=target)

        assert result.exit_code == 0, result.output
        assert "Emitted:" in result.output
        assert "1 topic(s)" in result.output
