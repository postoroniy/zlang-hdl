from pathlib import Path

import pytest

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


SOURCE = """
module SignedProductFormal {
    in a:fixed<4,2> in b:fixed<4,2>
    in c:fixed<4,2> in d:fixed<4,2>
    out y:fixed<9,4>
    y = a*b - c*d
}
"""


def _formal_source(implementation: str):
    module = compile_source(SOURCE).ir
    expression = module.assignments[0].expression
    selected = "selected:signed-product-reduction"
    reference = emit_reference_model(
        "SignedProductReference", "y", expression.type,
        tuple((port.name, port.type) for port in module.inputs), expression,
    )
    property_ = make_equivalence_property(
        expression, expression, candidate_class="m31",
        reference_root="signed-product-reference",
        implementation_root="direct_systemverilog",
        inputs=tuple(f"port:{port.name}" for port in module.inputs),
        reference_output="port:y", implementation_output="port:y",
    )
    names = {f"port:{port.name}": port.name for port in module.inputs}
    names["port:y"] = "y"
    reference_bindings = publish_bindings(
        module, side=BindingSide.REFERENCE,
        selected_ir_identity=selected, backend="semantic_reference",
        artifact_hash_value=artifact_hash(reference), rtl_names=names,
    )
    implementation_bindings = publish_bindings(
        module, side=BindingSide.IMPLEMENTATION,
        selected_ir_identity=selected, backend="direct_systemverilog",
        artifact_hash_value=artifact_hash(implementation), rtl_names=names,
    )
    miter = emit_miter(
        property_, BindingMap((*reference_bindings, *implementation_bindings)),
        reference_module="SignedProductReference",
        implementation_module=module.name,
    )
    top = "m36_" + property_.id.replace(".", "_")
    return property_, reference + "\n" + implementation + "\n" + miter, top


@pytest.mark.skipif(
    len(formal_tools_available()) != 3,
    reason="Yosys/SymbiYosys formal tools are unavailable",
)
def test_correct_signed_product_reduction_is_proven() -> None:
    module = compile_source(SOURCE).ir
    implementation = emit_artifact(
        module, selected_ir_identity="selected:signed-product-reduction"
    ).text
    property_, source, top = _formal_source(implementation)
    result = run_equivalence_formal(
        property_, source, top=top, mode=EquivalenceMode.PROVE, depth=4,
    )
    assert result.status is EquivalenceStatus.PROVEN


@pytest.mark.skipif(
    len(formal_tools_available()) != 3,
    reason="Yosys/SymbiYosys formal tools are unavailable",
)
@pytest.mark.parametrize(
    "body",
    (
        "assign y = $signed(p0) + $signed(p1);",
        "assign y = $signed(p1) - $signed(p0);",
        "assign y = $signed(p0);",
        "logic signed [5:0] q0,q1; assign q0=$signed(p0)>>>2; "
        "assign q1=$signed(p1)>>>2; assign y=($signed(q0)-$signed(q1))<<<2;",
    ),
    ids=("subtract-to-add", "wrong-term-order", "dropped-term", "premature-quantization"),
)
def test_signed_product_mutations_fail_with_counterexamples(body: str) -> None:
    implementation = f"""
module SignedProductFormal(
  input logic signed [3:0] a,b,c,d,
  output logic signed [8:0] y
);
  logic signed [7:0] p0,p1;
  assign p0=$signed(a)*$signed(b);
  assign p1=$signed(c)*$signed(d);
  {body}
endmodule
"""
    property_, source, top = _formal_source(implementation)
    result = run_equivalence_formal(
        property_, source, top=top, mode=EquivalenceMode.BMC, depth=4,
    )
    assert result.status is EquivalenceStatus.FAILED
    assert result.counterexample is not None
