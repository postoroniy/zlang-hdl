from __future__ import annotations

import json

from tools.signed_product_pipeline_qor import build_evidence_payload
from zlang.target_planner import EVIDENCE_SCHEMA, load_qor_evidence


TARGET_IDENTITY = "std.target.xilinx.xc7z030.xc7z030ffg676_1"
TARGET_PART = "xc7z030ffg676-1"
ARCHITECTURE_IDENTITY = (
    "std.arch.xilinx7_signed_product.Xilinx7SignedProductCascade"
)


def _row(
    mode: str,
    configuration: str,
    graph_identity: str,
    *,
    fmax_mhz: float,
    wns_ns: float,
) -> dict[str, object]:
    return {
        "mode": mode,
        "configuration": configuration,
        "pipeline_configuration": (
            f"std.target.xilinx.series7.DSP48E1.{configuration}"
        ),
        "implementation_graph": graph_identity,
        "lut": 221,
        "ff": 18,
        "dsp": 2,
        "bram": 0,
        "fmax_mhz": fmax_mhz,
        "wns_ns": wns_ns,
    }


def test_routed_rows_generate_deterministic_planner_evidence(tmp_path) -> None:
    rows = (
        _row(
            "imag", "multiply_registered", "imag-current-graph",
            fmax_mhz=104.789, wns_ns=0.457,
        ),
        _row(
            "real", "unregistered", "real-current-graph",
            fmax_mhz=82.740, wns_ns=-2.086,
        ),
    )
    arguments = {
        "target_identity": TARGET_IDENTITY,
        "target_part": TARGET_PART,
        "architecture_template_identity": ARCHITECTURE_IDENTITY,
        "clock_period_ns": 10.0,
    }
    forward = build_evidence_payload(rows, **arguments)
    reverse = build_evidence_payload(tuple(reversed(rows)), **arguments)
    assert forward == reverse
    assert json.dumps(forward, indent=2, sort_keys=True) == json.dumps(
        reverse, indent=2, sort_keys=True
    )
    assert forward["schema"] == EVIDENCE_SCHEMA
    records = forward["records"]
    assert [item["key"]["implementation_graph_identity"] for item in records] == [
        "real-current-graph", "imag-current-graph",
    ]
    assert records[0] == {
        "key": {
            "target_identity": TARGET_IDENTITY,
            "target_part": TARGET_PART,
            "architecture_template_identity": ARCHITECTURE_IDENTITY,
            "implementation_graph_identity": "real-current-graph",
            "pipeline_configuration_identity": (
                "std.target.xilinx.series7.DSP48E1.unregistered"
            ),
            "backend": "direct_systemverilog",
            "tool": "Vivado",
            "tool_version": "2024.2",
            "clock_period_ns": 10.0,
        },
        "stage": "routed_measurement",
        "lut": 221,
        "ff": 18,
        "dsp": 2,
        "bram": 0,
        "fmax_mhz": 82.740,
        "wns_ns": -2.086,
        "provenance": (
            "signed FFT real scalar cascade, routed Vivado checkpoint"
        ),
    }

    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(forward))
    restored = load_qor_evidence(evidence_path)
    assert len(restored) == 2
    assert restored[0].key.implementation_graph_identity == "real-current-graph"
    assert restored[1].key.implementation_graph_identity == "imag-current-graph"

