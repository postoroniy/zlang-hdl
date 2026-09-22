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
# v3, Direct-SV DAG schema v1, and hierarchy-local naming schema v3.  The
# AXI/CSR case now retains shared nodes
# once while inlining single-use nodes; the other RTL bodies remain unchanged.
# They cover ready/valid hierarchy (including legal full-buffer simultaneous
# pop/push and reset-suppressed public handshakes), unbuffered
# request/response, directional request/response FIFOs with a stateful
# mixed-port child, and source-authored aggregate bus/CSR hierarchy.
EXPECTED_ARTIFACTS = {
    ("hierarchical_protocol.zhl", "ProtocolTop"): (
        "67ebdf5d5e204ace5eddf737c6cf5c6804ea3503d87f5353e15b7c7b1b89e238",
        "05833fb5301789a6603dc4d482b7987418c625ee11114ec2b4b09e0fd1e78bb7",
        19,
    ),
    ("hierarchical_request_response.zhl", "HierarchicalRequestResponse"): (
        "2fb5e7ce3a95471fded0bf9500c2c4314ee16edcf64e1ff53f37454317031706",
        "d05b060352e3f6731e551ef0c17d4cc3f083e85e564c1ae87cee03aab1323995",
        27,
    ),
    ("simple_dma.zhl", "SimpleDMA"): (
        # Request/response buffers retain their frozen conservative admission
        # rule and therefore use the explicitly distinct helper family after
        # ordinary ready/valid FIFOs gained full pop/push replacement.
        "02c43b29110ea89358a40c6513cb4c0f2db64cd95cc442da099fdd4f317b92fd",
        "be82555d4f91901664fd5be024c11b56c023e6fc8a6d42c841c25f673e61cf8e",
        29,
    ),
    ("axi_csr_top.zhl", "AxiCsrTop"): (
        "ce6becd67a964adb44c5c0f965d2262893215d72a1c3db51474bfd25728bd3f9",
        "b55f487eff1af135a5e2eb7761a79306f6bd252e4f413e99ad1c55ac541ff9f1",
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
