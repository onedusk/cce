"""Tests for verifier trust weighting and jurisdiction pass-through (M04)."""

import json

import pytest

from cce.llm.base import LLMResponse
from cce.verification.verifier import (
    _VERIFIER_FULL_PROMPT,
    VERIFIER_SYSTEM_PROMPT,
    Verifier,
)
from tests.conftest import MockLLMProvider, make_content_unit, make_evidence


def _make_valid_response() -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim": "Test claim",
                    "citation_ids": ["ev_001"],
                    "assessment": "supported",
                    "explanation": "Evidence matches",
                    "suggestion": "",
                }
            ],
            "summary": {
                "total_claims": 1,
                "supported": 1,
                "unsupported": 0,
                "uncited": 0,
                "leakage": 0,
                "conflicts": 0,
                "gaps_acknowledged": 0,
            },
            "overall_feedback": "All claims supported.",
            "contradictions": [],
        }
    )


@pytest.mark.integration
class TestTrustWeighting:
    async def test_system_prompt_contains_trust_weighting(self):
        llm = MockLLMProvider(
            [
                LLMResponse(
                    content=_make_valid_response(), model="mock", stop_reason="end_turn"
                )
            ]
        )
        verifier = Verifier(llm)
        unit = make_content_unit(content="Test claim [ev:ev_001].")
        evidence = [make_evidence(id="ev_001")]

        await verifier.verify(unit, evidence)

        system = llm.calls[0]["system"]
        assert "SOURCE TRUST WEIGHTING" in system

    async def test_system_prompt_contains_coi_instruction(self):
        llm = MockLLMProvider(
            [
                LLMResponse(
                    content=_make_valid_response(), model="mock", stop_reason="end_turn"
                )
            ]
        )
        verifier = Verifier(llm)
        unit = make_content_unit(content="Test claim [ev:ev_001].")
        evidence = [make_evidence(id="ev_001")]

        await verifier.verify(unit, evidence)

        system = llm.calls[0]["system"]
        assert "COI-flagged sources" in system

    async def test_base_prompt_preserved(self):
        llm = MockLLMProvider(
            [
                LLMResponse(
                    content=_make_valid_response(), model="mock", stop_reason="end_turn"
                )
            ]
        )
        verifier = Verifier(llm)
        unit = make_content_unit(content="Test claim [ev:ev_001].")
        evidence = [make_evidence(id="ev_001")]

        await verifier.verify(unit, evidence)

        system = llm.calls[0]["system"]
        assert system.startswith(VERIFIER_SYSTEM_PROMPT)
        assert system == _VERIFIER_FULL_PROMPT


@pytest.mark.integration
class TestJurisdiction:
    async def test_jurisdiction_in_user_prompt(self):
        llm = MockLLMProvider(
            [
                LLMResponse(
                    content=_make_valid_response(), model="mock", stop_reason="end_turn"
                )
            ]
        )
        verifier = Verifier(llm)
        unit = make_content_unit(content="Test claim [ev:ev_001].")
        evidence = [make_evidence(id="ev_001")]

        await verifier.verify(unit, evidence, jurisdiction="EU")

        user_msg = llm.calls[0]["messages"][0].content
        assert "Jurisdiction/scope: EU" in user_msg

    async def test_jurisdiction_absent_when_not_provided(self):
        llm = MockLLMProvider(
            [
                LLMResponse(
                    content=_make_valid_response(), model="mock", stop_reason="end_turn"
                )
            ]
        )
        verifier = Verifier(llm)
        unit = make_content_unit(content="Test claim [ev:ev_001].")
        evidence = [make_evidence(id="ev_001")]

        await verifier.verify(unit, evidence)

        user_msg = llm.calls[0]["messages"][0].content
        assert "Jurisdiction/scope" not in user_msg

    async def test_backward_compat_positional_args(self):
        """verify(unit, evidence) with positional args still works."""
        llm = MockLLMProvider(
            [
                LLMResponse(
                    content=_make_valid_response(), model="mock", stop_reason="end_turn"
                )
            ]
        )
        verifier = Verifier(llm)
        unit = make_content_unit(content="Test claim [ev:ev_001].")
        evidence = [make_evidence(id="ev_001")]

        report = await verifier.verify(unit, evidence)

        assert report.confidence_score > 0
        assert report.total_claims == 1


@pytest.mark.integration
async def test_coi_rules_can_be_switched_off():
    """B9: penalize_conflict_of_interest=False drops the COI rules but keeps
    the rest of the trust weighting; the default prompt is unchanged."""
    from cce.verification.verifier import _VERIFIER_PROMPT_NO_COI

    llm = MockLLMProvider(
        [
            LLMResponse(
                content=_make_valid_response(), model="m", stop_reason="end_turn"
            ),
            LLMResponse(
                content=_make_valid_response(), model="m", stop_reason="end_turn"
            ),
        ]
    )
    verifier = Verifier(llm)
    unit = make_content_unit(content="Claim [ev:ev_001].")
    evidence = [make_evidence(id="ev_001")]

    await verifier.verify(unit, evidence, penalize_conflict_of_interest=False)
    await verifier.verify(unit, evidence)

    off, on = llm.calls[0]["system"], llm.calls[1]["system"]
    assert off == _VERIFIER_PROMPT_NO_COI
    assert "COI-flagged" not in off and "[potential-COI]" not in off
    assert "SOURCE TRUST WEIGHTING" in off
    assert "[peer-reviewed]" in off and "[primary-source]" in off
    assert on == _VERIFIER_FULL_PROMPT
