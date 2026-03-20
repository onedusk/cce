"""Tests for cce.policy.loader — YAML parsing, directory loading, topic overrides."""

from pathlib import Path

import pytest
import yaml

from cce.policy.loader import load_policies, load_policy

pytestmark = pytest.mark.unit


def test_load_policy_minimal(tmp_path):
    policy_file = tmp_path / "minimal.yaml"
    policy_file.write_text(yaml.dump({"id": "test", "name": "Test"}))

    policy = load_policy(policy_file)
    assert policy.id == "test"
    assert policy.name == "Test"
    assert policy.domains_allow == []
    assert policy.domains_deny == []
    assert policy.max_sources_per_run == 50
    assert policy.reputation is not None
    assert policy.recency is not None


def test_load_policy_full(tmp_path):
    data = {
        "id": "full-policy",
        "name": "Full Policy",
        "domains_allow": ["trusted.org"],
        "domains_deny": ["spam.com"],
        "reputation": {
            "require_peer_reviewed": True,
            "trusted_institutions": ["nih.gov", "mit.edu"],
            "block_marketing": True,
        },
        "recency": {
            "max_age_days": 365,
            "prefer_recent": True,
        },
        "max_sources_per_run": 25,
        "topic_overrides": [
            {
                "topic_pattern": "medical",
                "domains_allow": ["nih.gov"],
            }
        ],
    }
    policy_file = tmp_path / "full.yaml"
    policy_file.write_text(yaml.dump(data))

    policy = load_policy(policy_file)
    assert policy.id == "full-policy"
    assert "trusted.org" in policy.domains_allow
    assert "spam.com" in policy.domains_deny
    assert policy.reputation.require_peer_reviewed is True
    assert "nih.gov" in policy.reputation.trusted_institutions
    assert policy.recency.max_age_days == 365
    assert policy.max_sources_per_run == 25
    assert len(policy.topic_overrides) == 1


def test_load_policies_directory(tmp_path):
    for i, pid in enumerate(["alpha", "beta"]):
        f = tmp_path / f"policy_{i}.yaml"
        f.write_text(yaml.dump({"id": pid, "name": f"Policy {pid}"}))

    policies = load_policies(tmp_path)
    assert len(policies) == 2
    assert "alpha" in policies
    assert "beta" in policies


def test_load_policies_skips_invalid(tmp_path):
    # Valid policy
    valid = tmp_path / "valid.yaml"
    valid.write_text(yaml.dump({"id": "good", "name": "Good Policy"}))

    # Malformed YAML (missing required 'id' field)
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(": this is not valid yaml content {{{")

    policies = load_policies(tmp_path)
    assert "good" in policies
    assert len(policies) == 1  # invalid was skipped


def test_parse_policy_topic_overrides(tmp_path):
    data = {
        "id": "override-test",
        "name": "Override Test",
        "topic_overrides": [
            {
                "topic_pattern": "medical.*research",
                "domains_allow": ["nih.gov", "who.int"],
                "domains_deny": ["quack.com"],
                "reputation": {
                    "require_peer_reviewed": True,
                    "trusted_institutions": ["nih.gov"],
                },
            }
        ],
    }
    policy_file = tmp_path / "overrides.yaml"
    policy_file.write_text(yaml.dump(data))

    policy = load_policy(policy_file)
    assert len(policy.topic_overrides) == 1
    ovr = policy.topic_overrides[0]
    assert ovr.topic_pattern == "medical.*research"
    assert "nih.gov" in ovr.domains_allow
    assert "quack.com" in ovr.domains_deny
    assert ovr.reputation is not None
    assert ovr.reputation.require_peer_reviewed is True


@pytest.mark.integration
def test_load_real_peer_reviewed_policy():
    policy_path = Path(__file__).resolve().parent.parent.parent / "policies" / "peer-reviewed.yaml"
    if not policy_path.exists():
        pytest.skip("policies/peer-reviewed.yaml not found")

    policy = load_policy(policy_path)
    assert policy.id == "peer-reviewed"
    assert "reddit.com" in policy.domains_deny
    assert "nih.gov" in policy.reputation.trusted_institutions
    assert policy.max_sources_per_run == 15
