"""Pipeline output serialization.

Converts PipelineResult (mix of Pydantic models and dataclasses) into a
JSON-serializable dict, and writes structured output to disk.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cce.orchestrator.pipeline import PipelineResult


def serialize_result(result: PipelineResult) -> dict[str, Any]:
    """Convert a PipelineResult into a JSON-serializable dict."""
    return {
        "status": result.job.status.value,
        "job": _serialize(result.job),
        "package": _serialize(result.package) if result.package else None,
        "gate_results": [_serialize_gate(gr) for gr in result.gate_results],
    }


def _serialize_gate(gr: Any) -> dict[str, Any]:
    """Serialize a GateResult dataclass, including its nested report."""
    d = dataclasses.asdict(gr)
    # Fix enum values
    d["decision"] = gr.decision.value
    return _convert_values(d)


def _serialize(obj: Any) -> Any:
    """Recursively serialize Pydantic models, dataclasses, and primitives."""
    if obj is None:
        return None
    if isinstance(obj, BaseModel):
        return _convert_values(obj.model_dump(mode="python"))
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _convert_values(dataclasses.asdict(obj))
    if isinstance(obj, list):
        return [_serialize(item) for item in obj]
    if isinstance(obj, dict):
        return _convert_values(obj)
    return _convert_value(obj)


def _convert_values(d: dict) -> dict:
    """Walk a dict and convert non-JSON-native types."""
    return {k: _convert_value(v) for k, v in d.items()}


def _convert_value(v: Any) -> Any:
    if isinstance(v, dict):
        return _convert_values(v)
    if isinstance(v, list):
        return [_convert_value(item) for item in v]
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, Path):
        return str(v)
    return v


def write_output(result: PipelineResult, output_dir: Path) -> Path:
    """Write full pipeline output to a run directory.

    Creates:
        <output_dir>/<run_id>/result.json  -- complete serialized result
        <output_dir>/<run_id>/content.md   -- rendered content for reading
        <output_dir>/<run_id>/evidence.json -- evidence objects only
        <output_dir>/<run_id>/verification.json -- gate results & reports

    Returns the run directory path.
    """
    run_id = result.package.lineage.run_id if result.package else result.job.id
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    full = serialize_result(result)

    # result.json -- everything
    (run_dir / "result.json").write_text(
        json.dumps(full, indent=2, ensure_ascii=False)
    )

    # content.md -- just the readable content
    content_parts: list[str] = []
    if result.package:
        for unit in result.package.units:
            content_parts.append(f"<!-- path: {unit.path} -->\n")
            content_parts.append(unit.content)
            content_parts.append("\n\n---\n")
            content_parts.append(f"**Citations:** {len(unit.citations)}")
            content_parts.append(f"  |  **Claim mappings:** {len(unit.evidence_map)}")
            content_parts.append(
                f"  |  **Confidence:** {unit.scores.confidence}"
                f"  |  **Coverage:** {unit.scores.coverage}"
                f"  |  **Diversity:** {unit.scores.source_diversity}\n"
            )
    (run_dir / "content.md").write_text("\n".join(content_parts) or "(no content)")

    # evidence.json -- evidence objects only
    evidence_data = (
        [_serialize(e) for e in result.package.evidence] if result.package else []
    )
    (run_dir / "evidence.json").write_text(
        json.dumps(evidence_data, indent=2, ensure_ascii=False)
    )

    # verification.json -- gate results with full reports
    verification_data = {
        "gate_results": full["gate_results"],
        "final_status": result.job.status.value,
        "final_confidence": (
            result.package.scores.confidence if result.package else 0.0
        ),
    }
    (run_dir / "verification.json").write_text(
        json.dumps(verification_data, indent=2, ensure_ascii=False)
    )

    return run_dir
