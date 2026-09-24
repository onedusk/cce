"""Structured-output schemas stay generic and within the API's JSON-schema subset."""

from __future__ import annotations

import pytest

from cce.synthesis.writer import WRITER_OUTPUT_SCHEMA
from cce.verification.verifier import VERIFIER_OUTPUT_SCHEMA

pytestmark = pytest.mark.unit

# Keywords structured outputs does not support (API reference, 2026-06).
_UNSUPPORTED = {
    "minimum",
    "maximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "minItems",
}
# The only enum allowed: the verifier's fixed assessment vocabulary.
_ALLOWED_ENUMS = {
    ("supported", "unsupported", "uncited", "leakage", "conflict", "gap_acknowledged")
}


def _walk(node: dict):
    yield node
    for child in node.get("properties", {}).values():
        yield from _walk(child)
    if "items" in node:
        yield from _walk(node["items"])


@pytest.mark.parametrize(
    "schema", [WRITER_OUTPUT_SCHEMA, VERIFIER_OUTPUT_SCHEMA], ids=["writer", "verifier"]
)
def test_schema_is_generic_and_supported(schema: dict) -> None:
    for node in _walk(schema):
        assert not (_UNSUPPORTED & node.keys()), node
        if node.get("type") == "object":
            # Every object must be closed and fully required.
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
        if "enum" in node:
            assert tuple(node["enum"]) in _ALLOWED_ENUMS, node["enum"]


def test_writer_schema_keeps_evidence_ids_free_form() -> None:
    """No evidence IDs as enums: the schema must not depend on the run."""
    items = WRITER_OUTPUT_SCHEMA["properties"]["citations_used"]["items"]
    assert items == {"type": "string"}
