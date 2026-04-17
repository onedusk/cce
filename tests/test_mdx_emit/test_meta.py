"""Tests for cce.output.mdx.meta — topic meta.json merge logic."""

from __future__ import annotations

import json

from cce.models.content import ContentScores
from cce.models.package import PackageLineage
from cce.output.mdx.meta import merge_topic_meta


def _scores(confidence: float = 0.9, coverage: float = 0.85, diversity: float = 0.8):
    return ContentScores(
        confidence=confidence, coverage=coverage, source_diversity=diversity
    )


def _lineage(policy_id: str = "peer-reviewed", engine_version: str = "0.1.0"):
    return PackageLineage(
        policy_id=policy_id, run_id="run_test", engine_version=engine_version
    )


class TestMergeTopicMeta:
    def test_merge_preserves_editorial_fields(self, tmp_path):
        meta_path = tmp_path / "meta.json"
        editorial = {
            "slug": "anxiety",
            "title": "Anxiety",
            "dimensions": ["emotional", "physical"],
            "orientationCopy": "Hand-authored text",
            "learnIntro": "Learn intro text",
        }
        meta_path.write_text(json.dumps(editorial))

        merge_topic_meta(
            meta_path, _scores(), _lineage(), "job_1", "2026-04-01T12:00:00Z"
        )

        result = json.loads(meta_path.read_text())
        assert result["slug"] == "anxiety"
        assert result["title"] == "Anxiety"
        assert result["dimensions"] == ["emotional", "physical"]
        assert result["orientationCopy"] == "Hand-authored text"
        assert result["learnIntro"] == "Learn intro text"
        # CCE fields also present
        assert result["jobId"] == "job_1"
        assert result["scores"]["confidence"] == 0.9

    def test_create_new_file(self, tmp_path):
        meta_path = tmp_path / "meta.json"
        assert not meta_path.exists()

        merge_topic_meta(
            meta_path, _scores(), _lineage(), "job_new", "2026-04-01T00:00:00Z"
        )

        result = json.loads(meta_path.read_text())
        assert result["jobId"] == "job_new"
        assert result["policyId"] == "peer-reviewed"
        assert result["engineVersion"] == "0.1.0"
        # No editorial defaults
        assert "slug" not in result
        assert "title" not in result

    def test_update_scores_on_remerge(self, tmp_path):
        meta_path = tmp_path / "meta.json"

        merge_topic_meta(meta_path, _scores(0.7, 0.6, 0.5), _lineage(), "job_1", "t1")
        merge_topic_meta(
            meta_path, _scores(0.95, 0.92, 0.88), _lineage(), "job_2", "t2"
        )

        result = json.loads(meta_path.read_text())
        assert result["scores"]["confidence"] == 0.95
        assert result["scores"]["coverage"] == 0.92
        assert result["scores"]["sourceDiversity"] == 0.88
        assert result["jobId"] == "job_2"

    def test_preserves_unknown_keys(self, tmp_path):
        meta_path = tmp_path / "meta.json"
        meta_path.write_text(json.dumps({"customField": True, "nested": {"a": 1}}))

        merge_topic_meta(meta_path, _scores(), _lineage(), "j", "t")

        result = json.loads(meta_path.read_text())
        assert result["customField"] is True
        assert result["nested"] == {"a": 1}

    def test_json_validity_and_trailing_newline(self, tmp_path):
        meta_path = tmp_path / "meta.json"
        merge_topic_meta(meta_path, _scores(), _lineage(), "j", "t")

        raw = meta_path.read_text()
        assert raw.endswith("\n")
        json.loads(raw)  # should not raise
