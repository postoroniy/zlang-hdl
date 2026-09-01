from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.manifest import BackendArtifact, publish_artifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.cross_backend import run_cross_backend_formal
from zlang.equivalence import (
    artifact_hash,
    emit_miter,
    emit_reference_model,
    formal_tools_available,
    make_equivalence_property,
    publish_bindings,
    run_equivalence_formal,
)
from zlang.ir.cross_backend import (
    CrossBackendMode,
    CrossBackendProperty,
    CrossBackendRelation,
    CrossBackendStatus,
)
from zlang.ir.equivalence import (
    BindingMap,
    BindingSide,
    EquivalenceMode,
    EquivalenceStatus,
)
from zlang.toolchain import generate_verilog, lint_with_verilator


SOURCE = """
module ConciseBitsRTL {
    in raw : bits<32>
    out result : bits<33>
    left = bitcast<vec<2,u8>>(raw[31:16])
    right = bitcast<vec<2,u8>>(raw[15:0])
    joined = concat(left, right)
    matrix : vec<2,vec<2,u8>> = reshape(joined)
    flat = reshape<vec<4,u8>>(matrix)
    y_value = bitcast<bits<32>>(flat)
    p_value = parity(raw)
    result = concat(y_value, p_value)
}
"""


RESHAPE_NAME_COLLISION_SOURCE = """
module ReshapeNameCollision {
    in zlangReshapeSource : vec<2,vec<2,u8>>
    out y : vec<4,u8>
    y = reshape(zlangReshapeSource)
}
"""


HARNESS = r'''
#include "VConciseBitsRTL.h"
#include "verilated.h"
#include <cstdint>
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VConciseBitsRTL dut;
  const unsigned values[] = {0u, 1u, 0x12345678u, 0x80000000u, 0xffffffffu};
  for (unsigned value : values) {
    dut.raw = value;
    dut.eval();
    const uint64_t expected = (uint64_t(value) << 1)
                            | (__builtin_popcount(value) & 1);
    if (dut.result != expected) return 1;
  }
  return 0;
}
'''


def _verilate_and_run(tmp_path: Path, rtl: tuple[Path, ...], suffix: str) -> None:
    harness = tmp_path / f"concise_bits_{suffix}.cpp"
    object_dir = tmp_path / f"obj_{suffix}"
    harness.write_text(HARNESS)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator",
            "--cc",
            "--exe",
            "--build",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--Mdir",
            str(object_dir),
            "--top-module",
            "ConciseBitsRTL",
            *(str(path) for path in rtl),
            str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_dir / "VConciseBitsRTL"),), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_direct_sv_concise_bit_collection_artifact_is_deterministic() -> None:
    module = compile_source(SOURCE, include_clash=False).ir
    first = emit_artifact(module)
    second = emit_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    restored = BackendArtifact.from_json(first.to_json())
    assert restored.artifact_hash == first.artifact_hash
    assert restored.bindings == first.bindings
    outputs = {
        binding.semantic_signal_id: (binding.width, binding.canonical_type)
        for binding in restored.bindings
        if binding.semantic_signal_id == "port:result"
    }
    assert outputs == {"port:result": (33, "bits<33>")}


def test_clash_reshape_temporary_cannot_capture_a_same_named_input() -> None:
    compilation = compile_source(RESHAPE_NAME_COLLISION_SOURCE)
    assert "let zlangReshapeSource =" not in compilation.clash
    assert "(\\zlangReshapeSource ->" in compilation.clash


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_real_clash_reshape_same_named_input_is_valid(tmp_path: Path) -> None:
    compilation = compile_source(RESHAPE_NAME_COLLISION_SOURCE)
    rtl = generate_verilog(
        compilation.clash,
        "ReshapeNameCollision",
        tmp_path / "reshape_collision_rtl",
        CLASH_EXECUTABLE,
    )
    lint_with_verilator(rtl, "ReshapeNameCollision")


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_concise_bit_collections_are_bit_exact(tmp_path: Path) -> None:
    artifact = emit_artifact(compile_source(SOURCE, include_clash=False).ir)
    rtl = tmp_path / "ConciseBitsRTL.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), "ConciseBitsRTL")
    _verilate_and_run(tmp_path, (rtl,), "sv")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_clash_concise_bit_collections_are_bit_exact(tmp_path: Path) -> None:
    compilation = compile_source(SOURCE)
    rtl = generate_verilog(
        compilation.clash,
        "ConciseBitsRTL",
        tmp_path / "clash_rtl",
        CLASH_EXECUTABLE,
    )
    lint_with_verilator(rtl, "ConciseBitsRTL")
    _verilate_and_run(tmp_path, tuple(rtl), "clash")


def _m36_sources(module, implementation: str):
    expression = module.assignments[0].expression
    selected_identity = "selected:concise-bit-collections"
    reference = emit_reference_model(
        "ConciseBitsReference",
        "result",
        expression.type,
        (("raw", module.inputs[0].type),),
        expression,
    )
    property_ = make_equivalence_property(
        expression,
        expression,
        candidate_class="value",
        reference_root="concise-bit-collections-reference",
        implementation_root="direct_systemverilog",
        inputs=("port:raw",),
        reference_output="port:result",
        implementation_output="port:result",
    )
    rtl_names = {"port:raw": "raw", "port:result": "result"}
    bindings = BindingMap(
        (
            *publish_bindings(
                module,
                side=BindingSide.REFERENCE,
                selected_ir_identity=selected_identity,
                backend="semantic_reference",
                artifact_hash_value=artifact_hash(reference),
                rtl_names=rtl_names,
            ),
            *publish_bindings(
                module,
                side=BindingSide.IMPLEMENTATION,
                selected_ir_identity=selected_identity,
                backend="direct_systemverilog",
                artifact_hash_value=artifact_hash(implementation),
                rtl_names=rtl_names,
            ),
        )
    )
    miter = emit_miter(
        property_,
        bindings,
        reference_module="ConciseBitsReference",
        implementation_module=module.name,
    )
    return property_, reference + "\n" + implementation + "\n" + miter


@pytest.mark.skipif(
    len(formal_tools_available()) != 3,
    reason="Yosys/SymbiYosys formal tools are unavailable",
)
def test_m36_concise_collections_pass_and_parity_mutation_fails() -> None:
    module = compile_source(SOURCE, include_clash=False).ir
    implementation = emit_artifact(module).text
    property_, source = _m36_sources(module, implementation)
    top = "m36_" + property_.id.replace(".", "_")
    correct = run_equivalence_formal(
        property_, source, top=top, mode=EquivalenceMode.BMC, depth=2
    )
    assert correct.status is EquivalenceStatus.BOUNDED_PASS

    mutated = implementation.replace(" ^ ", " | ", 1)
    assert mutated != implementation
    _, broken_source = _m36_sources(module, mutated)
    failed = run_equivalence_formal(
        property_, broken_source, top=top, mode=EquivalenceMode.BMC, depth=2
    )
    assert failed.status is EquivalenceStatus.FAILED
    assert failed.counterexample is not None


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or len(formal_tools_available()) != 3,
    reason="Clash and formal tools are required",
)
def test_m38_cross_backend_concise_collection_smoke() -> None:
    compilation = compile_source(SOURCE)
    module = compilation.ir
    selected_identity = "selected:concise-bit-collections-cross-backend"
    with tempfile.TemporaryDirectory() as temporary:
        files = generate_verilog(
            compilation.clash,
            module.name,
            Path(temporary) / "clash_rtl",
            CLASH_EXECUTABLE,
        )
        clash_rtl = "\n".join(path.read_text() for path in files)
    clash_artifact = publish_artifact(
        module,
        clash_rtl,
        backend="clash",
        selected_ir_identity=selected_identity,
    )
    direct_module = replace(module, name="ConciseBitsRTLSv")
    direct_artifact = emit_artifact(
        direct_module, selected_ir_identity=selected_identity
    )
    property_ = CrossBackendProperty(
        "m38.concise_bit_collections.real",
        CrossBackendRelation.SAME_CYCLE_VALUE,
        selected_identity,
        ("port:result",),
        None,
        None,
        0,
        0,
    )
    result = run_cross_backend_formal(
        property_,
        clash_artifact,
        direct_artifact,
        inputs=("port:raw",),
        mode=CrossBackendMode.BMC,
        depth=2,
    )
    assert result.status is CrossBackendStatus.BOUNDED_PASS
