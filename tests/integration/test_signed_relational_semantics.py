"""ZL-014 regression coverage for typed relational SystemVerilog emission.

SystemVerilog packed selects and interconnects are unsigned even when the ZLang
value they carry is signed.  These tests deliberately compare inputs, locals,
registers, and a stateful child output so executable backends must recover the
typed SInt/Fixed interpretation at each ordered-comparison boundary.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.equivalence import (
    artifact_hash,
    emit_miter,
    emit_reference_model,
    formal_tools_available,
    make_equivalence_property,
    publish_bindings,
    run_equivalence_formal,
)
from zlang.ir.equivalence import (
    BindingMap,
    BindingSide,
    EquivalenceMode,
    EquivalenceStatus,
)
from zlang.ir.formal import SignalBinding, render_bound_predicate
from zlang.ir.formal_predicates import (
    Binary,
    FormalBinaryOperator,
    FormalSignedness,
    ObservationRef,
)
from zlang.simulate import simulate_cycles
from zlang.toolchain import generate_verilog, lint_with_verilator


VERILATOR = shutil.which("verilator")


WITNESS_SOURCE = """
module HeldS32 {
    clock clk reset rst
    in load : bit
    in value : s32
    out held : s32

    reg state : s32 = 0
    capture: when load { state <- value }
    held = state
}

module SignedRelationalWitness {
    clock clk reset rst
    in load : bit
    in signed_input : s32
    in peer_input : s32
    in fixed_input : fixed<32,16>
    in unsigned_input : u32
    out flags : bits<16>

    peer : HeldS32 {
        load = load
        value = peer_input
    }

    reg signed_state : s32 = 0
    reg fixed_state : fixed<32,16> = 0
    reg unsigned_state : u32 = 0
    local_value = signed_input

    capture: when load {
        signed_state <- signed_input
        fixed_state <- fixed_input
        unsigned_state <- unsigned_input
    }

    flags = concat(
        signed_input < signed_state,
        signed_input <= signed_state,
        signed_input > signed_state,
        signed_input >= signed_state,
        signed_state < peer.held,
        signed_state <= peer.held,
        signed_state > peer.held,
        signed_state >= peer.held,
        local_value < bitcast<s32>(signed_state),
        local_value <= bitcast<s32>(signed_state),
        local_value > bitcast<s32>(signed_state),
        local_value >= bitcast<s32>(signed_state),
        fixed_input > fixed_state,
        unsigned_input > unsigned_state,
        signed_input == signed_state,
        signed_input != signed_state
    )
}
"""


M36_SOURCE = """
struct SignedBox { value : s8 }

module SignedProjectionM36 {
    in raw : bits<8>
    in threshold : s16
    out y : bit

    y = extend<16>(unpack<SignedBox>(raw).value) < threshold
}
"""


def _append_flag(value: int, predicate: bool) -> int:
    return (value << 1) | int(predicate)


def _expected_flags(
    signed_input: int,
    *,
    signed_state: int = 0,
    peer_state: int = 98_304,
    fixed_input: int,
    fixed_state: int = 0,
    unsigned_input: int = 0x8000_0000,
    unsigned_state: int = 0x7FFF_FFFF,
) -> int:
    predicates = (
        signed_input < signed_state,
        signed_input <= signed_state,
        signed_input > signed_state,
        signed_input >= signed_state,
        signed_state < peer_state,
        signed_state <= peer_state,
        signed_state > peer_state,
        signed_state >= peer_state,
        signed_input < signed_state,
        signed_input <= signed_state,
        signed_input > signed_state,
        signed_input >= signed_state,
        fixed_input > fixed_state,
        unsigned_input > unsigned_state,
        signed_input == signed_state,
        signed_input != signed_state,
    )
    result = 0
    for predicate in predicates:
        result = _append_flag(result, predicate)
    return result


def _cycle_inputs(signed_input: int, fixed_input: int) -> dict[str, int]:
    return {
        "load": 0,
        "signed_input": signed_input,
        "peer_input": 98_304,
        "fixed_input": fixed_input,
        "unsigned_input": 0x8000_0000,
    }


def _verilator_harness() -> str:
    return r'''
#include "VSignedRelationalWitness.h"
#include "verilated.h"
#include <cstdint>

static void tick(VSignedRelationalWitness &dut) {
  dut.clk = 0;
  dut.eval();
  dut.clk = 1;
  dut.eval();
  dut.clk = 0;
  dut.eval();
}

static uint16_t append_flag(uint16_t value, bool predicate) {
  return static_cast<uint16_t>((value << 1) | (predicate ? 1U : 0U));
}

static uint16_t expected_flags(int32_t input, int32_t fixed_input) {
  constexpr int32_t state = 0;
  constexpr int32_t peer = 98304;
  constexpr int32_t fixed_state = 0;
  constexpr uint32_t unsigned_input = UINT32_C(0x80000000);
  constexpr uint32_t unsigned_state = UINT32_C(0x7fffffff);
  uint16_t result = 0;
  result = append_flag(result, input < state);
  result = append_flag(result, input <= state);
  result = append_flag(result, input > state);
  result = append_flag(result, input >= state);
  result = append_flag(result, state < peer);
  result = append_flag(result, state <= peer);
  result = append_flag(result, state > peer);
  result = append_flag(result, state >= peer);
  result = append_flag(result, input < state);
  result = append_flag(result, input <= state);
  result = append_flag(result, input > state);
  result = append_flag(result, input >= state);
  result = append_flag(result, fixed_input > fixed_state);
  result = append_flag(result, unsigned_input > unsigned_state);
  result = append_flag(result, input == state);
  result = append_flag(result, input != state);
  return result;
}

static int check(VSignedRelationalWitness &dut, int32_t value) {
  dut.load = 0;
  dut.signed_input = static_cast<uint32_t>(value);
  dut.fixed_input = static_cast<uint32_t>(value);
  dut.unsigned_input = UINT32_C(0x80000000);
  dut.eval();
  return dut.flags == expected_flags(value, value) ? 0 : 1;
}

int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VSignedRelationalWitness dut;
  dut.rst = 1;
  dut.load = 0;
  dut.signed_input = 0;
  dut.peer_input = 0;
  dut.fixed_input = 0;
  dut.unsigned_input = 0;
  tick(dut);

  dut.rst = 0;
  dut.load = 1;
  dut.signed_input = 0;
  dut.peer_input = 98304;
  dut.fixed_input = 0;
  dut.unsigned_input = UINT32_C(0x7fffffff);
  tick(dut);

  if (check(dut, -32768)) return 1;
  if (check(dut, 0)) return 2;
  if (check(dut, 98304)) return 3;
  return 0;
}
'''


def _build_and_run(
    rtl: tuple[Path, ...],
    harness: Path,
    obj: Path,
    *,
    clash_generated: bool = False,
) -> None:
    assert VERILATOR is not None
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            VERILATOR,
            "--cc",
            "--exe",
            "--build",
            "-Wall",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            *(("-Wno-PROCASSINIT",) if clash_generated else ()),
            "--top-module",
            "SignedRelationalWitness",
            "--Mdir",
            str(obj),
            *(str(path) for path in rtl),
            str(harness),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    executed = subprocess.run(
        (str(obj / "VSignedRelationalWitness"),),
        check=False,
        capture_output=True,
        text=True,
    )
    assert executed.returncode == 0, executed.stderr or executed.stdout


def test_simulator_preserves_signed_relational_semantics_across_hierarchy() -> None:
    module = compile_source(
        WITNESS_SOURCE, top="SignedRelationalWitness", include_clash=False
    ).ir
    captured = {
        "load": 1,
        "signed_input": 0,
        "peer_input": 98_304,
        "fixed_input": 0,
        "unsigned_input": 0x7FFF_FFFF,
    }
    outputs = simulate_cycles(
        module,
        [
            _cycle_inputs(0, 0),
            captured,
            _cycle_inputs(-32_768, -32_768),
            _cycle_inputs(0, 0),
            _cycle_inputs(98_304, 98_304),
        ],
        reset=[True, False, False, False, False],
    )

    assert [item["flags"] for item in outputs[2:]] == [
        _expected_flags(-32_768, fixed_input=-32_768),
        _expected_flags(0, fixed_input=0),
        _expected_flags(98_304, fixed_input=98_304),
    ]


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_direct_sv_casts_typed_ordered_operands_and_matches_edges(
    tmp_path: Path,
) -> None:
    module = compile_source(
        WITNESS_SOURCE, top="SignedRelationalWitness", include_clash=False
    ).ir
    first = emit_artifact(module)
    second = emit_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash

    text = first.text
    assert "logic signed [31:0] signed_state;" in text
    assert re.search(r"\$signed\([^;]+\) < \$signed\(", text)
    assert re.search(r"\$signed\([^;]+\) <= \$signed\(", text)
    assert re.search(r"\$signed\([^;]+\) > \$signed\(", text)
    assert re.search(r"\$signed\([^;]+\) >= \$signed\(", text)
    assert re.search(r"\$unsigned\([^;]+\) > \$unsigned\(", text)

    rtl = tmp_path / "SignedRelationalWitness.sv"
    harness = tmp_path / "signed_relational.cpp"
    rtl.write_text(text)
    harness.write_text(_verilator_harness())
    _build_and_run((rtl,), harness, tmp_path / "obj_direct")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or VERILATOR is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_clash_matches_signed_relational_edges(tmp_path: Path) -> None:
    compilation = compile_source(WITNESS_SOURCE, top="SignedRelationalWitness")
    rtl = tuple(
        generate_verilog(
            compilation.clash,
            compilation.ir.name,
            tmp_path / "clash",
            CLASH_EXECUTABLE,
        )
    )
    lint_with_verilator(rtl, compilation.ir.name)
    harness = tmp_path / "signed_relational_clash.cpp"
    harness.write_text(_verilator_harness())
    _build_and_run(
        rtl,
        harness,
        tmp_path / "obj_clash",
        clash_generated=True,
    )


def _m36_source(implementation: str) -> tuple[object, str, str]:
    module = compile_source(M36_SOURCE, include_clash=False).ir
    expression = module.assignments[0].expression
    identity = "selected:signed-projection-relational"
    reference = emit_reference_model(
        "SignedProjectionReference",
        "y",
        expression.type,
        tuple((port.name, port.type) for port in module.inputs),
        expression,
    )
    property_ = make_equivalence_property(
        expression,
        expression,
        candidate_class="value",
        reference_root="signed-projection-reference",
        implementation_root="direct-systemverilog",
        inputs=tuple(f"port:{port.name}" for port in module.inputs),
        reference_output="port:y",
        implementation_output="port:y",
    )
    names = {f"port:{port.name}": port.name for port in module.inputs}
    names["port:y"] = "y"
    reference_bindings = publish_bindings(
        module,
        side=BindingSide.REFERENCE,
        selected_ir_identity=identity,
        backend="semantic_reference",
        artifact_hash_value=artifact_hash(reference),
        rtl_names=names,
    )
    implementation_bindings = publish_bindings(
        module,
        side=BindingSide.IMPLEMENTATION,
        selected_ir_identity=identity,
        backend="direct_systemverilog",
        artifact_hash_value=artifact_hash(implementation),
        rtl_names=names,
    )
    miter = emit_miter(
        property_,
        BindingMap((*reference_bindings, *implementation_bindings)),
        reference_module="SignedProjectionReference",
        implementation_module=module.name,
    )
    top = "m36_" + property_.id.replace(".", "_")
    return property_, reference + "\n" + implementation + "\n" + miter, top


@pytest.mark.skipif(
    len(formal_tools_available()) != 3,
    reason="Yosys/SymbiYosys formal tools are unavailable",
)
def test_m36_signed_projection_extend_passes_and_unsigned_mutation_fails() -> None:
    module = compile_source(M36_SOURCE, include_clash=False).ir
    implementation = emit_artifact(
        module, selected_ir_identity="selected:signed-projection-relational"
    ).text
    expected = "16'($signed(zlang_expr_0[7:0]))"
    assert expected in implementation

    property_, source, top = _m36_source(implementation)
    correct = run_equivalence_formal(
        property_, source, top=top, mode=EquivalenceMode.BMC, depth=2
    )
    assert correct.status is EquivalenceStatus.BOUNDED_PASS

    mutated = implementation.replace(
        expected,
        "16'($unsigned(zlang_expr_0[7:0]))",
        1,
    )
    assert mutated != implementation
    _, bad_source, bad_top = _m36_source(mutated)
    failed = run_equivalence_formal(
        property_, bad_source, top=bad_top, mode=EquivalenceMode.BMC, depth=2
    )
    assert failed.status is EquivalenceStatus.FAILED
    assert failed.counterexample is not None


def test_structured_formal_ordered_comparisons_cast_both_operands() -> None:
    signed = FormalSignedness.SIGNED
    unsigned = FormalSignedness.UNSIGNED
    bit = FormalSignedness.BIT
    bindings = {
        semantic_id: SignalBinding(
            semantic_id,
            "SignedRelationalFormal",
            rtl_name,
            32,
            "input",
        )
        for semantic_id, rtl_name in (
            ("signed:left", "signed_left"),
            ("signed:right", "signed_right"),
            ("unsigned:left", "unsigned_left"),
            ("unsigned:right", "unsigned_right"),
        )
    }
    signed_predicate = Binary(
        FormalBinaryOperator.GREATER,
        ObservationRef("signed:left", 32, signed),
        ObservationRef("signed:right", 32, signed),
        1,
        bit,
    )
    unsigned_predicate = Binary(
        FormalBinaryOperator.LESS_EQUAL,
        ObservationRef("unsigned:left", 32, unsigned),
        ObservationRef("unsigned:right", 32, unsigned),
        1,
        bit,
    )

    rendered_signed = render_bound_predicate(signed_predicate, bindings)
    rendered_unsigned = render_bound_predicate(unsigned_predicate, bindings)
    assert rendered_signed.count("$signed") >= 2
    assert " > " in rendered_signed
    assert rendered_unsigned.count("$unsigned") >= 2
    assert " <= " in rendered_unsigned
