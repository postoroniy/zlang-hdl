import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.clash.public_wrapper import ClashPublicTopWrapper
from zlang.backend.manifest import BackendArtifact, MANIFEST_VERSION
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
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
from zlang.ir import (
    BindingMap,
    BindingSide,
    CrossBackendMode,
    CrossBackendProperty,
    CrossBackendRelation,
    CrossBackendStatus,
    EquivalenceBinding,
    EquivalenceMode,
    EquivalenceStatus,
    SignalRole,
)
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


SOURCE = """
module RuntimeSelect {
    in values : vec<4,u8>
    in raw_index : u3
    out y : u8
    index = truncate<2>(raw_index)
    y = values[index]
}
"""


def _testbench() -> str:
    return r"""
module tb;
  logic [7:0] values [0:3];
  logic [2:0] raw_index;
  wire [7:0] y;
  RuntimeSelect dut(.values(values), .raw_index(raw_index), .y(y));
  initial begin
    values = '{8'd10, 8'd20, 8'd30, 8'd40};
    raw_index = 0; #1; if (y !== 8'd10) $fatal(1, "index 0");
    raw_index = 1; #1; if (y !== 8'd20) $fatal(1, "index 1");
    raw_index = 2; #1; if (y !== 8'd30) $fatal(1, "index 2");
    raw_index = 3; #1; if (y !== 8'd40) $fatal(1, "index 3");
    raw_index = 7; #1; if (y !== 8'd40) $fatal(1, "truncated index");
    $finish;
  end
endmodule
"""


def _run_verilator(files: list[Path], tmp_path: Path, tag: str) -> None:
    bench = tmp_path / f"tb_{tag}.sv"
    bench.write_text(_testbench())
    obj = tmp_path / f"obj_{tag}"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            "verilator", "--binary", "--timing", "-Wall", "-Wno-fatal",
            "--top-module", "tb", "--Mdir", str(obj),
            *(str(path) for path in files), str(bench),
        ),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run((str(obj / "Vtb"),), check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_runtime_expression_index_simulates(tmp_path: Path) -> None:
    artifact = emit_sv_artifact(compile_source(SOURCE).ir)
    rtl = tmp_path / "RuntimeSelect.sv"
    rtl.write_text(artifact.text)
    assert artifact.bindings[0].width == 32
    assert "32'(2'(raw_index))" in artifact.text
    _run_verilator([rtl], tmp_path, "sv")


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="Clash or Verilator unavailable",
)
def test_clash_runtime_expression_index_simulates(tmp_path: Path) -> None:
    result = compile_source(SOURCE)
    assert "fromIntegral" not in result.clash
    assert "zlangRuntimeVector" in result.clash
    files = list(generate_verilog(
        result.clash,
        result.ir.name,
        tmp_path / "clash",
        public_wrapper=ClashPublicTopWrapper.build(result.ir),
    ))
    assert files
    lint_with_verilator(tuple(files), result.ir.name)
    _run_verilator(files, tmp_path, "clash")


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="Clash or Verilator unavailable",
)
def test_clash_runtime_index_64_has_exact_physical_selector_width(
    tmp_path: Path,
) -> None:
    source = """
module RuntimeSelect64 {
    in values : vec<64,u8>
    in index : u6
    out y : u8
    y = values[index]
}
"""
    result = compile_source(source)
    files = tuple(
        generate_verilog(
            result.clash,
            result.ir.name,
            tmp_path / "clash64",
            public_wrapper=ClashPublicTopWrapper.build(result.ir),
        )
    )
    assert files
    lint_with_verilator(files, result.ir.name)


def _m36_source(implementation: str):
    module = compile_source(SOURCE).ir
    expression = module.assignments[0].expression
    reference = emit_reference_model(
        "RuntimeSelectReference", "y", expression.type,
        tuple((port.name, port.type) for port in module.inputs), expression,
    )
    property_ = make_equivalence_property(
        expression, expression, candidate_class="m27",
        reference_root="runtime-index-reference",
        implementation_root="explicit-switch",
        inputs=tuple(f"port:{port.name}" for port in module.inputs),
        reference_output="port:y", implementation_output="port:y",
    )
    names = {f"port:{port.name}": port.name for port in module.inputs}
    names["port:y"] = "y"
    selected = "selected:runtime-index"
    ref_bindings = publish_bindings(
        module, side=BindingSide.REFERENCE, selected_ir_identity=selected,
        backend="semantic_reference", artifact_hash_value=artifact_hash(reference),
        rtl_names=names,
    )
    impl_bindings = publish_bindings(
        module, side=BindingSide.IMPLEMENTATION, selected_ir_identity=selected,
        backend="direct_systemverilog", artifact_hash_value=artifact_hash(implementation),
        rtl_names=names,
    )
    miter = emit_miter(
        property_, BindingMap((*ref_bindings, *impl_bindings)),
        reference_module="RuntimeSelectReference",
        implementation_module="RuntimeSelect",
    )
    return property_, reference + "\n" + implementation + "\n" + miter


EXPLICIT_SWITCH = r"""
module RuntimeSelect(
  input logic [31:0] values,
  input logic [2:0] raw_index,
  output logic [7:0] y
);
  always_comb begin
    case (raw_index[1:0])
      2'd0: y = values[31:24];
      2'd1: y = values[23:16];
      2'd2: y = values[15:8];
      default: y = values[7:0];
    endcase
  end
endmodule
"""


@pytest.mark.skipif(
    len(formal_tools_available()) != 3,
    reason="Yosys/SymbiYosys formal tools are unavailable",
)
def test_m36_runtime_select_matches_explicit_switch_with_real_solver() -> None:
    property_, source = _m36_source(EXPLICIT_SWITCH)
    result = run_equivalence_formal(
        property_, source, top="m36_" + property_.id.replace(".", "_"),
        mode=EquivalenceMode.PROVE, depth=4,
    )
    assert result.status is EquivalenceStatus.PROVEN


def _cross_artifact(backend: str, module: str, body: str) -> BackendArtifact:
    digest = hashlib.sha256(body.encode()).hexdigest()
    identity = "selected:runtime-index"
    bindings = (
        EquivalenceBinding(MANIFEST_VERSION, BindingSide.IMPLEMENTATION, "port:values", identity, module, "values", 32, "unsigned", SignalRole.INPUT, None, None, backend, digest),
        EquivalenceBinding(MANIFEST_VERSION, BindingSide.IMPLEMENTATION, "port:raw_index", identity, module, "raw_index", 3, "unsigned", SignalRole.INPUT, None, None, backend, digest),
        EquivalenceBinding(MANIFEST_VERSION, BindingSide.IMPLEMENTATION, "port:y", identity, module, "y", 8, "unsigned", SignalRole.OUTPUT, None, None, backend, digest),
    )
    return BackendArtifact(backend, module, identity, digest, body, bindings)


@pytest.mark.skipif(
    len(formal_tools_available()) != 3,
    reason="Yosys/SymbiYosys formal tools are unavailable",
)
def test_m38_runtime_select_raw_bits_cross_backend_smoke() -> None:
    left_text = EXPLICIT_SWITCH.replace("RuntimeSelect", "ClashRuntimeSelect", 1)
    right_text = EXPLICIT_SWITCH.replace("RuntimeSelect", "SvRuntimeSelect", 1)
    left = _cross_artifact("clash", "ClashRuntimeSelect", left_text)
    right = _cross_artifact("direct_systemverilog", "SvRuntimeSelect", right_text)
    property_ = CrossBackendProperty(
        "m38.runtime_index", CrossBackendRelation.SAME_CYCLE_VALUE,
        "selected:runtime-index", ("port:y",), None, None, 0, 0,
    )
    result = run_cross_backend_formal(
        property_, left, right,
        inputs=("port:values", "port:raw_index"),
        mode=CrossBackendMode.BMC, depth=4,
    )
    assert result.status is CrossBackendStatus.BOUNDED_PASS
