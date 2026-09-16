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
# inline selected-top boundary and hierarchy-local naming schema v2 (public
# ports remain unchanged).
# They cover ready/valid hierarchy (including legal full-buffer simultaneous
# pop/push), unbuffered request/response, directional request/response FIFOs
# with a stateful mixed-port child, and source-authored aggregate bus/CSR
# hierarchy.
EXPECTED_ARTIFACTS = {
    ("hierarchical_protocol.zhl", "ProtocolTop"): (
        "6e6c27cf50bd5126097daed621d04d1e07b3bd55824b2fb708d31982fdc54a80",
        "c7a1b5054f9f28ab2f05d31fb85d896067c8c514a22e15f6bf56c36423136abd",
        19,
    ),
    ("hierarchical_request_response.zhl", "HierarchicalRequestResponse"): (
        "2fb5e7ce3a95471fded0bf9500c2c4314ee16edcf64e1ff53f37454317031706",
        "e032af175b863dc8fb4f9a9182643aa919649410a0399a03967a3a546fa01ca1",
        27,
    ),
    ("simple_dma.zhl", "SimpleDMA"): (
        # Request/response buffers retain their frozen conservative admission
        # rule and therefore use the explicitly distinct helper family after
        # ordinary ready/valid FIFOs gained full pop/push replacement.
        "51ba8bf730454115e500cc28af83b6981bdeedb221c04af274eaa9727d8344c9",
        "21780b9b39988716c6c68d798279202c3b8a65e7cbed513ce7564b90e5c0bd91",
        29,
    ),
    ("axi_csr_top.zhl", "AxiCsrTop"): (
        "5aff4cf6d89db8ac8d7e93093828979e27fac30c882c1449c9aee1a3bad1564b",
        "60c1182e80d36b630ef95023a72939ad5bc715d8575c3f8c01cd3b35fff9acd0",
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
