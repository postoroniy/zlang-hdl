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
# ZL-050 makes reusable standard-bus children inherit the enclosing exact
# clock/reset contract.  For the AXI/CSR witness this changes only the private
# child-specialization suffixes (and the corresponding binding paths); a
# baseline/current byte diff proved that ports, logic, reset branches, and the
# 77-binding public shape are unchanged.
# They cover ready/valid hierarchy (including legal full-buffer simultaneous
# pop/push and reset-suppressed public handshakes), unbuffered
# request/response, directional request/response FIFOs with a stateful
# mixed-port child, and source-authored aggregate bus/CSR hierarchy.
# The concise-example pass changed only source-owned binding locations for
# ProtocolTop and SimpleDMA. Their RTL digests and binding counts remain fixed;
# the JSON digests below deliberately bind the updated source-map origins.
# ZL-066 gives packed struct fields explicit typed sizes. A legacy/current
# renderer audit reproduced every prior digest: only those casts changed RTL,
# with artifact/build hash links changing in manifests. Public ABI, origins,
# semantic bindings and their counts are unchanged; ProtocolTop is byte-stable.
EXPECTED_ARTIFACTS = {
    ("hierarchical_protocol.zhl", "ProtocolTop"): (
        "f5d21496e9bf9f2ab7ec2bc22425eed42112200c9ab1de1339bab221973377f9",
        "27b5be46635bc51a1ee27525a50e8088a96927cc96ba6e9624da246f43cb50e5",
        19,
    ),
    ("hierarchical_request_response.zhl", "HierarchicalRequestResponse"): (
        "390546313c4a6390175ed575a42e344608987d9a2d2d07b2b9176bc2c6c46ef6",
        "08284b7fd3441d18a21b06177b17c4ff74d1d38af096d8cf53a1de12786b3eca",
        27,
    ),
    ("simple_dma.zhl", "SimpleDMA"): (
        # Request/response buffers retain their frozen conservative admission
        # rule and therefore use the explicitly distinct helper family after
        # ordinary ready/valid FIFOs gained full pop/push replacement.
        "222ccfa5783d0b57bcc411b78e5dc9292738df858078a323aafb14aaa7e072ff",
        "c89ce516749c1aba655703e2453dcf359c3c0c1bda53c393c92605ed21bef25a",
        29,
    ),
    ("axi_csr_top.zhl", "AxiCsrTop"): (
        "caf942ee439d84901a92b1746fafd49a0e690937f58411c84a224a6953fa9e65",
        "3b70d602e02729b88d14eb9b7217f0015dffe82e861740f4134754d63c439df1",
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
