"""Tests for verifier trust weighting and jurisdiction pass-through (M04)."""

import json

import pytest

from cce.llm.base import LLMResponse
from cce.verification.verifier import (
    VERIFIER_SYSTEM_PROMPT,
    Verifier,
    _VERIFIER_FULL_PROMPT,
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
            [LLMResponse(content=_make_valid_response(), model="mock", stop_reason="end_turn")]
        )
        verifier = Verifier(llm)
        unit = make_content_unit(content="Test claim [ev:ev_001].")
        evidence = [make_evidence(id="ev_001")]

        await verifier.verify(unit, evidence)

        system = llm.calls[0]["system"]
        assert "SOURCE TRUST WEIGHTING" in system

    async def test_system_prompt_contains_coi_instruction(self):
        llm = MockLLMProvider(
            [LLMResponse(content=_make_valid_response(), model="mock", stop_reason="end_turn")]
        )
        verifier = Verifier(llm)
        unit = make_content_unit(content="Test claim [ev:ev_001].")
        evidence = [make_evidence(id="ev_001")]

        await verifier.verify(unit, evidence)

        system = llm.calls[0]["system"]
        assert "COI-flagged sources" in system

    async def test_base_prompt_preserved(self):
        llm = MockLLMProvider(
            [LLMResponse(content=_make_valid_response(), model="mock", stop_reason="end_turn")]
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
            [LLMResponse(content=_make_valid_response(), model="mock", stop_reason="end_turn")]
        )
        verifier = Verifier(llm)
        unit = make_content_unit(content="Test claim [ev:ev_001].")
        evidence = [make_evidence(id="ev_001")]

        await verifier.verify(unit, evidence, jurisdiction="EU")

        user_msg = llm.calls[0]["messages"][0].content
        assert "Jurisdiction/scope: EU" in user_msg

    async def test_jurisdiction_absent_when_not_provided(self):
        llm = MockLLMProvider(
            [LLMResponse(content=_make_valid_response(), model="mock", stop_reason="end_turn")]
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
            [LLMResponse(content=_make_valid_response(), model="mock", stop_reason="end_turn")]
        )
        verifier = Verifier(llm)
        unit = make_content_unit(content="Test claim [ev:ev_001].")
        evidence = [make_evidence(id="ev_001")]

        report = await verifier.verify(unit, evidence)

        assert report.confidence_score > 0
        assert report.total_claims == 1
