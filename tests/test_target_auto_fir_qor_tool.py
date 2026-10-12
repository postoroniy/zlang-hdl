from __future__ import annotations

import json

import pytest

from tools.target_auto_fir_qor import build_evidence_payload
from zlang.target_planner import EVIDENCE_SCHEMA, load_qor_evidence


def _row(name: str, configuration: str, graph: str) -> dict[str, object]:
    return {
        "name": name,
        "configuration": configuration,
        "pipeline_configuration": (
            f"std.target.xilinx.series7.DSP48E1.{configuration}"
        ),
        "architecture_template_identity": (
            "std.arch.xilinx7_fir.Xilinx7SymmetricDSPCascade"
        ),
        "implementation_graph": graph,
        "lut": 20,
        "ff": 16,
        "dsp": 4,
        "bram": 0,
        "fmax_mhz": 123.5,
        "wns_ns": 1.9,
    }


def test_auto_fir_rows_generate_deterministic_exact_graph_evidence(
    tmp_path,
) -> None:
    rows = (
        _row("exact8", "multiply_output_registered", "exact-current"),
        _row("bounded_unregistered", "unregistered", "bounded-current"),
    )
    arguments = {
        "target_identity": "std.target.xilinx.xc7z030.xc7z030ffg676_1",
        "target_part": "xc7z030ffg676-1",
        "clock_period_ns": 10.0,
    }
    forward = build_evidence_payload(rows, **arguments)
    reverse = build_evidence_payload(tuple(reversed(rows)), **arguments)
    assert forward == reverse
    assert forward["schema"] == EVIDENCE_SCHEMA
    assert [
        item["key"]["implementation_graph_identity"]
        for item in forward["records"]
    ] == ["bounded-current", "exact-current"]

    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(forward))
    restored = load_qor_evidence(evidence)
    assert [item.key.implementation_graph_identity for item in restored] == [
        "bounded-current",
        "exact-current",
    ]


def test_auto_fir_evidence_rejects_duplicate_or_mismatched_rows() -> None:
    arguments = {
        "target_identity": "target",
        "target_part": "part",
        "clock_period_ns": 10.0,
    }
    duplicate = _row("bounded_a", "unregistered", "same")
    with pytest.raises(ValueError, match="unique graph identities"):
        build_evidence_payload((duplicate, duplicate), **arguments)

    mismatch = _row("bounded_b", "unregistered", "other")
    mismatch["pipeline_configuration"] = "pipeline.multiply_registered"
    with pytest.raises(ValueError, match="mismatched pipeline configuration"):
        build_evidence_payload((mismatch,), **arguments)
