"""Source policy loader.

Loads SourcePolicy definitions from YAML files. Each policy is a separate
YAML file (or a list within a single file).

Usage:
    policies = load_policies("policies/")         # directory of YAML files
    policy = load_policy("policies/strict.yaml")  # single file
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from cce.policy.types import (
    RecencyRule,
    ReputationRule,
    SourcePolicy,
    TopicOverride,
)

logger = logging.getLogger(__name__)


def load_policy(path: str | Path) -> SourcePolicy:
    """Load a single SourcePolicy from a YAML file."""
    path = Path(path)
    with open(path) as f:
        data = yaml.safe_load(f)

    if isinstance(data, list):
        # If the file contains multiple policies, return the first
        data = data[0]

    return _parse_policy(data)


def load_policies(directory: str | Path) -> dict[str, SourcePolicy]:
    """Load all policies from a directory of YAML files.

    Returns a dict keyed by policy ID.
    """
    directory = Path(directory)
    policies: dict[str, SourcePolicy] = {}

    for path in sorted(directory.glob("*.yaml")):
        try:
            with open(path) as f:
                data = yaml.safe_load(f)

            if isinstance(data, list):
                for item in data:
                    policy = _parse_policy(item)
                    policies[policy.id] = policy
            elif isinstance(data, dict):
                policy = _parse_policy(data)
                policies[policy.id] = policy

            logger.info("Loaded policy from %s", path.name)
        except Exception as e:
            logger.warning("Failed to load policy from %s: %s", path, e)

    return policies


def _parse_policy(data: dict) -> SourcePolicy:
    """Parse a policy dict (from YAML) into a SourcePolicy."""
    reputation_data = data.get("reputation", {})
    recency_data = data.get("recency", {})
    overrides_data = data.get("topic_overrides", [])

    overrides = []
    for ovr in overrides_data:
        ovr_rep = ovr.get("reputation")
        ovr_rec = ovr.get("recency")
        overrides.append(
            TopicOverride(
                topic_pattern=ovr["topic_pattern"],
                domains_allow=ovr.get("domains_allow", []),
                domains_deny=ovr.get("domains_deny", []),
                reputation=ReputationRule(**ovr_rep) if ovr_rep else None,
                recency=RecencyRule(**ovr_rec) if ovr_rec else None,
            )
        )

    return SourcePolicy(
        id=data["id"],
        name=data.get("name", data["id"]),
        domains_allow=data.get("domains_allow", []),
        domains_deny=data.get("domains_deny", []),
        reputation=ReputationRule(**reputation_data) if reputation_data else ReputationRule(),
        recency=RecencyRule(**recency_data) if recency_data else RecencyRule(),
        max_sources_per_run=data.get("max_sources_per_run", 50),
        topic_overrides=overrides,
    )
