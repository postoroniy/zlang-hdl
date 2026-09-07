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
# hierarchy-local naming schema v1 (public ports remain unchanged).
# They cover ready/valid hierarchy (including legal full-buffer simultaneous
# pop/push), unbuffered request/response, directional request/response FIFOs
# with a stateful mixed-port child, and source-authored aggregate bus/CSR
# hierarchy.
EXPECTED_ARTIFACTS = {
    ("hierarchical_protocol_m40.zhl", "ProtocolTop"): (
        "6e6c27cf50bd5126097daed621d04d1e07b3bd55824b2fb708d31982fdc54a80",
        "68a892ca58919d9d4e9258b57de6d19c3e09e14d0b50949cd3de5df993d69c6d",
        19,
    ),
    ("hierarchical_request_response_m40.zhl", "HierarchicalRequestResponse"): (
        "2fb5e7ce3a95471fded0bf9500c2c4314ee16edcf64e1ff53f37454317031706",
        "d477f309a02b3d36b792ad96fa0fae71f90a8c7e0556248197a574f162e32076",
        27,
    ),
    ("simple_dma_m40.zhl", "SimpleDMA"): (
        # Request/response buffers retain their frozen conservative admission
        # rule and therefore use the explicitly distinct helper family after
        # ordinary ready/valid FIFOs gained full pop/push replacement.
        "51ba8bf730454115e500cc28af83b6981bdeedb221c04af274eaa9727d8344c9",
        "5cb5513c8de5346b6032ae74d89a3d5a3f8ccf8c4086de8d9e2904c0c71ebe25",
        29,
    ),
    ("axi_csr_top.zhl", "AxiCsrTop"): (
        "7354ad915d8933d98ea45d42ad4c75d148ac26d61a98869f69a5dee063875d3a",
        "5a84aa7c578c358fd6b0358447f228eb23f0a6d0e87508dbc838f085f21559e8",
        77,
    ),
}


def _artifact(example: str, top: str) -> BackendArtifact:
    module = compile_source(
        (ROOT / "examples" / example).read_text(),
        top=top,
        include_clash=False,
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
        (ROOT / "examples" / "hierarchical_protocol_m40.zhl").read_text(),
        top="ProtocolTop",
        include_clash=False,
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
