import pytest

from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


def test_named_primitive_fixed_and_type_value_specializations_are_concrete() -> None:
    source = """
module Cell<type T,D=4> {
  clock c reset r in x:T out y:T
  fifo q:fifo<T,D> q.data=x q.push=0 q.pop=0 y=q.front
}
module Top {
  clock c reset r
  in a:u8 in b:fixed<18,16> out y:u8
  inst u:Cell<T=u8,D=4> {x=a}
  inst f:Cell<T=fixed<18,16>,D=8> {x=b}
  y=u.y
}
"""
    module = analyze(parse(source))
    u8_child, fixed_child = module.children
    assert str(u8_child.inputs[0].type) == "u8"
    assert u8_child.fifos[0].depth == 4
    assert str(fixed_child.inputs[0].type) == "fixed<18,16>"
    assert fixed_child.fifos[0].depth == 8
    assert module.instances[1].specializations[0].value == "fixed<18,16>"
    assert module.instances[1].specializations[1].value == 8


def test_positional_specialization_matches_named_specialization() -> None:
    base = "module Cell<type T,D=4>{in x:T out y:T y=x} "
    named = analyze(parse(
        base + "module Top{in x:u8 out y:u8 inst c:Cell<T=u8,D=8>{x} y=c.y}"
    ))
    positional = analyze(parse(
        base + "module Top{in x:u8 out y:u8 inst c:Cell<u8,8>{x} y=c.y}"
    ))
    assert named.children[0] == positional.children[0]
    assert (
        named.elaborated_instances[0].specialization_identity
        == positional.elaborated_instances[0].specialization_identity
    )


def test_generic_struct_and_alias_arguments_are_canonical_and_identity_stable() -> None:
    source = """
import std.math.complex
type Byte=u8
module Cell<type T>{in x:T out y:T y=x}
module Top {
  in a:u8
  in z:Complex<fixed<18,16>>
  out y:u8
  inst alias:Cell<T=Byte>{x=a}
  inst concrete:Cell<T=u8>{x=a}
  inst complex:Cell<T=Complex<fixed<18,16>>>{x=z}
  y=alias.y
}
"""
    module = analyze(parse(source))
    alias, concrete, complex_child = module.children
    assert alias.inputs[0].type == concrete.inputs[0].type
    assert str(complex_child.inputs[0].type) == "Complex<fixed<18,16>>"
    identities = {
        item.instance.name: item.specialization_identity
        for item in module.elaborated_instances
    }
    assert identities["alias"] == identities["concrete"]
    assert identities["complex"] != identities["concrete"]


def test_type_and_value_changes_both_participate_in_specialization_identity() -> None:
    source = """
module Cell<type T,D=4>{clock c reset r in x:T fifo q:fifo<T,D>
q.data=x q.push=0 q.pop=0}
module Top{clock c reset r in a:u8 in b:u16
inst u4:Cell<T=u8,D=4>{x=a}
inst u8:Cell<T=u8,D=8>{x=a}
inst w4:Cell<T=u16,D=4>{x=b}}
"""
    module = analyze(parse(source))
    identities = {
        item.specialization_identity for item in module.elaborated_instances
    }
    assert len(identities) == 3


def test_nested_parent_type_parameter_propagates_to_grandchild() -> None:
    source = """
module Grand<type U>{in x:U out y:U y=x}
module Child<type T>{in x:T out y:T inst g:Grand<U=T>{x} y=g.y}
module Top{in x:fixed<18,16> out y:fixed<18,16>
inst c:Child<T=fixed<18,16>>{x} y=c.y}
"""
    module = analyze(parse(source))
    child = module.children[0]
    grand = child.children[0]
    assert str(child.inputs[0].type) == "fixed<18,16>"
    assert str(grand.inputs[0].type) == "fixed<18,16>"
    assert grand.parameters == (("U", "type", "fixed<18,16>"),)


def test_type_parameter_resolves_inside_parameterized_vector() -> None:
    source = (
        "module Cell<type T,N=2>{in x:vec<N,T> out y:T y=x[0]} "
        "module Top{in x:vec<4,u16> out y:u16 "
        "inst c:Cell<T=u16,N=4>{x} y=c.y}"
    )
    child = analyze(parse(source)).children[0]
    assert str(child.inputs[0].type) == "vec<4,u16>"
    assert str(child.outputs[0].type) == "u16"


def test_specialized_hierarchy_canonical_round_trip_has_no_type_parameters() -> None:
    source = (
        "module Cell<type T,D=4>{clock c reset r in x:T out y:T "
        "fifo q:fifo<T,D> q.data=x q.push=0 q.pop=0 y=q.front} "
        "module Top{clock c reset r in x:u8 out y:u8 "
        "inst cell:Cell<T=u8,D=8>{x} y=cell.y}"
    )
    semantic = analyze(parse(source))
    canonical = lower(semantic)
    restored = restore(canonical)
    assert restored == semantic
    assert str(restored.children[0].inputs[0].type) == "u8"
    assert restored.children[0].fifos[0].depth == 8


@pytest.mark.parametrize(
    ("instance", "message"),
    (
        ("inst c:Cell<D=4>", "missing required type argument 'T'"),
        ("inst c:Cell<T=Missing,D=4>{x}", "cannot resolve type argument.*unknown type 'Missing'"),
        ("inst c:Cell<T=4,D=4>{x}", "type parameter 'T'.*requires a type argument"),
        ("inst c:Cell<T=u8,D=u16>{x}", "value parameter 'D'.*cannot receive a type argument"),
        ("inst c:Cell<T=u8,T=u16,D=4>{x}", "parameter 'T' is assigned more than once"),
        ("inst c:Cell<T=u8,D=4,9>{x}", "too many specialization arguments"),
        ("inst c:Cell<T=u8>{x}", "missing required value argument 'D'"),
    ),
)
def test_module_specialization_diagnostics(instance: str, message: str) -> None:
    source = (
        "module Cell<type T,D>{in x:T out y:T y=x} "
        f"module Top{{in x:u8 out y:u8 {instance} y=x}}"
    )
    with pytest.raises(SemanticError, match=message):
        analyze(parse(source))


def test_unresolved_parent_type_parameter_is_diagnostic() -> None:
    source = (
        "module Grand<type U>{in x:U out y:U y=x} "
        "module Parent<type T>{in x:T out y:T inst g:Grand<U=T>{x} y=g.y}"
    )
    with pytest.raises(SemanticError, match="unknown type 'T'"):
        analyze(parse(source))
