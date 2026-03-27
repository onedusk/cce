"""Taxonomy tagging subsystem.

Provides the TaxonomyPlugin protocol and reference implementations.
"""

from cce.tagging.base import TaggingResult, TaxonomyPlugin, TaxonomyUnavailableError

__all__ = ["TaggingResult", "TaxonomyPlugin", "TaxonomyUnavailableError"]
