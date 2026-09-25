"""Pinned context in the evidence block (B11)."""

from __future__ import annotations

import pytest

from cce.evidence.formatting import format_evidence_for_prompt
from cce.llm.anthropic import AnthropicProvider
from cce.models.evidence import SourceQuality
from tests.conftest import make_evidence

pytestmark = pytest.mark.unit

CONTEXT = [
    make_evidence(id="ctx_a", url="consumer://acme/canon", excerpt="No em-dashes."),
    make_evidence(
        id="ctx_b",
        url="consumer://acme/canon",
        excerpt="Our plan is $12 a month.",
        source_quality=SourceQuality(conflict_of_interest=True),
    ),
]
SOURCES = [make_evidence(excerpt="A discovered excerpt about the plan pricing.")]


@pytest.mark.parametrize("style", ["writer", "verifier"])
def test_without_context_the_block_is_unchanged(style):
    plain = format_evidence_for_prompt(SOURCES, style=style)
    assert format_evidence_for_prompt(SOURCES, style=style, context=None) == plain
    assert format_evidence_for_prompt(SOURCES, style=style, context=[]) == plain
    assert "CONTEXT" not in plain


@pytest.mark.parametrize("style", ["writer", "verifier"])
def test_context_comes_first_under_its_own_heading(style):
    block = format_evidence_for_prompt(SOURCES, style=style, context=CONTEXT)

    context_part, sources_part = block.split("=== SOURCES ===")
    assert context_part.startswith("=== CONTEXT (settled) ===\nSettled statements")
    for ev in CONTEXT:
        assert f'<evidence id="{ev.id}">' in context_part
        assert ev.excerpt in context_part
    assert SOURCES[0].excerpt in sources_part
    assert block.count("<evidence id=") == 3
    for marker in ("=== EVIDENCE END ===", "=== END EVIDENCE ==="):
        assert marker not in block


def test_verifier_context_carries_no_trust_tags():
    block = format_evidence_for_prompt(SOURCES, style="verifier", context=CONTEXT)
    assert "[potential-COI]" not in block.split("=== SOURCES ===")[0]
    assert "trust weighting does not apply" in block


@pytest.mark.parametrize("shape", ["writer", "verifier"])
async def test_context_sits_inside_the_cached_prefix(shape):
    from tests.test_evidence.test_untrusted_prompts import (
        _verifier_prompt,
        _writer_prompt,
    )

    if shape == "writer":
        from cce.models.request import CurationRequest

        request = CurationRequest(
            topic="t", paths=["blog"], policy_id="p", context=CONTEXT
        )
        prompt = await _writer_prompt(SOURCES, request=request)
        fence = "=== EVIDENCE END ==="
    else:
        prompt = await _verifier_prompt(
            "A claim [ev:ctx_a].", [*CONTEXT, *SOURCES], context=CONTEXT
        )
        fence = "=== END EVIDENCE ==="

    cached = AnthropicProvider._split_for_cache(prompt)[0]
    assert cached["text"].endswith(fence)
    for text in ("=== CONTEXT (settled) ===", "=== SOURCES ===", "No em-dashes."):
        assert text in cached["text"]
