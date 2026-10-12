from __future__ import annotations

import pytest

from zlang.ast import CallableRef, TypeName, VectorTypeName
from zlang.parser import ParseError, parse


def test_typed_constant_and_callable_parameter_surface() -> None:
    module = parse("""
        fn widen(x:u8)->u9 { extend<9>(x) }
        fn apply<type A,type B,table:vec<2,A>,op:fn(A)->B>(x:A) {
            op(table[0])
        }
        module Top {
            in x:u8 out y:u9
            table:vec<2,u8>=generate(i in 0..2) extend<8>(i)
            y=apply<A=u8,B=u9,table=table,op=fn widen>(x)
        }
    """)

    parameters = module.functions[1].generic_parameters
    assert tuple(item.kind for item in parameters) == (
        "type", "type", "constant", "callable"
    )
    assert parameters[2].type_name == VectorTypeName(2, TypeName("A"))
    assert parameters[3].callable_parameters == (TypeName("A"),)
    assert parameters[3].callable_return_type == TypeName("B")
    call = module.assignments[-1].expression
    assert isinstance(call.specializations[-1].value, CallableRef)
    assert call.specializations[-1].value.name == "widen"


def test_callable_reference_can_explicitly_specialize_generic_target() -> None:
    module = parse("""
        fn convert<type A,type B>(x:A)->B { extend<9>(x) }
        fn apply<type A,type B,op:fn(A)->B>(x:A) { op(x) }
        module Top { in x:u8 out y:u9
          y=apply<A=u8,B=u9,op=fn convert<A=u8,B=u9>>(x) }
    """)
    reference = module.assignments[0].expression.specializations[-1].value
    assert isinstance(reference, CallableRef)
    assert tuple(item.name for item in reference.specializations) == ("A", "B")


def test_compile_time_callable_default_is_not_part_of_the_first_slice() -> None:
    with pytest.raises(ParseError):
        parse("""
            fn identity(x:u8)->u8{x}
            fn apply<op:fn(u8)->u8=fn identity>(x:u8){op(x)}
            module Top{in x:u8 out y:u8 y=apply<op=fn identity>(x)}
        """)
