"""Formal harnesses must use the backend's exact public HDL port names."""

from __future__ import annotations

import shutil

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import (
    emit_artifact as emit_systemverilog_artifact,
    emit_formal_artifact,
)
from zlang.compiler import compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_harness,
    run_verilog_formal,
)
from zlang.ir.equivalence import SignalRole
from zlang.ir.formal import FormalStatus


HARDWARE = """
struct Pair { high : u4 low : u4 }

module ReservedFormalPorts {
    clock clk reset rst
    in input : u4
    in bus : Pair
    out packed : bits<8>

    packed = concat(input, bus.low)
}
"""


VERIFIED = HARDWARE.rsplit("}", 1)[0] + """
    assert packed_shape {
        packed == concat(input, bus.low)
    }
}
"""


def _formal_artifact(source: str):
    compilation = compile_source(source, include_clash=False)
    artifact = emit_formal_artifact(
        compilation.ir,
        build_recursive_formal_design(compilation.ir),
    )
    connected = connect_formal_design(compilation.formal_design, artifact)
    return compilation, artifact, connected


def test_formal_manifest_uses_exact_mangled_scalar_and_aggregate_leaf_ports() -> None:
    compilation, artifact, connected = _formal_artifact(VERIFIED)
    bindings = {item.semantic_signal_id: item for item in artifact.bindings}

    # ``input`` and ``packed`` are legal ZLang port names but SystemVerilog
    # reserved words.  Formal metadata must publish the exact same physical
    # projection as the production TopPhysicalABI emitter.
    assert bindings["port:input"].rtl_path == "zlang_input"
    assert bindings["port:input"].role is SignalRole.INPUT
    assert bindings["port:packed"].rtl_path == "zlang_packed"
    assert bindings["port:packed"].role is SignalRole.OUTPUT
    assert "input wire logic [3:0] zlang_input" in artifact.text
    assert "output logic [7:0] zlang_packed" in artifact.text

    # The same mapping is authoritative for flattened aggregate leaves.  The
    # packed semantic root remains deliberately unavailable at the public ABI.
    assert bindings["port:bus"].rtl_path == ""
    assert not bindings["port:bus"].physical_available
    assert bindings["port:bus.high"].rtl_path == "bus_high"
    assert bindings["port:bus.low"].rtl_path == "bus_low"
    assert "input wire logic [3:0] bus_high" in artifact.text
    assert "input wire logic [3:0] bus_low" in artifact.text

    harness = emit_harness(connected, depth=4)
    assert ".zlang_input(zlang_input)" in harness
    assert ".zlang_packed(zlang_packed)" in harness
    assert ".bus_high(bus_high)" in harness
    assert ".bus_low(bus_low)" in harness
    assert ".input(input)" not in harness
    assert ".packed(packed)" not in harness

    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.module == artifact.module
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.formal_artifact_hash == artifact.formal_artifact_hash
    assert restored.bindings == artifact.bindings
    assert restored.binding_map() == artifact.binding_map()

    # Verification is an overlay: neither production backend may change its
    # text or artifact identity merely because the source includes a goal.
    plain = compile_source(HARDWARE, include_clash=False)
    plain_sv = emit_systemverilog_artifact(plain.ir)
    verified_sv = emit_systemverilog_artifact(compilation.ir)
    assert verified_sv.text == plain_sv.text
    assert verified_sv.artifact_hash == plain_sv.artifact_hash

    plain_clash = emit_clash_artifact(plain.ir)
    verified_clash = emit_clash_artifact(compilation.ir)
    assert verified_clash.text == plain_clash.text
    assert verified_clash.artifact_hash == plain_clash.artifact_hash
    clash_bindings = {
        item.semantic_signal_id: item.rtl_path
        for item in verified_clash.bindings
    }
    assert clash_bindings["port:input"] == "zlang_input"
    assert clash_bindings["port:packed"] == "zlang_packed"
    assert clash_bindings["port:bus.high"] == "bus_high"
    assert clash_bindings["port:bus.low"] == "bus_low"


@pytest.mark.skipif(
    not all(
        shutil.which(tool)
        for tool in ("yosys", "sby", "yosys-smtbmc", "z3")
    ),
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
def test_reserved_public_ports_pass_and_mutation_fails_with_real_solver(
    tmp_path,
) -> None:
    _, _, connected = _formal_artifact(VERIFIED)
    property_ = next(
        item for item in connected.properties
        if item.generated_from == "verification-assert:$module:packed_shape"
    )
    passed = run_verilog_formal(
        emit_harness(connected, depth=4),
        top="ReservedFormalPorts__m35_formal",
        property_id=property_.id,
        depth=4,
        systemverilog=True,
        source_origin=property_.source_origin,
        work_directory=tmp_path / "pass",
    )
    assert passed.status is FormalStatus.BOUNDED_PASS
    assert passed.counterexample is None

    mutated_source = VERIFIED.replace(
        "packed = concat(input, bus.low)",
        "packed = concat(bus.low, input)",
        1,
    )
    assert mutated_source != VERIFIED
    _, _, mutated_connected = _formal_artifact(mutated_source)
    mutated_property = next(
        item for item in mutated_connected.properties
        if item.generated_from == "verification-assert:$module:packed_shape"
    )
    failed = run_verilog_formal(
        emit_harness(mutated_connected, depth=4),
        top="ReservedFormalPorts__m35_formal",
        property_id=mutated_property.id,
        depth=4,
        systemverilog=True,
        source_origin=mutated_property.source_origin,
        work_directory=tmp_path / "mutation",
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.counterexample is not None
    assert failed.counterexample.raw_trace
    assert failed.source_origin == mutated_property.source_origin
