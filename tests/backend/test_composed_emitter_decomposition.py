from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog.emitter import (
    SystemVerilogEmissionError,
    emit_artifact,
)
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]
VERILATOR = shutil.which("verilator")

# Captured after the always-leaf public-top ABI, explicit internal-driver
# normalization, connection-owned request/response admission/accounting, and
# inline selected-top boundary, hierarchy-local naming schema v2, and exact
# selected-value normalization schema v3, functional-region emission schema
# v3, Direct-SV DAG schema v1, backend typed-constant-folding schema v1, and
# hierarchy-local naming schema v3. The constant folder preserves typed values
# while compacting exact extended constants, so both the affected RTL text and
# its manifest are intentionally captured below. The AXI/CSR case retains shared nodes
# once while inlining single-use nodes; the other RTL bodies remain unchanged.
# ZL-048 makes child specialization suffixes declaration/binding/content-owned
# instead of parent-context-owned, and readable private FSM naming advances the
# naming schema to v5.  A baseline/current byte diff proved that the four RTL
# bodies below differ only in those child module/instance suffixes; binding
# counts and public ports are unchanged.
# They cover ready/valid hierarchy (including legal full-buffer simultaneous
# pop/push and reset-suppressed public handshakes), unbuffered
# request/response, directional request/response FIFOs with a stateful
# mixed-port child, and source-authored aggregate bus/CSR hierarchy.
EXPECTED_ARTIFACTS = {
    ("hierarchical_protocol.zhl", "ProtocolTop"): (
        "f5d21496e9bf9f2ab7ec2bc22425eed42112200c9ab1de1339bab221973377f9",
        "93fe3622edfcf7f4184ac2e11a06f39a7af1a3fbb3a7affa5c8173be11cd502a",
        19,
    ),
    ("hierarchical_request_response.zhl", "HierarchicalRequestResponse"): (
        "1db32dac9bbd1efdc3a4a320d224b764ddcf36d3823163b0e4c7e19597327d5f",
        "53490762ed38db2948b077383e087eb2ea598ad57a4471c00ec6208ba90f0f96",
        27,
    ),
    ("simple_dma.zhl", "SimpleDMA"): (
        # Request/response buffers retain their frozen conservative admission
        # rule and therefore use the explicitly distinct helper family after
        # ordinary ready/valid FIFOs gained full pop/push replacement.
        "e3bfb9419d22d605c8fa636e8e8c7b6e841eceaff3fa14271983eed8631ae3b2",
        "5300f768dc79b674ae3dc926e8bc1963ad187d9b2e6ec5421d9f447eb3c934d1",
        29,
    ),
    ("axi_csr_top.zhl", "AxiCsrTop"): (
        "7c4af9f2943a8b213fc4571db73fe60beff54e3a134ab6eafd0e0657b5a8516f",
        "9e8737b0df3344f198de940fe402b77c04e1117873cdde10be40e151791f90ea",
        77,
    ),
}


def _artifact(example: str, top: str) -> BackendArtifact:
    module = compile_source(
        (ROOT / "examples" / example).read_text(),
        top=top,
    ).ir
    return emit_artifact(module)


@pytest.mark.parametrize(("example", "top"), tuple(EXPECTED_ARTIFACTS))
def test_composed_extraction_preserves_sv_artifact_and_binding_bytes(
    example: str,
    top: str,
) -> None:
    artifact = _artifact(example, top)
    expected_hash, expected_json_hash, expected_bindings = EXPECTED_ARTIFACTS[
        (example, top)
    ]

    assert hashlib.sha256(artifact.text.encode()).hexdigest() == expected_hash
    assert artifact.artifact_hash == expected_hash
    assert len(artifact.bindings) == expected_bindings
    encoded = artifact.to_json()
    assert hashlib.sha256(encoded.encode()).hexdigest() == expected_json_hash
    restored = BackendArtifact.from_json(encoded)
    assert restored.to_json() == encoded
    assert restored.bindings == artifact.bindings


def test_composed_boundary_rejects_misaligned_child_elaboration() -> None:
    module = compile_source(
        (ROOT / "examples" / "hierarchical_protocol.zhl").read_text(),
        top="ProtocolTop",
    ).ir
    malformed = replace(module, children=module.children[:-1])

    with pytest.raises(
        SystemVerilogEmissionError,
        match="typed children.*elaborated instances",
    ):
        emit_artifact(malformed)


@pytest.mark.skipif(VERILATOR is None, reason="Verilator is required")
@pytest.mark.parametrize(("example", "top"), tuple(EXPECTED_ARTIFACTS))
def test_extracted_composed_sv_is_strict_verilator_lint_clean(
    example: str,
    top: str,
    tmp_path: Path,
) -> None:
    artifact = _artifact(example, top)
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(artifact.text)
    completed = subprocess.run(
        (
            VERILATOR or "verilator",
            "--lint-only",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--top-module",
            top,
            str(rtl),
        ),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
