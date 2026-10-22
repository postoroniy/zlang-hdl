"""Grammar-admitted integer bitwise conditions use exact parameter semantics."""

from itertools import product
import operator

import pytest

from zlang.compiler import compile_source
from zlang.ir.constants import constant_runtime_value
from zlang.semantic import SemanticError


@pytest.mark.parametrize("symbol,operation", (("&", operator.and_),
                                             ("|", operator.or_),
                                             ("^", operator.xor)))
def test_parameter_bitwise_if_matches_independent_integers(symbol, operation):
    values = (-129, -5, -1, 0, 1, 3, 64, (1 << 129)+1)
    for a, b in product(values, repeat=2):
        expected = operation(a, b)
        source = f"""module Top<A={a},B={b}> {{
 out y:bit
 if (A {symbol} B) == {expected} {{ y=1 }} else {{ y=0 }}
}}"""
        result = compile_source(source)
        assert constant_runtime_value(result.ir.assignments[0].expression) == 1


@pytest.mark.parametrize("symbol,expected", (("&", 1), ("|", 7), ("^", 6)))
def test_where_uses_the_same_integer_bitwise_semantics(symbol, expected):
    good = f"module Top<A=5,B=3> where (A {symbol} B) == {expected} {{out y:bit y=1}}"
    assert compile_source(good).ir
    with pytest.raises(SemanticError, match="parameter constraint is not satisfied"):
        compile_source(good.replace(f"== {expected}", f"== {expected+1}"))


def test_generated_bit_masks_use_compile_time_conditions():
    text = """module Top<N=9,J=2> {
 out y:bits<N>
 y=bitcast<bits<N>>(generate(i in 0..N)
     if ((i+1) & (1 << J)) != 0 {0b1} else {0b0})
}"""
    result = compile_source(text)
    assert constant_runtime_value(result.ir.assignments[0].expression) == 0b001111000


@pytest.mark.parametrize("symbol", ("&", "|", "^"))
def test_bitwise_compile_time_if_never_admits_runtime_operands(symbol):
    source = f"module Top{{in x:u8 out y:bit if (x {symbol} 0)==0 {{y=1}}else{{y=0}}}}"
    with pytest.raises(SemanticError, match="runtime value 'x'"):
        compile_source(source)


def test_unselected_branch_does_not_evaluate_invalid_arithmetic():
    source = """module Top<K=5> {out y:bit
 if (K & 3)==1 {y=1} else {y=1%0}
}"""
    assert constant_runtime_value(compile_source(source).ir.assignments[0].expression) == 1
