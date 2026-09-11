from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from zlang.backend.manifest import BackendArtifact, publish_artifact
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
from zlang.toolchain import lint_with_verilator


SOURCE = """
struct Pair { hi:u4 lo:u4 }
module PackingRTL {
    in raw:bits<8>
    in signed_value:s8
    out y:bits<20>
    out lsb:bit
    out msb:bit
    out reversed:bits<8>
    pair:Pair = unpack<Pair>(raw)
    roundtrip:bits<8> = pack(pair)
    y = concat(
        roundtrip,
        signed_value[7:4],
        pack(unpack<s8>(pack(signed_value)))
    )
    lsb = raw[0]
    msb = raw[7]
    reversed = bitcast<bits<8>>(generate(i in 0..8) raw[i])
}
"""

SIGNED_LITERAL_SOURCE = """
module SignedLiteralRTL {
    out y : s8
    y = -1
}
"""


HARNESS = r'''
#include "VPackingRTL.h"
#include "verilated.h"
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VPackingRTL dut;
  struct Case {
    unsigned raw;
    unsigned signed_value;
    unsigned expected;
    unsigned reversed;
  };
  const Case cases[] = {
    {0xa5u, 0xfeu, 0xa5ffeu, 0xa5u},
    {0x12u, 0x7fu, 0x1277fu, 0x48u},
    {0x00u, 0x80u, 0x00880u, 0x00u},
    {0xffu, 0x00u, 0xff000u, 0xffu},
  };
  for (const auto &item : cases) {
    dut.raw = item.raw;
    dut.signed_value = item.signed_value;
    dut.eval();
    if (dut.y != item.expected) return 1;
    if (dut.lsb != (item.raw & 1u)) return 2;
    if (dut.msb != ((item.raw >> 7) & 1u)) return 3;
    if (dut.reversed != item.reversed) return 4;
  }
  return 0;
}
'''


def _verilate_and_run(tmp_path: Path, rtl: tuple[Path, ...], suffix: str) -> None:
    harness = tmp_path / f"packing_{suffix}.cpp"
    obj = tmp_path / f"obj_{suffix}"
    harness.write_text(HARNESS)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(obj), "--top-module", "PackingRTL",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run((str(obj / "VPackingRTL"),), capture_output=True, text=True)
    assert run.returncode == 0, run.stderr or run.stdout


def test_direct_sv_packing_is_deterministic_and_manifest_round_trips() -> None:
    module = compile_source(SOURCE).ir
    first = emit_artifact(module)
    second = emit_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert "{8'(" in first.text
    restored = BackendArtifact.from_json(first.to_json())
    assert restored.artifact_hash == first.artifact_hash
    assert restored.bindings == first.bindings
    output = next(
        binding for binding in restored.bindings
        if binding.semantic_signal_id == "port:y"
    )
    assert (output.width, output.canonical_type) == (20, "bits<20>")


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_packing_lints_and_is_bit_exact(tmp_path: Path) -> None:
    artifact = emit_artifact(compile_source(SOURCE).ir)
    rtl = tmp_path / "PackingRTL.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), "PackingRTL")
    _verilate_and_run(tmp_path, (rtl,), "sv")




def _m36(module, implementation: str, implementation_module: str, backend: str):
    expression = module.assignments[0].expression
    identity = "selected:packing-value"
    input_ids = tuple(f"port:{port.name}" for port in module.inputs)
    rtl_names = {
        f"port:{port.name}": port.name
        for port in module.ports
    }
    reference = emit_reference_model(
        "PackingReference",
        "y",
        expression.type,
        tuple((port.name, port.type) for port in module.inputs),
        expression,
    )
    property_ = make_equivalence_property(
        expression,
        expression,
        candidate_class="value",
        reference_root="packing-reference",
        implementation_root=backend,
        inputs=input_ids,
        reference_output="port:y",
        implementation_output="port:y",
    )
    bindings = BindingMap(
        (*publish_bindings(
            module,
            side=BindingSide.REFERENCE,
            selected_ir_identity=identity,
            backend="semantic_reference",
            artifact_hash_value=artifact_hash(reference),
            rtl_names=rtl_names,
        ), *publish_bindings(
            module,
            side=BindingSide.IMPLEMENTATION,
            selected_ir_identity=identity,
            backend=backend,
            artifact_hash_value=artifact_hash(implementation),
            rtl_names=rtl_names,
        ))
    )
    miter = emit_miter(
        property_,
        bindings,
        reference_module="PackingReference",
        implementation_module=implementation_module,
    )
    return property_, reference + "\n" + implementation + "\n" + miter


@pytest.mark.skipif(
    len(formal_tools_available()) != 3,
    reason="Yosys/SymbiYosys formal tools are unavailable",
)
def test_m36_packing_reference_is_visible_and_mutation_fails() -> None:
    module = compile_source(SOURCE).ir
    implementation = emit_artifact(module).text
    property_, source = _m36(module, implementation, module.name, "direct_systemverilog")
    top = "m36_" + property_.id.replace(".", "_")
    correct = run_equivalence_formal(
        property_, source, top=top, mode=EquivalenceMode.BMC, depth=2
    )
    assert correct.status is EquivalenceStatus.BOUNDED_PASS

    mutated = implementation.replace(">> 4", ">> 3", 1)
    assert mutated != implementation
    _, bad_source = _m36(module, mutated, module.name, "direct_systemverilog")
    failed = run_equivalence_formal(
        property_, bad_source, top=top, mode=EquivalenceMode.BMC, depth=2
    )
    assert failed.status is EquivalenceStatus.FAILED
    assert failed.counterexample is not None


@pytest.mark.skipif(
    len(formal_tools_available()) != 3,
    reason="Yosys/SymbiYosys formal tools are unavailable",
)
def test_m36_negative_signed_literal_passes_and_mutation_fails() -> None:
    module = compile_source(SIGNED_LITERAL_SOURCE).ir
    implementation = emit_artifact(module).text
    assert "-8'sd1" in implementation
    property_, source = _m36(
        module,
        implementation,
        module.name,
        "direct_systemverilog",
    )
    top = "m36_" + property_.id.replace(".", "_")
    correct = run_equivalence_formal(
        property_, source, top=top, mode=EquivalenceMode.BMC, depth=2
    )
    assert correct.status is EquivalenceStatus.BOUNDED_PASS

    mutated = implementation.replace("-8'sd1", "8'sd0", 1)
    assert mutated != implementation
    _, bad_source = _m36(
        module,
        mutated,
        module.name,
        "direct_systemverilog",
    )
    failed = run_equivalence_formal(
        property_, bad_source, top=top, mode=EquivalenceMode.BMC, depth=2
    )
    assert failed.status is EquivalenceStatus.FAILED
    assert failed.counterexample is not None
