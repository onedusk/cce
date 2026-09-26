"""B9 acceptance: the repo's policies allow and deny the same hosts as the
old substring matcher, apart from the documented bug fixes.

For every allow/deny entry of every shipped policy, a table of host variants
is checked against a frozen copy of the pre-B9 ``_passes_policy``. Every
difference must be in ``_is_expected_fix``, which names the bug each fixed
row belonged to.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from cce.discovery.discoverer import Discoverer
from cce.policy.types import SourcePolicy

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def _old_passes_policy(url: str, policy: SourcePolicy) -> bool:
    """Frozen pre-B9 implementation (substring match on netloc)."""
    domain = urlparse(url).netloc.lower()
    if not domain:
        return False
    for denied in policy.domains_deny:
        if denied.lower() in domain:
            return False
    if policy.domains_allow:
        if not any(allowed.lower() in domain for allowed in policy.domains_allow):
            return False
    return True


def _policies() -> list[SourcePolicy]:
    files = sorted((REPO / "policies").glob("*.yaml")) + sorted(
        (REPO / "policies" / "examples").glob("*.yaml")
    )
    policies = []
    for f in files:
        data = yaml.safe_load(f.read_text())
        if isinstance(data, dict) and "id" in data and "name" in data:
            policies.append(SourcePolicy(**data))
    return policies


VARIANTS = {
    "exact": "https://{e}/p",
    "www": "https://www.{e}/p",
    "deep_subdomain": "https://a.b.{e}/p",
    "prefix_lookalike": "https://f{e}/p",  # fox.com vs x.com
    "trailing_label": "https://{e}.au/p",  # amazon.com.au
    "port": "https://{e}:443/p",
    "userinfo": "https://user@{e}/p",
}
NEUTRAL = [
    "https://example.com/p",
    "https://en.wikipedia.org/wiki/Sleep",
    "https://www.nih.gov/news",
    "https://www.mayoclinic.org/x",
]


def _is_expected_fix(variant: str, in_allow: bool, in_deny: bool) -> bool:
    # A deny entry's text inside another label no longer blocks the host.
    if variant == "prefix_lookalike" and in_deny:
        return True
    # An allow entry no longer admits lookalikes or hosts it only prefixes.
    if in_allow and variant in ("prefix_lookalike", "trailing_label"):
        return True
    return False


def _rows(policy: SourcePolicy):
    allow = [e.strip().lstrip("*").strip(".") for e in policy.domains_allow]
    deny = [e.strip().lstrip("*").strip(".") for e in policy.domains_deny]
    for entry in {*allow, *deny}:
        if not entry:
            continue
        for variant, template in VARIANTS.items():
            yield template.format(e=entry), variant, entry in allow, entry in deny
    for url in NEUTRAL:
        yield url, "neutral", False, False


@pytest.mark.parametrize("policy", _policies(), ids=lambda p: p.id)
def test_repo_policies_keep_their_allow_and_deny_results(policy):
    unexpected = []
    for url, variant, in_allow, in_deny in _rows(policy):
        old = _old_passes_policy(url, policy)
        new = Discoverer._passes_policy(url, policy)
        if old != new and not _is_expected_fix(variant, in_allow, in_deny):
            unexpected.append((url, variant, old, new))
    assert unexpected == []


def test_parity_covers_real_policies():
    ids = {p.id for p in _policies()}
    assert "peer-reviewed" in ids


def test_parity_table_actually_exercises_the_fixes():
    """The comparison is sensitive: the documented fixes do show up as
    differences (else a matcher change could pass this suite vacuously)."""
    diffs = [
        variant
        for policy in _policies()
        for url, variant, in_allow, in_deny in _rows(policy)
        if _old_passes_policy(url, policy) != Discoverer._passes_policy(url, policy)
    ]
    assert "prefix_lookalike" in diffs
