"""Taxonomy data contracts.

A TaxonomyConfig defines the classification axes for tagging evidence.
Each Dimension is one axis with a fixed set of valid values.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Dimension(BaseModel):
    """A single axis of a taxonomy (e.g., 'well-being dimension')."""

    id: str = Field(description="Machine identifier (e.g., 'emotional')")
    name: str = Field(description="Display name (e.g., 'Emotional Well-Being')")
    description: str | None = Field(
        default=None, description="What this dimension measures"
    )
    values: list[str] = Field(
        description="Valid classification values (e.g., ['primary', 'secondary', 'none'])"
    )

    model_config = {"frozen": True}


class TaxonomyConfig(BaseModel):
    """A complete taxonomy definition, loaded from YAML."""

    id: str = Field(description="Registry key (e.g., 'wellbeing-8d')")
    name: str = Field(description="Human-readable name")
    dimensions: list[Dimension] = Field(
        description="The taxonomy axes (at least one required)"
    )

    model_config = {"frozen": True}

    def dimension_ids(self) -> list[str]:
        """Return all dimension IDs in order."""
        return [d.id for d in self.dimensions]

    def get_dimension(self, dimension_id: str) -> Dimension | None:
        """Look up a dimension by ID."""
        for d in self.dimensions:
            if d.id == dimension_id:
                return d
        return None
