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
# v3, Direct-SV DAG schema v2, and hierarchy-local naming schema v3.  The
# AXI/CSR case now retains shared nodes
# once while inlining single-use nodes; the other RTL bodies remain unchanged.
# They cover ready/valid hierarchy (including legal full-buffer simultaneous
# pop/push and reset-suppressed public handshakes), unbuffered
# request/response, directional request/response FIFOs with a stateful
# mixed-port child, and source-authored aggregate bus/CSR hierarchy.
# Manifest hashes use current structured origins, the always-leaf top ABI, and
# canonical selected-value normalization identities.  Protocol/AXI RTL remains
# byte-identical to the pre-recovery baseline.  SimpleDMA's old expected RTL
# hash was already stale on that baseline; the value below is the identical
# baseline/current artifact.  The request/response artifact additionally
# records the repaired one-field aggregate fold that the baseline could not
# emit.
EXPECTED_ARTIFACTS = {
    ("hierarchical_protocol.zhl", "ProtocolTop"): (
        "67ebdf5d5e204ace5eddf737c6cf5c6804ea3503d87f5353e15b7c7b1b89e238",
        "e93c47ef792518c9a99bc55f82a3de3cdcb040869c6316eb48deedb91c6dee62",
        19,
    ),
    ("hierarchical_request_response.zhl", "HierarchicalRequestResponse"): (
        "48254a7a2da27640922978654b1dde628e4e5251ed7288e0d18b6fd713a7e4ec",
        "bc5a746a034bfc87aad6251e7b6fe239d4ab9ebd840147580b0e2f75acda49df",
        27,
    ),
    ("simple_dma.zhl", "SimpleDMA"): (
        # Request/response buffers retain their frozen conservative admission
        # rule and therefore use the explicitly distinct helper family after
        # ordinary ready/valid FIFOs gained full pop/push replacement.
        "73cb85917c83bf1f396135e2a396467b027a2f812bdfb3518c101f4b5c8341d2",
        "2c80c38cbdace521e31f1c55d3265f07e1e6ca6f6dc3e594361d729ff6d7d9c2",
        29,
    ),
    ("axi_csr_top.zhl", "AxiCsrTop"): (
        "ce6becd67a964adb44c5c0f965d2262893215d72a1c3db51474bfd25728bd3f9",
        "842b78e48c6f9bd0fd269a57c0c9a6ad87e47fd47f9da929d8945a8503ae986a",
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
