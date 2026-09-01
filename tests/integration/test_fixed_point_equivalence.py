from dataclasses import replace
from pathlib import Path
import tempfile

import pytest

from zlang.backend.manifest import publish_artifact
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
from zlang.toolchain import find_clash_executable, generate_verilog


SOURCE = (
    "module FixedCross { in a:fixed<8,4> out y:fixed<6,2> "
    "y=fixed_round_even_saturate(a) }"
)


def _m36(
    module,
    implementation: str,
    implementation_module: str,
    backend: str,
    identity: str = "selected:fixed-review",
):
    expression = module.assignments[0].expression
    input_ids = tuple(f"port:{port.name}" for port in module.inputs)
    rtl_names = {**{item: item.removeprefix("port:") for item in input_ids}, "port:y": "y"}
    reference = emit_reference_model(
        "FixedReference",
        "y",
        expression.type,
        tuple((port.name, port.type) for port in module.inputs),
        expression,
    )
    property_ = make_equivalence_property(
        expression,
        expression,
        candidate_class="value",
        reference_root="fixed-reference",
        implementation_root=backend,
        inputs=input_ids,
        reference_output="port:y",
        implementation_output="port:y",
    )
    reference_bindings = publish_bindings(
        module,
        side=BindingSide.REFERENCE,
        selected_ir_identity=identity,
        backend="semantic_reference",
        artifact_hash_value=artifact_hash(reference),
        rtl_names=rtl_names,
    )
    implementation_bindings = publish_bindings(
        module,
        side=BindingSide.IMPLEMENTATION,
        selected_ir_identity=identity,
        backend=backend,
        artifact_hash_value=artifact_hash(implementation),
        rtl_names=rtl_names,
    )
    miter = emit_miter(
        property_,
        BindingMap((*reference_bindings, *implementation_bindings)),
        reference_module="FixedReference",
        implementation_module=implementation_module,
    )
    source = reference + "\n" + implementation + "\n" + miter
    top = "m36_" + property_.id.replace(".", "_")
    return property_, source, top


@pytest.mark.skipif(
    len(formal_tools_available()) != 3,
    reason="Yosys/SymbiYosys formal tools are unavailable",
)
def test_real_m36_fixed_reference_passes_and_rounding_mutation_fails() -> None:
    module = compile_source(SOURCE).ir
    implementation = emit_artifact(
        module, selected_ir_identity="selected:fixed-review"
    ).text
    property_, source, top = _m36(
        module, implementation, module.name, "direct_systemverilog"
    )
    correct = run_equivalence_formal(
        property_, source, top=top, mode=EquivalenceMode.PROVE, depth=4
    )
    assert correct.status is EquivalenceStatus.PROVEN

    mutated = implementation.replace(" + 9'd1 + ", " + 9'd0 + ")
    assert mutated != implementation
    _, bad_source, bad_top = _m36(
        module, mutated, module.name, "direct_systemverilog"
    )
    failed = run_equivalence_formal(
        property_, bad_source, top=bad_top, mode=EquivalenceMode.BMC, depth=4
    )
    assert failed.status is EquivalenceStatus.FAILED
    assert failed.counterexample is not None


@pytest.mark.skipif(
    len(formal_tools_available()) != 3,
    reason="Yosys/SymbiYosys formal tools are unavailable",
)
@pytest.mark.parametrize(
    ("source", "bad_rtl"),
    (
        (
            "module FixedMutation { in a:fixed<12,4> out y:fixed_sat<6,2> "
            "y=fixed_truncate_saturate(a) }",
            """module FixedMutation(input logic signed [11:0] a, output logic signed [5:0] y);
assign y = $signed(a) >>> 2; endmodule""",
        ),
        (
            "module FixedMutation { in a:fixed<12,4> out y:fixed_sat<6,2> "
            "y=fixed_truncate_saturate(a) }",
            """module FixedMutation(input logic signed [11:0] a, output logic signed [5:0] y);
always_comb begin
  if (($signed(a) >>> 2) > 31) y = 30;
  else if (($signed(a) >>> 2) < -32) y = -32;
  else y = $signed(a) >>> 2;
end endmodule""",
        ),
        (
            "module FixedMutation { in a:ufixed<5,2> in b:ufixed<4,2> "
            "out y:ufixed<5,2> y=a-b }",
            """module FixedMutation(input logic [4:0] a, input logic [3:0] b,
output logic [4:0] y); assign y = {1'b0, (a[3:0] - b)}; endmodule""",
        ),
        (
            "module FixedMutation { in a:fixed<4,2> in b:fixed<4,2> "
            "in c:fixed<4,2> in d:fixed<4,2> out y:fixed<9,4> "
            "y=(a*b)+(c*d) }",
            """module FixedMutation(input logic signed [3:0] a,b,c,d,
output logic signed [8:0] y); logic signed [7:0] p0,p1,narrow;
assign p0=a*b; assign p1=c*d; assign narrow=p0+p1;
assign y={{1{narrow[7]}},narrow}; endmodule""",
        ),
        (
            "module FixedMutation { in a:fixed<4,2> in b:fixed<4,2> "
            "in c:fixed<4,2> in d:fixed<4,2> out y:fixed<9,2> "
            "y=quantize<fixed<9,2>>((a*b)+(c*d)){round floor overflow wrap} }",
            """module FixedMutation(input logic signed [3:0] a,b,c,d,
output logic signed [8:0] y); logic signed [7:0] p0,p1;
assign p0=a*b; assign p1=c*d;
assign y=($signed(p0) >>> 2)+($signed(p1) >>> 2); endmodule""",
        ),
    ),
    ids=("wrap-for-saturate", "saturation-off-by-one", "unsigned-sub-width", "missing-guard", "premature-quantize"),
)
def test_real_m36_fixed_mutation_matrix_fails(source: str, bad_rtl: str) -> None:
    module = compile_source(source).ir
    property_, formal_source, top = _m36(
        module, bad_rtl, module.name, "direct_systemverilog"
    )
    result = run_equivalence_formal(
        property_, formal_source, top=top, mode=EquivalenceMode.BMC, depth=4
    )
    assert result.status is EquivalenceStatus.FAILED
    assert result.counterexample is not None


@pytest.mark.skipif(
    not find_clash_executable() or len(formal_tools_available()) != 3,
    reason="Clash or formal tools are unavailable",
)
def test_real_fixed_m36_triangle_and_m38_cross_backend_pass() -> None:
    result = compile_source(SOURCE)
    module = result.ir
    identity = "selected:fixed-cross-backend"
    with tempfile.TemporaryDirectory() as temporary:
        files = generate_verilog(
            result.clash, module.name, Path(temporary) / "clash-rtl"
        )
        clash_rtl = "\n".join(path.read_text() for path in files)

    sv_module = replace(module, name="FixedCrossSv")
    sv_artifact = emit_artifact(sv_module, selected_ir_identity=identity)
    clash_artifact = publish_artifact(
        module,
        clash_rtl,
        backend="clash",
        selected_ir_identity=identity,
    )

    for typed_module, rtl, top, backend in (
        (module, clash_rtl, module.name, "clash"),
        (sv_module, sv_artifact.text, sv_module.name, "direct_systemverilog"),
    ):
        property_, source, miter_top = _m36(
            typed_module, rtl, top, backend, identity
        )
        triangle = run_equivalence_formal(
            property_, source, top=miter_top, mode=EquivalenceMode.PROVE, depth=4
        )
        assert triangle.status is EquivalenceStatus.PROVEN

    property_ = CrossBackendProperty(
        "m38.fixed.real",
        CrossBackendRelation.SAME_CYCLE_VALUE,
        identity,
        ("port:y",),
        None,
        None,
        0,
        0,
    )
    cross = run_cross_backend_formal(
        property_,
        clash_artifact,
        sv_artifact,
        inputs=("port:a",),
        mode=CrossBackendMode.BMC,
        depth=4,
    )
    assert cross.status is CrossBackendStatus.BOUNDED_PASS

    mutated_text = sv_artifact.text.replace(" + 9'd1 + ", " + 9'd0 + ")
    assert mutated_text != sv_artifact.text
    mutated_artifact = publish_artifact(
        sv_module,
        mutated_text,
        backend="direct_systemverilog",
        selected_ir_identity=identity,
    )
    failed = run_cross_backend_formal(
        property_,
        clash_artifact,
        mutated_artifact,
        inputs=("port:a",),
        mode=CrossBackendMode.BMC,
        depth=4,
    )
    assert failed.status is CrossBackendStatus.FAILED
    assert failed.counterexample is not None
