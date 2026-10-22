"""Derived callee defaults resolve exact input widths without caller capture."""

import pytest

from zlang.compiler import compile_source
from zlang.semantic import SemanticError


@pytest.mark.parametrize("shape,value", (("bits<W+1>", "bits<6>"),
    ("vec<2,bits<W+1>>", "vec<2,bits<6>>"),
    ("(bits<W+1>,bit)", "(bits<6>,bit)")))
def test_derived_default_in_exact_input_type(shape, value):
    source = f"""fn identity<N,W=N+1>(x:{shape})->{shape}{{x}}
 module Top{{in x:{value} out y:{value} y=identity<N=4>(x)}}"""
    result = compile_source(source)
    assert result.ir.generic_specializations[0].return_type == result.ir.ports[-1].type


def test_explicit_value_overrides_input_default():
    source = """fn identity<N,W=N+1>(x:bits<W+1>)->bits<W+1>{x}
 module Top{in x:bits<9> out y:bits<9> y=identity<N=4,W=8>(x)}"""
    assert compile_source(source).ir


def test_wrong_input_width_still_rejects():
    source = """fn identity<N,W=N+1>(x:bits<W+1>)->bits<W+1>{x}
 module Top{in x:bits<7> out y:bits<7> y=identity<N=4>(x)}"""
    with pytest.raises(SemanticError, match="exact type unification"):
        compile_source(source)


def test_direct_inference_still_precedes_a_default():
    source = """fn identity<N=8>(x:vec<N,bit>)->vec<N,bit>{x}
 module Top{in x:vec<3,bit> out y:vec<3,bit> y=identity(x)}"""
    assert compile_source(source).ir.generic_specializations[0].return_type.length == 3


def test_input_default_cannot_capture_same_named_caller_parameter():
    source = """fn identity<N,W=N+1>(x:bits<W+1>)->bits<W+1>{x}
 module Top<N=4>{in x:bits<6> out y:bits<6> y=identity(x)}"""
    with pytest.raises(SemanticError, match="unresolved compile-time parameter 'N'"):
        compile_source(source)


def test_local_default_precedes_same_named_caller_default():
    source = """fn identity<N,W=N+1>(x:bits<W+1>)->bits<W+1>{x}
 module Top<W=99>{in x:bits<6> out y:bits<6> y=identity<N=4>(x)}"""
    assert compile_source(source).ir
