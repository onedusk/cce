"""Tests for cce.verification.verifier — verification report and LLM response parsing."""

import json

import pytest

from cce.llm.base import LLMResponse
from cce.verification.verifier import VERIFIER_SYSTEM_PROMPT, Verifier, VerificationReport
from tests.conftest import MockLLMProvider, make_content_unit, make_evidence


# ---------------------------------------------------------------------------
# VerificationReport.pass_rate
# ---------------------------------------------------------------------------


class TestPassRate:
    pytestmark = pytest.mark.unit

    def test_report_pass_rate_all_supported(self):
        report = VerificationReport(total_claims=5, supported=5)
        assert report.pass_rate == 1.0

    def test_report_pass_rate_mixed(self):
        report = VerificationReport(
            total_claims=10, supported=6, gaps_acknowledged=2
        )
        assert report.pass_rate == 0.8

    def test_report_pass_rate_zero_claims(self):
        report = VerificationReport(total_claims=0, supported=0)
        assert report.pass_rate == 0.0


# ---------------------------------------------------------------------------
# Verifier._parse_response — unit tests (sync)
# ---------------------------------------------------------------------------


def _make_valid_verifier_json(
    *,
    total: int = 10,
    supported: int = 8,
    unsupported: int = 0,
    uncited: int = 0,
    leakage: int = 0,
    conflicts: int = 0,
    gaps: int = 2,
) -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim": f"Claim {i}",
                    "citation_ids": ["ev_001"],
                    "assessment": "supported",
                    "explanation": "Matches evidence",
                    "suggestion": "",
                }
                for i in range(supported)
            ],
            "summary": {
                "total_claims": total,
                "supported": supported,
                "unsupported": unsupported,
                "uncited": uncited,
                "leakage": leakage,
                "conflicts": conflicts,
                "gaps_acknowledged": gaps,
            },
            "overall_feedback": "All good.",
            "contradictions": [],
        }
    )


class TestParseResponse:
    pytestmark = pytest.mark.unit

    def _verifier(self) -> Verifier:
        return Verifier(MockLLMProvider([]))

    def test_parse_response_valid_json(self):
        raw = _make_valid_verifier_json()
        report = self._verifier()._parse_response(raw)
        assert len(report.claims) == 8
        assert report.total_claims == 10
        assert report.supported == 8
        assert report.gaps_acknowledged == 2
        assert report.overall_feedback == "All good."
        assert report.contradictions == []
        assert report.confidence_score > 0

    def test_parse_response_non_json(self):
        report = self._verifier()._parse_response("This is not JSON at all")
        assert report.confidence_score == 0.0

    def test_parse_response_missing_summary(self):
        raw = json.dumps(
            {
                "claims": [
                    {"claim": "A", "assessment": "supported"},
                    {"claim": "B", "assessment": "unsupported"},
                ],
                "overall_feedback": "Some issues.",
                "contradictions": [],
            }
        )
        report = self._verifier()._parse_response(raw)
        # No summary → total_claims defaults to len(claims)
        assert report.total_claims == 2


# ---------------------------------------------------------------------------
# Confidence calculation — unit tests (sync)
# ---------------------------------------------------------------------------


class TestConfidence:
    pytestmark = pytest.mark.unit

    def _confidence(self, **kwargs) -> float:
        raw = _make_valid_verifier_json(**kwargs)
        return Verifier(MockLLMProvider([]))._parse_response(raw).confidence_score

    def test_confidence_no_penalties(self):
        c = self._confidence(total=10, supported=8, gaps=2, leakage=0, conflicts=0)
        assert c == pytest.approx(1.0)

    def test_confidence_leakage_penalty(self):
        # base = (7+1)/10 = 0.8, penalty = max(0, 1 - (2/10)*1.5) = 0.7
        # confidence = 0.8 * 0.7 = 0.56
        c = self._confidence(
            total=10, supported=7, gaps=1, leakage=2, unsupported=0, conflicts=0
        )
        assert c == pytest.approx(0.56, abs=0.01)

    def test_confidence_conflict_penalty(self):
        # base = (8+2)/10 = 1.0, conflict penalty = *0.9
        c = self._confidence(total=10, supported=8, gaps=2, conflicts=1, leakage=0)
        assert c == pytest.approx(0.9, abs=0.01)

    def test_confidence_both_penalties(self):
        # base = (7+1)/10 = 0.8
        # leakage: 0.8 * max(0, 1 - (1/10)*1.5) = 0.8 * 0.85 = 0.68
        # conflict: 0.68 * 0.9 = 0.612
        c = self._confidence(
            total=10, supported=7, gaps=1, leakage=1, conflicts=1, unsupported=1
        )
        assert c == pytest.approx(0.612, abs=0.01)

    def test_confidence_clamped(self):
        # Even with extreme leakage, confidence should not go below 0
        c = self._confidence(total=10, supported=0, gaps=0, leakage=10, conflicts=5)
        assert 0.0 <= c <= 1.0


# ---------------------------------------------------------------------------
# _format_evidence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_evidence():
    ev1 = make_evidence(id="ev_001", url="https://a.com", excerpt="Excerpt one.")
    ev2 = make_evidence(id="ev_002", url="https://b.com", excerpt="Excerpt two.")
    result = Verifier._format_evidence([ev1, ev2])
    assert "[ev_001] (URL: https://a.com)" in result
    assert "[ev_002] (URL: https://b.com)" in result
    assert "Excerpt one." in result
    assert "Excerpt two." in result


# ---------------------------------------------------------------------------
# verify() — integration test (async, mocked LLM)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_verify_sends_correct_prompt():
    valid_response = _make_valid_verifier_json()
    llm = MockLLMProvider(
        [LLMResponse(content=valid_response, model="mock", stop_reason="end_turn")]
    )
    verifier = Verifier(llm)

    unit = make_content_unit(content="AI models are powerful [ev:ev_001].")
    evidence = [make_evidence(id="ev_001")]

    await verifier.verify(unit, evidence)

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["system"] == VERIFIER_SYSTEM_PROMPT
    assert call["temperature"] == 0.1
    assert call["max_tokens"] == 16384
    user_msg = call["messages"][0].content
    assert "AI models are powerful" in user_msg
    assert "[ev_001]" in user_msg
