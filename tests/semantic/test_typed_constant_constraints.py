"""Typed integer/bit specialization constants remain compile-time in where."""

import pytest

from zlang.compiler import compile_source
from zlang.backend.systemverilog import emit_artifact
from zlang.semantic import SemanticError


@pytest.mark.parametrize("kind,value,condition", (
    ("bit", "1", "valid == 1"),
    ("bits<3>", "0b101", "valid == 5"),
    ("uint<3>", "5", "valid % 3 == 2"),
    ("sint<3>", "-3", "valid < 0 && valid % 2 == 1"),
))
def test_typed_constant_where_accepts_exact_integer_value(kind, value, condition):
    source = f"""module ConstantContract<valid:{kind}> where {condition} {{}}
module Top {{ in x:bit out y:bit constant:{kind}={value}
 inst check:ConstantContract<valid=constant> y=x }}"""
    result = compile_source(source, top="Top")
    assert emit_artifact(result.ir).text


def test_constant_predicate_rejects_false_before_emission():
    source = """module ConstantContract<valid:bit> where valid == 1 {}
module Top { in x:bit out y:bit constant:bit=0
 inst check:ConstantContract<valid=constant> y=x }"""
    with pytest.raises(SemanticError, match="parameter constraint is not satisfied"):
        compile_source(source, top="Top")


def test_where_does_not_reinterpret_fixed_point_raw_bits_as_integer():
    source = """module ConstantContract<valid:fixed<4,2>> where valid == 1 {}
module Top { in x:bit out y:bit constant:fixed<4,2>=0.25
 inst check:ConstantContract<valid=constant> y=x }"""
    with pytest.raises(SemanticError, match="parameter constraint cannot be discharged"):
        compile_source(source, top="Top")


def test_where_does_not_admit_runtime_constant_argument():
    source = """module ConstantContract<valid:bit> where valid == 1 {}
module Top { in x:bit out y:bit inst check:ConstantContract<valid=x> y=x }"""
    with pytest.raises(SemanticError, match="not a module-local value"):
        compile_source(source, top="Top")


@pytest.mark.parametrize("last", (1, 0))
def test_constant_reduction_predicate_forwards_through_specialization(last):
    source = f"""fn predicate<N>(values:vec<N,bit>) -> bit {{ reduce(&,values) }}
module ConstantContract<valid:bit> where valid == 1 {{}}
module Outer<values:vec<3,bit>> {{
 valid=predicate<N=3>(values) inst check:ConstantContract<valid=valid> }}
module Top {{ in x:bit out y:bit values:vec<3,bit>=[1,1,{last}]
 inst outer:Outer<values=values> y=x }}"""
    if last:
        assert emit_artifact(compile_source(source, top="Top").ir).text
    else:
        with pytest.raises(SemanticError, match="parameter constraint is not satisfied"):
            compile_source(source, top="Top")


def test_callable_derived_defaults_use_callee_bindings():
    source = """fn widen_first<N,M=N+1>(value:vec<N,u1>) -> uint<M> { extend<M>(value[0]) }
module Top { in x:vec<3,u1> out y:u4 out z:u5 y=widen_first(x) z=widen_first<M=5>(x) }"""
    assert emit_artifact(compile_source(source, top="Top").ir).text


@pytest.mark.parametrize("default,match", (("5%0", "modulo by zero"), ("3/2", "division must be exact"),
                                           ("N+1", "defaults may reference only earlier parameters")))
def test_callable_derived_default_errors_fail_closed(default, match):
    source = f"fn value<N={default}>()->u8{{N}} module Top<N=7>{{out y:u8 y=value()}}"
    with pytest.raises(SemanticError, match=match):
        compile_source(source, top="Top")


def test_callable_derived_default_cannot_capture_caller_parameter():
    source = "fn value<M=R+1>()->u8{M} module Top<R=7>{out y:u8 y=value()}"
    with pytest.raises(SemanticError, match="unresolved compile-time parameter 'R'"):
        compile_source(source, top="Top")
