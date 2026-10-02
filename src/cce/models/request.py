"""Curation request data contracts.

A CurationRequest is the only required input to run the engine.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator

from cce.models.evidence import Evidence

# A path names a directory under the emit target and a URL segment, so it is
# one plain component: no separators, no "..", no leading dot (audit 2.1).
_PATH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

# An ID has to fit inside a [ev:ID] marker (parsing.EV_MARKER_RE), and the
# gate reads a "," inside one as several IDs.
_CITABLE_ID_RE = re.compile(r"[^\s\[\],]+")
# The form of the engine's own evidence IDs (discoverer.py); reserved, so a
# context ID never names a stored row.
_ENGINE_ID_RE = re.compile(r"ev_[0-9a-f]{12}")


_REDUCED_DATE_RE = re.compile(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")


def _parse_bound(value: str, *, end: bool = False) -> datetime:
    """ISO 8601 date or datetime -> aware datetime; no offset means UTC, as
    for a page's published date (CR-03). A bound with no time (``2024``,
    ``2024-06``, ``2024-06-01``) covers the whole year, month or day: ``end``
    gives its last instant, for an inclusive upper bound."""
    reduced = _REDUCED_DATE_RE.fullmatch(value)
    if reduced is None:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    year = int(reduced.group(1))
    month, day = (int(g) if g else None for g in reduced.groups()[1:])
    start = datetime(year, month or 1, day or 1, tzinfo=UTC)
    if not end:
        return start
    if day is not None:
        following = start + timedelta(days=1)
    elif month is not None:
        following = start.replace(year=year + (month == 12), month=month % 12 + 1)
    else:
        following = start.replace(year=year + 1)
    return following - timedelta(microseconds=1)


class CurationConstraints(BaseModel):
    """Filters applied to source discovery."""

    date_from: str | None = Field(
        default=None,
        description=(
            "ISO 8601 date or datetime, lower bound on a source's published "
            "date (no offset means UTC)"
        ),
    )
    date_to: str | None = Field(
        default=None,
        description=(
            "ISO 8601 date or datetime, upper bound on a source's published "
            "date (no offset means UTC)"
        ),
    )
    domains_allow: list[str] = Field(
        default_factory=list,
        description=(
            "Only include sources from these domains (narrows the policy's allow list)"
        ),
    )
    domains_deny: list[str] = Field(
        default_factory=list,
        description="Exclude sources from these domains (added to the policy's)",
    )
    jurisdiction: str | None = Field(
        default=None, description="Legal/regulatory jurisdiction filter"
    )

    @field_validator("date_from", "date_to")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        if v:
            try:
                _parse_bound(v)
            except ValueError:
                raise ValueError(
                    f"{v[:50]!r} is not an ISO 8601 date or datetime"
                ) from None
        return v

    def date_bounds(self) -> tuple[datetime | None, datetime | None]:
        """``(date_from, date_to)`` as aware datetimes (None when unset)."""
        return (
            _parse_bound(self.date_from) if self.date_from else None,
            _parse_bound(self.date_to, end=True) if self.date_to else None,
        )

    model_config = {"frozen": True}


class CurationRequest(BaseModel):
    """Input contract for a curation run."""

    MAX_SUBTOPIC_LENGTH: ClassVar[int] = 200

    topic: str = Field(
        ..., min_length=1, max_length=500, description="Primary topic to curate"
    )
    subtopics: list[str] = Field(
        default_factory=list, max_length=20, description="Optional subtopics to cover"
    )
    paths: list[str] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Output paths to generate, drawn from registered PathConfig",
    )
    audience: str = Field(
        default="general",
        max_length=100,
        description="Target audience (free-form or enum per product)",
    )
    constraints: CurationConstraints | None = Field(
        default=None, description="Discovery filters"
    )
    policy_id: str = Field(
        ..., min_length=1, description="Which SourcePolicy config to use"
    )
    taxonomy_id: str | None = Field(
        default=None,
        description="Which TaxonomyConfig to use (Phase 2, optional for Phase 1)",
    )
    path_config_id: str | None = Field(
        default=None,
        description="Which PathConfig to use (Phase 2, optional for Phase 1)",
    )
    risk_profile: str = Field(
        default="medium",
        pattern=r"^(low|medium|high)$",
        description="Maps to quality gate thresholds: low, medium, high",
    )
    context: list[Evidence] = Field(
        default_factory=list,
        description=(
            "Pinned evidence (B11): settled statements the caller already "
            "knows. Skips discovery, the 50-character fragment minimum and "
            "every cap; shown to the writer and verifier as settled context "
            "and citable as [ev:ID]. Carried on the job and its package, not "
            "written to the evidence store. IDs must be unique, contain no "
            "whitespace, '[', ']' or ',', and not take the engine's "
            "ev_<12 hex> form; excerpt_hash must be the SHA-256 hex of the "
            "excerpt."
        ),
    )

    @field_validator("paths")
    @classmethod
    def _paths_named_and_unique(cls, v: list[str]) -> list[str]:
        for path in v:
            if not _PATH_NAME_RE.fullmatch(path):
                raise ValueError(
                    f"path {path[:50]!r} must be 1-64 letters, digits, '_' or '-', "
                    "starting with a letter or digit"
                )
        # Everything downstream is keyed by path; a repeat would leave one
        # draft unrecorded (review of B7). Dropped, order kept.
        return list(dict.fromkeys(v))

    @field_validator("context")
    @classmethod
    def _context_citable(cls, v: list[Evidence]) -> list[Evidence]:
        seen: set[str] = set()
        for ev in v:
            if not _CITABLE_ID_RE.fullmatch(ev.id):
                raise ValueError(
                    f"context id {ev.id[:50]!r} can't be cited as [ev:ID]: no "
                    "whitespace, '[', ']' or ','"
                )
            if _ENGINE_ID_RE.fullmatch(ev.id):
                raise ValueError(
                    f"context id {ev.id!r} takes the engine's ev_<12 hex> form"
                )
            if ev.id in seen:
                raise ValueError(f"duplicate context id {ev.id!r}")
            seen.add(ev.id)
            if ev.excerpt_hash != hashlib.sha256(ev.excerpt.encode()).hexdigest():
                raise ValueError(
                    f"context {ev.id!r}: excerpt_hash is not the SHA-256 of excerpt"
                )
        return v

    @field_validator("subtopics")
    @classmethod
    def _subtopic_elements_bounded(cls, v: list[str]) -> list[str]:
        for s in v:
            if len(s) > cls.MAX_SUBTOPIC_LENGTH:
                raise ValueError(
                    f"subtopic exceeds {cls.MAX_SUBTOPIC_LENGTH} chars: {s[:50]}…"
                )
        return v

    model_config = {"frozen": True}
