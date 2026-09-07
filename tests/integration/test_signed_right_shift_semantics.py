"""Executable regression coverage for typed right-shift signedness.

The semantic IR records the left operand type.  Every executable
SystemVerilog path must therefore select arithmetic ``>>>`` for signed values
and logical ``>>`` for unsigned/raw values without guessing from RTL text.
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
from zlang.simulate import simulate
from zlang.toolchain import generate_verilog, lint_with_verilator


VERILATOR = shutil.which("verilator")


WITNESS_SOURCE = """
module SignedRightShiftWitness {
    in signed_value : s64
    in unsigned_value : u64
    in raw_value : bits<64>
    in amount : u6
    in q16_a : s32
    in q16_b : s32

    out signed_y : s64
    out unsigned_y : u64
    out raw_y : bits<64>
    out q16_y : s64

    signed_y = signed_value >> amount
    unsigned_y = unsigned_value >> amount
    raw_y = raw_value >> amount
    q16_y = (q16_a * q16_b) >> 16
}
"""


M36_SOURCE = """
module SignedShiftM36 {
    in a : s64
    in amount : u6
    out y : s64
    y = a >> amount
}
"""


def _verilator_harness() -> str:
    return r'''
#include "VSignedRightShiftWitness.h"
#include "verilated.h"
#include <cstdint>

static int64_t signed64(uint64_t value) {
  return static_cast<int64_t>(value);
}

int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VSignedRightShiftWitness dut;

  dut.signed_value = static_cast<uint64_t>(INT64_C(-65536));
  dut.unsigned_value = UINT64_C(0x8000000000000000);
  dut.raw_value = UINT64_C(0x8000000000000000);
  dut.amount = 16;
  dut.q16_a = static_cast<uint32_t>(INT32_C(-65536));
  dut.q16_b = static_cast<uint32_t>(INT32_C(131072));
  dut.eval();
  if (signed64(dut.signed_y) != -1) return 1;
  if (dut.unsigned_y != UINT64_C(0x0000800000000000)) return 2;
  if (dut.raw_y != UINT64_C(0x0000800000000000)) return 3;
  if (signed64(dut.q16_y) != -131072) return 4;

  dut.signed_value = UINT64_C(0x8000000000000000);
  dut.unsigned_value = UINT64_C(0xffffffffffffffff);
  dut.raw_value = UINT64_C(0xffffffffffffffff);
  dut.amount = 63;
  dut.eval();
  if (signed64(dut.signed_y) != -1) return 5;
  if (dut.unsigned_y != 1) return 6;
  if (dut.raw_y != 1) return 7;

  dut.signed_value = UINT64_C(0x4000000000000000);
  dut.unsigned_value = UINT64_C(0x4000000000000000);
  dut.raw_value = UINT64_C(0x4000000000000000);
  dut.amount = 62;
  dut.eval();
  if (signed64(dut.signed_y) != 1) return 8;
  if (dut.unsigned_y != 1 || dut.raw_y != 1) return 9;
  return 0;
}
'''


def test_semantic_simulator_uses_typed_right_shift() -> None:
    module = compile_source(WITNESS_SOURCE, include_clash=False).ir

    assert simulate(
        module,
        signed_value=-65536,
        unsigned_value=1 << 63,
        raw_value=1 << 63,
        amount=16,
        q16_a=-65536,
        q16_b=131072,
    ) == {
        "signed_y": -1,
        "unsigned_y": 0x0000_8000_0000_0000,
        "raw_y": 0x0000_8000_0000_0000,
        "q16_y": -131072,
    }


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_direct_systemverilog_uses_typed_shift_and_matches_signed_edges(
    tmp_path: Path,
) -> None:
    module = compile_source(WITNESS_SOURCE, include_clash=False).ir
    text = emit_artifact(module).text

    assert re.search(r"assign signed_y = .*>>>.*;", text)
    assert re.search(r"assign q16_y = .*>>>.*;", text)
    assert re.search(r"assign unsigned_y = .* >> .*;", text)
    assert re.search(r"assign raw_y = .* >> .*;", text)

    rtl = tmp_path / "SignedRightShiftWitness.sv"
    harness = tmp_path / "signed_right_shift.cpp"
    obj = tmp_path / "obj"
    rtl.write_text(text)
    harness.write_text(_verilator_harness())
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            VERILATOR,
            "--cc",
            "--exe",
            "--build",
            "-Wall",
            "--top-module",
            "SignedRightShiftWitness",
            "--Mdir",
            str(obj),
            str(rtl),
            str(harness),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    executed = subprocess.run(
        (str(obj / "VSignedRightShiftWitness"),),
        check=False,
        capture_output=True,
        text=True,
    )
    assert executed.returncode == 0, executed.stderr or executed.stdout


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or VERILATOR is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_clash_uses_typed_shift_and_matches_signed_edges(tmp_path: Path) -> None:
    compilation = compile_source(WITNESS_SOURCE)
    assert "shiftR" in compilation.clash

    rtl = tuple(
        generate_verilog(
            compilation.clash,
            compilation.ir.name,
            tmp_path / "clash",
            CLASH_EXECUTABLE,
        )
    )
    lint_with_verilator(rtl, compilation.ir.name)

    harness = tmp_path / "signed_right_shift_clash.cpp"
    obj = tmp_path / "obj_clash"
    harness.write_text(_verilator_harness())
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            VERILATOR,
            "--cc",
            "--exe",
            "--build",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--top-module",
            compilation.ir.name,
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
        (str(obj / "VSignedRightShiftWitness"),),
        check=False,
        capture_output=True,
        text=True,
    )
    assert executed.returncode == 0, executed.stderr or executed.stdout


def _m36_source(implementation: str) -> tuple[object, str, str]:
    module = compile_source(M36_SOURCE, include_clash=False).ir
    expression = module.assignments[0].expression
    selected = "selected:signed-right-shift"
    reference = emit_reference_model(
        "SignedShiftReference",
        "y",
        expression.type,
        tuple((port.name, port.type) for port in module.inputs),
        expression,
    )
    property_ = make_equivalence_property(
        expression,
        expression,
        candidate_class="value",
        reference_root="signed-shift-reference",
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
        selected_ir_identity=selected,
        backend="semantic_reference",
        artifact_hash_value=artifact_hash(reference),
        rtl_names=names,
    )
    implementation_bindings = publish_bindings(
        module,
        side=BindingSide.IMPLEMENTATION,
        selected_ir_identity=selected,
        backend="direct_systemverilog",
        artifact_hash_value=artifact_hash(implementation),
        rtl_names=names,
    )
    miter = emit_miter(
        property_,
        BindingMap((*reference_bindings, *implementation_bindings)),
        reference_module="SignedShiftReference",
        implementation_module=module.name,
    )
    top = "m36_" + property_.id.replace(".", "_")
    return property_, reference + "\n" + implementation + "\n" + miter, top


@pytest.mark.skipif(
    len(formal_tools_available()) != 3,
    reason="Yosys/SymbiYosys formal tools are unavailable",
)
def test_m36_signed_shift_reference_passes_and_logical_mutation_fails() -> None:
    module = compile_source(M36_SOURCE, include_clash=False).ir
    implementation = emit_artifact(
        module, selected_ir_identity="selected:signed-right-shift"
    ).text
    assert ">>>" in implementation

    property_, correct_source, top = _m36_source(implementation)
    assert "assign y" in correct_source and ">>>" in correct_source
    correct = run_equivalence_formal(
        property_, correct_source, top=top, mode=EquivalenceMode.BMC, depth=2
    )
    assert correct.status is EquivalenceStatus.BOUNDED_PASS

    mutated_implementation = implementation.replace(">>>", ">>", 1)
    assert mutated_implementation != implementation
    property_, mutated_source, top = _m36_source(mutated_implementation)
    mutated = run_equivalence_formal(
        property_, mutated_source, top=top, mode=EquivalenceMode.BMC, depth=2
    )
    assert mutated.status is EquivalenceStatus.FAILED
    assert mutated.counterexample is not None


def test_structured_formal_predicate_renders_signed_shift_arithmetically() -> None:
    signed = FormalSignedness.SIGNED
    unsigned = FormalSignedness.UNSIGNED
    bit = FormalSignedness.BIT
    shifted = Binary(
        FormalBinaryOperator.SHIFT_RIGHT,
        ObservationRef("port:a", 64, signed),
        ObservationRef("port:amount", 6, unsigned),
        64,
        signed,
    )
    predicate = Binary(
        FormalBinaryOperator.EQUAL,
        shifted,
        ObservationRef("port:y", 64, signed),
        1,
        bit,
    )
    bindings = {
        semantic_id: SignalBinding(
            semantic_id,
            "SignedShiftFormal",
            rtl_name,
            width,
            direction,
        )
        for semantic_id, rtl_name, width, direction in (
            ("port:a", "a", 64, "input"),
            ("port:amount", "amount", 6, "input"),
            ("port:y", "y", 64, "output"),
        )
    }

    rendered = render_bound_predicate(predicate, bindings)
    assert ">>>" in rendered
    assert "$signed" in rendered
