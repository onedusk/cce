"""Tests for CurationRequest field validation constraints."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cce.models.request import CurationRequest


def _valid(**overrides) -> dict:
    """Base valid request kwargs."""
    defaults = {
        "topic": "test topic",
        "paths": ["learn"],
        "policy_id": "test-policy",
    }
    defaults.update(overrides)
    return defaults


class TestCurationRequestValidation:
    def test_valid_request(self):
        r = CurationRequest(**_valid())
        assert r.topic == "test topic"

    def test_empty_topic_rejected(self):
        with pytest.raises(ValidationError, match="topic"):
            CurationRequest(**_valid(topic=""))

    def test_topic_too_long_rejected(self):
        with pytest.raises(ValidationError, match="topic"):
            CurationRequest(**_valid(topic="x" * 501))

    def test_empty_paths_rejected(self):
        with pytest.raises(ValidationError, match="paths"):
            CurationRequest(**_valid(paths=[]))

    def test_too_many_paths_rejected(self):
        with pytest.raises(ValidationError, match="paths"):
            CurationRequest(**_valid(paths=[f"path_{i}" for i in range(11)]))

    def test_too_many_subtopics_rejected(self):
        with pytest.raises(ValidationError, match="subtopics"):
            CurationRequest(**_valid(subtopics=[f"sub_{i}" for i in range(21)]))

    @pytest.mark.unit
    def test_subtopic_too_long_rejected(self):
        with pytest.raises(ValidationError, match="subtopic exceeds 200 chars"):
            CurationRequest(**_valid(subtopics=["x" * 201]))

    @pytest.mark.unit
    def test_subtopic_at_max_length_accepted(self):
        r = CurationRequest(**_valid(subtopics=["x" * 200]))
        assert len(r.subtopics[0]) == 200

    def test_empty_policy_id_rejected(self):
        with pytest.raises(ValidationError, match="policy_id"):
            CurationRequest(**_valid(policy_id=""))

    def test_invalid_risk_profile_rejected(self):
        with pytest.raises(ValidationError, match="risk_profile"):
            CurationRequest(**_valid(risk_profile="extreme"))

    def test_valid_risk_profiles(self):
        for profile in ("low", "medium", "high"):
            r = CurationRequest(**_valid(risk_profile=profile))
            assert r.risk_profile == profile

    def test_audience_too_long_rejected(self):
        with pytest.raises(ValidationError, match="audience"):
            CurationRequest(**_valid(audience="x" * 101))

    def test_defaults_work(self):
        r = CurationRequest(**_valid())
        assert r.audience == "general"
        assert r.risk_profile == "medium"
        assert r.subtopics == []
        assert r.constraints is None
