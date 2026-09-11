from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.ir.hierarchy import build_hierarchy_index, specialization_fingerprint
from zlang.opt import lower, restore
from zlang.opt.lowering import CanonicalizationError
from zlang.ir.module import PortDirection
from zlang.ir.types import UIntType
from zlang.semantic import SemanticError
from zlang.simulate import simulate_cycles
from zlang.toolchain import lint_with_verilator


SOURCE = """
module StatefulRvLane {
    clock clk
    reset rst
    in input : rv<u8>
    out output : rv<u8>

    reg tag : u8 = 0
    output.payload = truncate<8>(input.payload + tag)
    output.valid = input.valid
    input.ready = output.ready
    when input.transfer { tag <- truncate<8>(tag + 1) }
}

module StatefulRvArray<K=1> {
    clock clk
    reset rst
    in input0 : rv<u8>
    in input1 : rv<u8>
    out output0 : rv<u8>
    out output1 : rv<u8>

    inst lane[2] : StatefulRvLane
    connect input0 -> lane[0].input
    connect lane[0].output -> output0
    connect input1 -> lane[K].input
    connect lane[1].output -> output1
}
"""


GENERATED_SOURCE = """
module Source {
    clock clk reset rst
    out tx : rv<u8>
    tx.payload = 1
    tx.valid = 1
}
module Sink {
    clock clk reset rst
    in rx : rv<u8>
    rx.ready = 1
}
module GeneratedRvArrays {
    clock clk reset rst
    inst source[2] : Source
    inst sink[2] : Sink
    generate(i in 0..2) { connect source[i].tx -> sink[i].rx }
}
"""


def _module():
    return compile_source(SOURCE).ir


def _cycles() -> list[dict[str, object]]:
    return [
        {
            "input0": {"payload": 0, "valid": 0},
            "input1": {"payload": 0, "valid": 0},
            "output0": {"ready": 1},
            "output1": {"ready": 1},
        },
        {
            "input0": {"payload": 10, "valid": 1},
            "input1": {"payload": 20, "valid": 1},
            "output0": {"ready": 1},
            "output1": {"ready": 1},
        },
        {
            "input0": {"payload": 10, "valid": 1},
            "input1": {"payload": 20, "valid": 1},
            "output0": {"ready": 0},
            "output1": {"ready": 1},
        },
        {
            "input0": {"payload": 10, "valid": 1},
            "input1": {"payload": 20, "valid": 1},
            "output0": {"ready": 1},
            "output1": {"ready": 1},
        },
        {
            "input0": {"payload": 10, "valid": 1},
            "input1": {"payload": 20, "valid": 1},
            "output0": {"ready": 1},
            "output1": {"ready": 1},
        },
        {
            "input0": {"payload": 10, "valid": 1},
            "input1": {"payload": 20, "valid": 1},
            "output0": {"ready": 1},
            "output1": {"ready": 1},
        },
    ]


def test_ready_valid_array_elaboration_and_canonical_identity_are_exact() -> None:
    module = _module()
    restored = restore(lower(module))
    hierarchy = build_hierarchy_index(module)

    assert restored == module
    assert [item.instance.name for item in module.elaborated_instances] == [
        "lane[0]", "lane[1]",
    ]
    assert len({item.instance_identity for item in module.elaborated_instances}) == 2
    assert len({item.specialization_identity for item in module.elaborated_instances}) == 1
    assert [item.physical_name for item in hierarchy.children_of((module.name,))] == [
        "lane[0]", "lane[1]",
    ]
    assert [
        (edge.source.owner, edge.source.name,
         edge.destination.owner, edge.destination.name)
        for edge in module.hierarchical_connections
    ] == [
        ("StatefulRvArray", "input0", "lane[0]", "input"),
        ("lane[0]", "output", "StatefulRvArray", "output0"),
        ("StatefulRvArray", "input1", "lane[1]", "input"),
        ("lane[1]", "output", "StatefulRvArray", "output1"),
    ]


def test_canonical_reused_specialization_requires_exact_port_and_state_content() -> None:
    canonical = lower(_module())
    second = canonical.children[1]
    mutations = (
        replace(
            second,
            ports=(replace(second.ports[0], type=UIntType(16)), *second.ports[1:]),
        ),
        replace(
            second,
            registers=(
                replace(second.registers[0], type=UIntType(16)),
                *second.registers[1:],
            ),
        ),
    )
    for mutated in mutations:
        with pytest.raises(
            CanonicalizationError,
            match="specialization identity .* incompatible typed .* content",
        ):
            restore(replace(
                canonical,
                children=(canonical.children[0], mutated),
            ))


def test_canonical_reused_specialization_accepts_deepcopied_equivalent_child() -> None:
    canonical = lower(_module())
    reconstructed = replace(
        deepcopy(canonical.children[0]),
        source_identity="/relocated/StatefulRvLane.zhl",
        source_hash="f" * 64,
    )
    restored = restore(replace(
        canonical,
        children=(canonical.children[0], reconstructed),
    ))

    assert specialization_fingerprint(restored.children[0]) == (
        specialization_fingerprint(restored.children[1])
    )


def test_generated_ready_valid_array_connections_resolve_binder_physically() -> None:
    module = compile_source(GENERATED_SOURCE).ir
    assert [
        (edge.source.owner, edge.destination.owner)
        for edge in module.hierarchical_connections
    ] == [("source[0]", "sink[0]"), ("source[1]", "sink[1]")]


def test_ready_valid_array_simulation_preserves_independent_state_stall_and_reset() -> None:
    result = simulate_cycles(
        _module(), _cycles(), reset=[True, False, False, False, True, False]
    )

    assert [item["output0"]["payload"] for item in result] == [0, 10, 11, 11, 10, 10]
    assert [item["output1"]["payload"] for item in result] == [0, 20, 21, 22, 20, 20]
    assert result[2]["input0"]["ready"] == 0
    assert result[2]["input1"]["ready"] == 1
    assert result[2]["output0"]["transfer"] == 0
    assert result[2]["output1"]["transfer"] == 1




@pytest.mark.parametrize(
    ("replacement", "diagnostic"),
    (
        ("lane[2].input", "index 2 is out of range"),
        ("lane[select].input", "requires a compile-time index"),
        ("lane.input", "requires a compile-time index"),
    ),
)
def test_invalid_ready_valid_array_selector_is_precise(
    replacement: str, diagnostic: str,
) -> None:
    source = SOURCE.replace("lane[0].input", replacement)
    if "select" in replacement:
        source = source.replace(
            "in input0 : rv<u8>", "in input0 : rv<u8>\n    in select : u1"
        )
    with pytest.raises(SemanticError, match=diagnostic):
        compile_source(source)


@pytest.mark.parametrize(
    ("source", "diagnostic"),
    (
        (
            "module Mixed { clock clk reset rst in rx:rv<u8> in enable:bit "
            "out tx:rv<u8> rx.ready=tx.ready tx.payload=rx.payload "
            "tx.valid=rx.valid } module Top { clock clk reset rst "
            "inst lane[2]:Mixed }",
                "input 'enable' has no compile-time indexed binding",
        ),
        (
            SOURCE.replace(
                "connect input0 -> lane[0].input",
                "connect input0 -> lane[0].input { buffer 1 }",
            ),
            "must be direct",
        ),
    ),
)
def test_ready_valid_array_scope_boundaries_are_explicit(
    source: str, diagnostic: str,
) -> None:
    with pytest.raises(SemanticError, match=diagnostic):
        compile_source(source)


@pytest.mark.parametrize("storage", ("memory", "rom"))
def test_ready_valid_array_rejects_memory_and_rom_children(storage: str) -> None:
    declaration = (
        "memory table:mem<u8,2>{ read_latency 1 collision read_first } "
        "table.read_address=0 table.write_enable=0 "
        "table.write_address=0 table.write_data=0 "
        if storage == "memory" else
        "rom table:rom<u8,2>{ read_latency 1 init generate(i in 0..2) i } "
        "table.read_address=0 "
    )
    source = """
module BufferedLane {
  clock clk reset rst
  in rx:rv<u8> out tx:rv<u8>
  STORAGE
  rx.ready=tx.ready tx.payload=rx.payload tx.valid=rx.valid
}
module Top {
  clock clk reset rst
  in rx0:rv<u8> in rx1:rv<u8> out tx0:rv<u8> out tx1:rv<u8>
  inst lane[2]:BufferedLane
  connect rx0 -> lane[0].rx connect lane[0].tx -> tx0
  connect rx1 -> lane[1].rx connect lane[1].tx -> tx1
}
""".replace("STORAGE", declaration)
    with pytest.raises(SemanticError, match="not synchronous memory or initialized ROM"):
        compile_source(source)


def test_canonical_ready_valid_array_rejects_malformed_physical_endpoints() -> None:
    canonical = lower(_module())
    first = canonical.hierarchical_connections[0]

    malformed = (
        (
            replace(first, destination=replace(first.destination, owner="lane[9]")),
            "unknown physical owner 'lane\\[9\\]'",
        ),
        (
            replace(
                first,
                destination=replace(
                    first.destination, direction=PortDirection.OUTPUT,
                ),
            ),
            "direction metadata disagrees",
        ),
        (
            replace(
                first,
                destination=replace(first.destination, payload_type=UIntType(16)),
            ),
            "payload type disagrees",
        ),
        (
            replace(first, destination=replace(first.destination, domain="other")),
            "domain metadata disagrees",
        ),
    )
    for connection, message in malformed:
        with pytest.raises(CanonicalizationError, match=message):
            restore(replace(
                canonical,
                hierarchical_connections=(
                    connection, *canonical.hierarchical_connections[1:],
                ),
            ))


def test_canonical_ready_valid_array_rejects_duplicate_consumers_and_drivers() -> None:
    canonical = lower(_module())
    first, _, third, fourth = canonical.hierarchical_connections
    duplicate_consumer = replace(
        third,
        source=first.source,
    )
    with pytest.raises(CanonicalizationError, match="multiple consumers"):
        restore(replace(
            canonical,
            hierarchical_connections=(
                first,
                canonical.hierarchical_connections[1],
                duplicate_consumer,
                fourth,
            ),
        ))

    duplicate_driver = replace(
        third,
        destination=first.destination,
    )
    with pytest.raises(CanonicalizationError, match="multiple drivers"):
        restore(replace(
            canonical,
            hierarchical_connections=(
                first,
                canonical.hierarchical_connections[1],
                duplicate_driver,
                fourth,
            ),
        ))


HARNESS = r'''\
#include "VStatefulRvArray.h"
#include "verilated.h"
static void tick(VStatefulRvArray& dut) {
  dut.clk = 0; dut.eval(); dut.clk = 1; dut.eval(); dut.clk = 0; dut.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VStatefulRvArray dut;
  dut.input0_payload = 0; dut.input0_valid = 0;
  dut.input1_payload = 0; dut.input1_valid = 0;
  dut.output0_ready = 1; dut.output1_ready = 1;
  dut.rst = 1; tick(dut);
  dut.rst = 0;
  dut.input0_payload = 10; dut.input0_valid = 1;
  dut.input1_payload = 20; dut.input1_valid = 1;
  dut.eval(); if (dut.output0_payload != 10 || dut.output1_payload != 20) return 1;
  tick(dut);
  dut.output0_ready = 0; dut.output1_ready = 1; dut.eval();
  if (dut.input0_ready != 0 || dut.input1_ready != 1) return 2;
  if (dut.output0_payload != 11 || dut.output1_payload != 21) return 3;
  tick(dut); dut.eval();
  if (dut.output0_payload != 11 || dut.output1_payload != 22) return 4;
  dut.output0_ready = 1; tick(dut); dut.eval();
  if (dut.output0_payload != 12 || dut.output1_payload != 23) return 5;
  dut.rst = 1; tick(dut); dut.rst = 0; dut.eval();
  if (dut.output0_payload != 10 || dut.output1_payload != 20) return 6;
  return 0;
}
'''


def _verilate(
    tmp_path: Path, rtl: tuple[Path, ...],
) -> None:
    harness = tmp_path / "test.cpp"
    harness.write_text(HARNESS)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(tmp_path / "obj"),
            "--top-module", "StatefulRvArray",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(tmp_path / "obj" / "VStatefulRvArray"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_ready_valid_array_passes_real_verilator(tmp_path: Path) -> None:
    rtl = tmp_path / "StatefulRvArray.sv"
    rtl.write_text(emit_sv_artifact(_module()).text)
    _verilate(tmp_path, (rtl,))
