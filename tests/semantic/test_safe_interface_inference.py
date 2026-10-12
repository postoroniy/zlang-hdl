from __future__ import annotations

import pytest

from zlang.backend.systemverilog import emit_artifact
from zlang.ir import FixedConvert
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


def test_complete_named_interface_surface_is_inherited() -> None:
    module = analyze(
        parse(
            """
interface PassIfc {
    in x : u8
    out y : u8
}
module Pass : PassIfc { y = x }
"""
        )
    )
    assert [(port.name, str(port.type)) for port in module.inputs] == [("x", "u8")]
    assert [(port.name, str(port.type)) for port in module.outputs] == [("y", "u8")]
    assert module.module_signature is not None
    assert restore(lower(module)) == module
    assert "module Pass" in emit_artifact(module).text


def test_applied_parameterized_interface_inherits_clock_ports_and_timing() -> None:
    module = analyze(
        parse(
            """
interface DelayIfc<type T, N=2> {
    clock clk
    reset rst
    in x : vec<N,T>
    out y : T
    timing { latency 2 ii 1 }
}
module Delay<type T, N=2> : DelayIfc<T=T,N=N> {
    y = delay<2>(x[0])
}
module Top {
    clock clk
    reset rst
    in x : vec<3,u8>
    out y : u8
    inst stage : Delay<T=u8,N=3> { x }
    y = stage.y
}
"""
        )
    )
    child = module.children[0]
    assert child.clock == "clk" and child.reset == "rst"
    assert str(child.inputs[0].type) == "vec<3,u8>"
    assert child.timing_contract is not None
    assert child.timing_contract.latency == 2


def test_full_legacy_redeclaration_remains_accepted() -> None:
    source = """
interface PassIfc { in x:u8 out y:u8 }
module Pass : PassIfc { in x:u8 out y:u8 y=x }
"""
    assert analyze(parse(source)).module_signature is not None


@pytest.mark.parametrize(
    "body",
    (
        "in x:u8 y=x",
        "out y:u8 y=0",
        "clock clk reset rst in x:u8 out y:u8 y=x",
    ),
)
def test_partial_interface_redeclaration_is_not_implicitly_completed(body: str) -> None:
    source = (
        "interface PassIfc { in x:u8 out y:u8 } "
        f"module Pass : PassIfc {{ {body} }}"
    )
    with pytest.raises(SemanticError, match="port set differs|clock/reset"):
        analyze(parse(source))


def test_inherited_interface_port_cannot_be_shadowed_by_state() -> None:
    with pytest.raises(SemanticError, match="duplicate state or port name 'x'"):
        analyze(
            parse(
                """
interface StatefulIfc { clock clk reset rst in x:u8 out y:u8 }
module Stateful : StatefulIfc { reg x:u8=0 y=x }
"""
            )
        )


def test_instance_specialization_is_inferred_from_complete_scalar_bindings() -> None:
    module = analyze(
        parse(
            """
module Cell<type T,N=2> {
    in x : vec<N,T>
    out y : T
    y = x[0]
}
module Top {
    in x : vec<3,u8>
    out y : u8
    inst cell : Cell { x }
    y = cell.y
}
"""
        )
    )
    assert tuple(
        (item.name, item.value) for item in module.instances[0].specializations
    ) == (("T", "u8"), ("N", 3))
    assert str(module.children[0].inputs[0].type) == "vec<3,u8>"
    assert restore(lower(module)) == module

    explicit = analyze(
        parse(
            """
module Cell<type T,N=2> {
    in x : vec<N,T>
    out y : T
    y = x[0]
}
module Top {
    in x : vec<3,u8>
    out y : u8
    inst cell : Cell<T=u8,N=3> { x }
    y = cell.y
}
"""
        )
    )
    assert (
        module.elaborated_instances[0].specialization_identity
        == explicit.elaborated_instances[0].specialization_identity
    )
    assert emit_artifact(module).artifact_hash == emit_artifact(explicit).artifact_hash


def test_instance_inference_uses_inherited_named_interface_inputs() -> None:
    module = analyze(
        parse(
            """
interface CellIfc<type T,N> { in x:vec<N,T> out y:T }
module Cell<type T,N> : CellIfc<T,N> { y=x[0] }
module Top {
    in x:vec<4,u16>
    out y:u16
    inst cell:Cell { x }
    y=cell.y
}
"""
        )
    )
    child = module.children[0]
    assert str(child.inputs[0].type) == "vec<4,u16>"
    assert child.module_signature is not None


def test_instance_inference_recurses_through_nominal_generic_struct_shape() -> None:
    module = analyze(
        parse(
            """
struct Box<type T> { value:T }
module Cell<type T> { in x:Box<T> out y:T y=x.value }
module Top {
    in x:Box<u16>
    out y:u16
    inst cell:Cell { x }
    y=cell.y
}
"""
        )
    )
    assert module.instances[0].specializations[0].value == "u16"


def test_instance_inference_rejects_conflicting_exact_bindings() -> None:
    source = """
module Pair<type T> { in a:T in b:T out y:T y=a }
module Top {
    in a:u8
    in b:u16
    out y:u8
    inst pair:Pair { a b }
    y=pair.y
}
"""
    with pytest.raises(SemanticError, match="conflicting exact inference"):
        analyze(parse(source))


def test_instance_inference_does_not_solve_width_equations_or_use_outputs() -> None:
    with pytest.raises(SemanticError, match="missing required value argument 'N'"):
        analyze(
            parse(
                """
module Cell<N> { in x:uint<N+1> out y:u8 y=truncate<8>(x) }
module Top { in x:u8 out y:u8 inst c:Cell{x} y=c.y }
"""
            )
        )
    with pytest.raises(SemanticError, match="missing required type argument 'T'"):
        analyze(
            parse(
                """
module Source<type T> { out y:T y=0 }
module Top { out y:u8 inst source:Source y=0 }
"""
            )
        )


def test_policy_explicit_contextual_quantize_has_explicit_form_identity() -> None:
    contextual = analyze(
        parse(
            """
module Q {
    in x:fixed<18,16>
    out y:SF_Sat8.8
    y=quantize(x) { round nearest_even overflow saturate }
}
"""
        )
    )
    explicit = analyze(
        parse(
            """
module Q {
    in x:fixed<18,16>
    out y:SF_Sat8.8
    y=quantize<SF_Sat8.8>(x) { round nearest_even overflow saturate }
}
"""
        )
    )
    left = contextual.assignments[0].expression
    right = explicit.assignments[0].expression
    assert isinstance(left, FixedConvert)
    assert left == right
    assert lower(contextual).assignments == lower(explicit).assignments


def test_contextual_quantize_requires_one_explicit_fixed_destination() -> None:
    with pytest.raises(SemanticError, match="unambiguous fixed-point target"):
        analyze(
            parse(
                """
module Q {
    in x:fixed<18,16>
    y=quantize(x) { round nearest_even overflow saturate }
}
"""
            )
        )
