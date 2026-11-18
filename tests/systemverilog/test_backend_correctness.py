"""Bounded correctness regressions for shared SV syntax and typed hierarchy."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from zlang import sim
from zlang.backend.systemverilog import (
    SystemVerilogEmissionError,
    emit_artifact,
    emit_contracts,
    emit_experimental,
)
from zlang.backend.systemverilog.syntax import sized_decimal
from zlang.compiler import compile_source
from zlang.formal import build_recursive_formal_design, run_verilog_formal
from zlang.ir import expressions as expr
from zlang.ir.formal import FormalStatus
from zlang.ir.types import BitType, SIntType
from zlang.ir.verification import Contract, ContractKind
from zlang.simulation_compare import compare_program


SPECIALIZED_HIERARCHY = """
module Child<W=8> {
    in x : uint<W>
    out y : uint<W>
    y = x
}
module SpecializedTop {
    in a : u8
    in b : u16
    out y : u16
    inst c8 : Child<8> { x=a }
    inst c16 : Child<16> { x=b }
    y = extend<16>(c8.y) | c16.y
}
"""


def test_two_global_fifos_preserve_independent_transactions(tmp_path: Path) -> None:
    source = tmp_path / "two_fifos.zhl"
    source.write_text("""
module TwoFifos {
    clock clk reset rst
    in a_data:u8 in a_push:bit in a_pop:bit
    in b_data:u8 in b_push:bit in b_pop:bit in gate_b:bit
    out a_front:u8 out a_valid:bit out b_front:u8 out b_valid:bit
    fifo first:fifo<u8,2>
    fifo second:fifo<u8,3>
    first.data=a_data first.push=a_push first.pop=a_pop
    second.data=b_data second.push=b_push & (~gate_b | first.full) second.pop=b_pop
    a_front=first.front a_valid=first.valid
    b_front=second.front b_valid=second.valid
}
""")
    # Tuples contain offered (data, push, pop) per FIFO. The full flag is
    # only referenced by the other FIFO's control, never by an output.
    cycles = (
        (True, 0, (0xAA, 1, 0), (0xBB, 1, 0)),
        (False, 1, (0x11, 1, 0), (0x91, 1, 0)),  # gate B while A has room
        (False, 0, (0x22, 1, 0), (0x92, 1, 0)),
        (False, 1, (0x33, 0, 0), (0x93, 1, 0)),  # gate passes when A is full
        (False, 0, (0x44, 1, 1), (0x94, 1, 0)),
        (False, 0, (0, 0, 0), (0x95, 1, 1)),
        (False, 0, (0x55, 0, 0), (0x96, 0, 0)),  # hold both queues at capacity
        (False, 0, (0, 0, 1), (0, 0, 1)),
        (False, 0, (0, 0, 1), (0, 0, 1)),
        (True, 0, (0x66, 1, 0), (0x97, 1, 0)),  # discard B's pending beat
        (False, 0, (0x77, 1, 0), (0, 0, 0)),
    )
    queues = (deque(), deque())
    events, expected = [], []
    for reset, gate_b, first, second in cycles:
        inputs = {"gate_b": gate_b}
        allow_b = not gate_b or len(queues[0]) == 2
        for name, queue, depth, controls, allowed in zip(
            ("a", "b"), queues, (2, 3), (first, second), (True, allow_b), strict=True,
        ):
            data, push, pop = controls
            inputs.update({f"{name}_data": data, f"{name}_push": push, f"{name}_pop": pop})
            if reset:
                queue.clear()
                continue
            if pop:
                queue.popleft()
            if push and allowed:
                assert len(queue) < depth
                queue.append(data)
        events.append({"reset": {"rst": reset}, "set": inputs, "edges": ["clk"]})
        expected.append({
            "a_front": queues[0][0] if queues[0] else 0, "a_valid": int(bool(queues[0])),
            "b_front": queues[1][0] if queues[1] else 0, "b_valid": int(bool(queues[1])),
        })
    comparison = compare_program(
        sim.compile(source), events, simulator="iverilog",
        artifact_directory=tmp_path / "comparison", timeout=30, build_timeout=30,
    )
    assert comparison.native_trace == tuple(expected)
    assert comparison.rtl_trace == tuple(expected)


def test_two_global_memories_preserve_independent_reads_and_writes(tmp_path: Path) -> None:
    source = tmp_path / "two_memories.zhl"
    source.write_text("""
module TwoMemories {
    clock clk reset rst
    in a_read:u1 in a_write:u1 in a_enable:bit in a_data:u8
    in b_read:u1 in b_write:u1 in b_enable:bit in b_data:u8
    out a_result:u8 out b_result:u8
    memory first:mem<u8,2> { read_latency 1 collision read_first }
    memory second:mem<u8,2> { read_latency 1 collision read_first }
    first.read_address=a_read first.write_address=a_write
    first.write_enable=a_enable first.write_data=a_data
    second.read_address=b_read second.write_address=b_write
    second.write_enable=b_enable second.write_data=b_data
    a_result=first.read_data b_result=second.read_data
}
""")
    # Inputs are (read address, write address, write enable, data) per RAM.
    # A sample without a clock edge distinguishes latency one from a bypass.
    cycles = (
        (True, True, (0, 0, 1, 0xFF), (1, 1, 1, 0xEE)),
        (False, True, (1, 1, 1, 0x31), (0, 0, 1, 0x92)),
        (False, True, (1, 0, 0, 0xFF), (1, 1, 1, 0xA7)),
        (False, False, (0, 0, 1, 0x55), (0, 1, 0, 0)),
        (False, True, (0, 0, 1, 0x55), (0, 1, 0, 0)),
        (False, True, (0, 1, 0, 0), (1, 0, 0, 0)),
        (False, True, (1, 0, 0, 0), (0, 1, 0, 0)),
        (True, False, (1, 1, 1, 0xDD), (0, 0, 1, 0xCC)),
        (True, True, (1, 1, 1, 0xDD), (0, 0, 1, 0xCC)),
        (False, True, (1, 0, 0, 0), (0, 1, 0, 0)),
    )
    cells = ([0, 0], [0, 0])
    samples = {"a_result": 0, "b_result": 0}
    events, expected = [], []
    for reset, edge, first, second in cycles:
        inputs = {}
        for name, memory, controls in zip(("a", "b"), cells, (first, second), strict=True):
            inputs.update({f"{name}_{field}": value for field, value in zip(
                ("read", "write", "enable", "data"), controls, strict=True,
            )})
            if not edge:
                continue
            read, write, enable, data = controls
            if reset:
                memory[:] = [0, 0]
            samples[f"{name}_result"] = memory[read]
            if enable and not reset:
                memory[write] = data
        events.append({"reset": {"rst": reset}, "set": inputs, "edges": ["clk"] if edge else []})
        expected.append(samples.copy())
    comparison = compare_program(
        sim.compile(source), events, simulator="iverilog",
        artifact_directory=tmp_path / "comparison", timeout=30, build_timeout=30,
    )
    assert comparison.native_trace == tuple(expected)
    assert comparison.rtl_trace == tuple(expected)


def _negative_contract_module():
    module = compile_source(
        "module NegativeContract { clock clk reset rst in x:s8 out y:s8 y=x }",
    ).ir
    signed = SIntType(8)
    bit = BitType()
    value = expr.InputRef("x", signed)
    negative = expr.Binary(
        expr.BinaryOperator.EQUAL,
        value,
        expr.Constant(-1, signed),
        signed,
        bit,
    )
    negative_switch = expr.Switch(
        value,
        (expr.SwitchCase(-1, expr.Constant(1, bit)),),
        expr.Constant(0, bit),
        bit,
    )
    return replace(
        module,
        contracts=(
            Contract(
                ContractKind.GUARANTEE,
                "negative_constant",
                "clk",
                "rst",
                negative,
            ),
            Contract(
                ContractKind.GUARANTEE,
                "negative_switch",
                "clk",
                "rst",
                negative_switch,
            ),
        ),
    )


def test_contract_negative_constants_use_legal_shared_sv_spelling() -> None:
    module = _negative_contract_module()
    contracts = emit_contracts(module)
    assert sized_decimal(8, -1, signed=True) == "-8'sd1"
    assert contracts.count("-8'sd1") == 2
    assert "8'sd-1" not in contracts


def test_contract_sidecar_fails_closed_for_split_public_struct_output() -> None:
    result = compile_source(
        "struct Result { data:u8 ok:bit } "
        "module SplitContract { clock clk reset rst in x:u8 out y:Result "
        "y = Result { data=x ok=1 } "
        "assert output_ok @ clk { y.ok } }",
    )

    assert "non-executable verification contract report" in result.contracts_sva
    assert "no exact packed public-top binding" in result.contracts_sva
    assert "bind SplitContract" not in result.contracts_sva


@pytest.mark.parametrize(
    ("domain", "event", "reset_guard", "support"),
    (
        (
            "clock clk async reset arst_n @clk { polarity active_low }",
            "@(posedge clk)",
            "disable iff (zlang_formal_reset_active)",
            "always @(posedge clk or negedge arst_n)",
        ),
        (
            "clock clk { edge falling } reset rst @clk",
            "@(negedge clk)",
            "disable iff (rst)",
            None,
        ),
    ),
)
def test_contract_sidecar_uses_the_exact_nondefault_clock_reset_contract(
    domain: str,
    event: str,
    reset_guard: str,
    support: str | None,
) -> None:
    result = compile_source(
        f"module NonDefaultContract {{ {domain} in x:bit out y:bit "
        "y=x assert same @clk { y == x } }",
    )

    assert "non-executable verification contract report" not in result.contracts_sva
    assert f"same: assert property ({event} {reset_guard}" in result.contracts_sva
    assert "bind NonDefaultContract" in result.contracts_sva
    if support is None:
        assert "zlang_formal_reset_release" not in result.contracts_sva
    else:
        assert support in result.contracts_sva
        assert '(* ASYNC_REG = "TRUE" *)' in result.contracts_sva


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_negative_constant_and_switch_contracts_lint_with_verilator() -> None:
    module = _negative_contract_module()
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "negative_contract.sv"
        source.write_text(emit_experimental(module) + emit_contracts(module))
        completed = subprocess.run(
            (
                "verilator",
                "--lint-only",
                "--assert",
                "--top-module",
                module.name,
                str(source),
            ),
            capture_output=True,
            text=True,
        )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("yosys", "sby", "z3")),
    reason="Yosys, SymbiYosys, and Z3 unavailable",
)
def test_contract_negative_literal_executes_through_sby_parser() -> None:
    literal = sized_decimal(8, -1, signed=True)
    source = f"""
module NegativeContractLiteral(input clk, input signed [7:0] x);
  always @(posedge clk) assert((x == {literal}) || (x != {literal}));
endmodule
"""
    result = run_verilog_formal(
        source,
        top="NegativeContractLiteral",
        property_id="contract.negative_literal.syntax",
        depth=2,
        systemverilog=True,
    )
    assert result.status is FormalStatus.BOUNDED_PASS, result.reason


def test_composed_hierarchy_rejects_incomplete_or_mismatched_metadata() -> None:
    module = compile_source(
        SPECIALIZED_HIERARCHY,
        top="SpecializedTop",
    ).ir
    incomplete = replace(module, children=module.children[:-1])
    with pytest.raises(SystemVerilogEmissionError, match="typed children"):
        emit_experimental(incomplete)

    wrong_child = replace(module.children[0], name="WrongChild")
    mismatched = replace(module, children=(wrong_child, *module.children[1:]))
    with pytest.raises(SystemVerilogEmissionError, match="not typed child"):
        emit_experimental(mismatched)

    wrong_path = replace(
        module.elaborated_instances[0],
        semantic_path=(module.name, "not-c8"),
    )
    mismatched_path = replace(
        module,
        elaborated_instances=(wrong_path, *module.elaborated_instances[1:]),
    )
    with pytest.raises(SystemVerilogEmissionError, match="semantic path"):
        emit_experimental(mismatched_path)


def test_recursive_artifact_rejects_missing_path_and_specialization_mismatch() -> None:
    module = compile_source(
        SPECIALIZED_HIERARCHY,
        top="SpecializedTop",
    ).ir
    design = build_recursive_formal_design(module)
    child_binding_index = next(
        index
        for index, binding in enumerate(design.bindings)
        if len(binding.physical_instance_path) == 2
    )
    child_binding = design.bindings[child_binding_index]
    bad_bindings = list(design.bindings)
    bad_bindings[child_binding_index] = replace(
        child_binding,
        physical_instance_path=(module.name, "missing"),
    )
    with pytest.raises(SystemVerilogEmissionError, match="physical path"):
        emit_artifact(module, recursive_design=replace(design, bindings=tuple(bad_bindings)))

    child_node_index = next(
        index
        for index, node in enumerate(design.instances)
        if len(node.physical_instance_path) == 2
    )
    nodes = list(design.instances)
    nodes[child_node_index] = replace(
        nodes[child_node_index],
        specialization_identity="wrong-specialization",
    )
    with pytest.raises(SystemVerilogEmissionError, match="specialization"):
        emit_artifact(module, recursive_design=replace(design, instances=tuple(nodes)))


def test_same_prefix_specializations_extend_their_component_names() -> None:
    module = compile_source(
        SPECIALIZED_HIERARCHY,
        top="SpecializedTop",
    ).ir
    identities = (
        "aaaaaaaaaa11111111111111",
        "aaaaaaaaaa22222222222222",
    )
    elaborated = tuple(
        replace(item, specialization_identity=identity)
        for item, identity in zip(module.elaborated_instances, identities, strict=True)
    )
    text = emit_experimental(replace(module, elaborated_instances=elaborated))
    assert "module Child_saaaaaaaaaa11 (" in text
    assert "module Child_saaaaaaaaaa22 (" in text
    assert text.count("module Child_s") == 2


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_same_prefix_specialization_components_lint_with_verilator() -> None:
    module = compile_source(
        SPECIALIZED_HIERARCHY,
        top="SpecializedTop",
    ).ir
    identities = (
        "aaaaaaaaaa11111111111111",
        "aaaaaaaaaa22222222222222",
    )
    elaborated = tuple(
        replace(item, specialization_identity=identity)
        for item, identity in zip(module.elaborated_instances, identities, strict=True)
    )
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "same_prefix.sv"
        source.write_text(
            emit_experimental(replace(module, elaborated_instances=elaborated))
        )
        completed = subprocess.run(
            (
                "verilator",
                "--lint-only",
                "--top-module",
                module.name,
                str(source),
            ),
            capture_output=True,
            text=True,
        )
    assert completed.returncode == 0, completed.stderr
