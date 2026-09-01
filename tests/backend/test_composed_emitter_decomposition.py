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
# normalization, and connection-owned request/response admission/accounting.
# They cover ready/valid hierarchy (including legal full-buffer simultaneous
# pop/push), unbuffered request/response, directional request/response FIFOs
# with a stateful mixed-port child, and source-authored aggregate bus/CSR
# hierarchy.
EXPECTED_ARTIFACTS = {
    ("hierarchical_protocol_m40.zl", "ProtocolTop"): (
        "4b6a498ba320ef15c8556a4cbf75c468d9bb88793446157b16d136c0251f881e",
        "87d0e962cae3b885015fb30d0d16e6a64ad8abf230780b766ef0734d80cbe694",
        19,
    ),
    ("hierarchical_request_response_m40.zl", "HierarchicalRequestResponse"): (
        "d3c85fd06fdcb7cb0d4dea986f3cf777e0a6a3bf134a9fe83015a56f239cbece",
        "4d7c57c2d012860e27774d85ac19d519f583bd69614c463f06e48ab613a0b7cf",
        27,
    ),
    ("simple_dma_m40.zl", "SimpleDMA"): (
        # Request/response buffers retain their frozen conservative admission
        # rule and therefore use the explicitly distinct helper family after
        # ordinary ready/valid FIFOs gained full pop/push replacement.
        "20b0fd12233bc6f7bf8519693f03a676c42af6d4e97542dc2accb6cb056d0975",
        "8304bd9129a86214637d8604cc34ba7ae5eec655d15892c567e81871e426e477",
        29,
    ),
    ("axi_csr_top.zl", "AxiCsrTop"): (
        "26593d3f28137342b3a75921cbb15dc921cff5b61b2c7a4dc25e610e9b197d28",
        "0ba1d70d9295e16a07bf4659a41e3dc1944c181808de525ec092797c1e6c4b57",
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
        (ROOT / "examples" / "hierarchical_protocol_m40.zl").read_text(),
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
