"""Formal harnesses must use the backend's exact public HDL port names."""

from __future__ import annotations

import shutil

import pytest

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
    compilation = compile_source(source)
    artifact = emit_formal_artifact(
        compilation.ir,
        build_recursive_formal_design(compilation.ir),
    )
    connected = connect_formal_design(compilation.formal_design, artifact)
    return compilation, artifact, connected




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
