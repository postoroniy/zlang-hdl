"""Compile-time modulo never becomes a runtime remainder operation."""

import pytest

from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.native_simulation import simulate
from zlang.semantic import SemanticError


PSS_FUNCTION = """
fn nr_pss_chip<ID,K>() -> s2 {
    mseq = 0x42afa51b8fe1de592044c5d6c19a9cf6
    mseq[(K + 43 * ID) % 127] ? -1 : 1
}
"""


@pytest.mark.parametrize(('a', 'b', 'expected'), (
    (5, 3, 2), (-5, 3, 1), (5, -3, -1), (-5, -3, -2),
    (-6, 3, 0), (6, -3, 0), (0, -3, 0), (1, 1, 0),
))
def test_modulo_has_the_same_floor_semantics_in_all_compile_time_contexts(a, b, expected):
    source = f"""
        fn value<N>() -> s8 {{ N + 0 }}
        module W<A={a},B={b}> {{
            out ordinary:s8 out generic:s8 out selected:bit
            ordinary=A % B
            generic=value<N=({a}) % ({b})>()
            if A % B == {expected} {{ selected=1 }} else {{ selected=0 }}
        }}
    """
    compiled = compile_source(source)
    assert simulate(compiled.ir) == dict(ordinary=expected, generic=expected, selected=1)
    assert '%' not in emit(compiled.ir)


def test_literal_modulo_and_immutable_constants_are_compile_time_integers():
    compiled = compile_source('''module W {
        out a:s8 out b:s8 out c:u2
        numerator=-5 denominator=3
        a=numerator % denominator
        b=5 % -3
        c=(3 % 2) + 0
    }''')
    assert simulate(compiled.ir) == dict(a=1, b=-1, c=1)


def test_generic_module_binding_and_static_vector_index_accept_modulo():
    compiled = compile_source('''
        module Child<N=0>{ out y:u8 y=N }
        module W<K=8>{ out y:u8 out selected:u8
            table:vec<3,u8>=[10,20,30]
            inst child:Child<N=(K+1)%7>
            y=child.y
            selected=table[K%3]
        }
    ''')
    assert simulate(compiled.ir) == dict(y=2, selected=30)


@pytest.mark.parametrize('ident', (0, 1, 2))
def test_nr_pss_chip_matches_all_127_chips(ident):
    compiled = compile_source(PSS_FUNCTION + f'''
        module W {{ out chips:vec<127,s2>
            chips=generate(k in 0..127) nr_pss_chip<ID={ident},K=k>()
        }}
    ''')
    sequence = 0x42afa51b8fe1de592044c5d6c19a9cf6
    expected = [-1 if sequence >> ((k + 43*ident) % 127) & 1 else 1 for k in range(127)]
    assert list(simulate(compiled.ir)['chips']) == expected
    assert '%' not in emit(compiled.ir)


@pytest.mark.parametrize('source', (
    'module W {out y:u8 y=5%0}',
    'module W<D=0>{out y:u8 y=5%D}',
    'module W {out y:bit if 5%0 == 0 {y=1} else {y=0}}',
    'fn f<N>()->u8{N} module W {out y:u8 y=f<N=5%0>()}',
    'module C<N>{out y:u8 y=N} module W {out y:u8 inst c:C<N=5%0> y=c.y}',
    'module W {out y:bit table=0xff y=table[5%0]}',
))
def test_zero_divisor_is_a_source_diagnostic(source):
    with pytest.raises(SemanticError, match='modulo by zero') as caught:
        compile_source(source, source_unit='modulo.zhl')
    assert caught.value.primary is not None
    assert caught.value.primary.span.start_line >= 1
    assert caught.value.code == 'ZL-SEMANTIC-MODULO-ZERO'


@pytest.mark.parametrize('expression', ('x%3', '5%x', 'x%x', '(x-x)%3'))
def test_runtime_modulo_is_rejected_even_when_optimization_could_cancel_operands(expression):
    with pytest.raises(SemanticError, match='runtime remainder is not supported'):
        compile_source(f'module W{{in x:u8 out y:u8 y={expression}}}')


def test_unselected_compile_time_branch_does_not_evaluate_modulo_by_zero():
    compiled = compile_source('module W<N=1>{out y:u8 y=if N%2 == 1 {7} else {3%0}}')
    assert simulate(compiled.ir) == {'y': 7}


def test_modulo_precedence_and_literal_identity():
    compiled = compile_source('module W<K=8>{out y:u8 y=1+K%3*2}')
    literal = compile_source('module W{out y:u8 y=5}')
    assert simulate(compiled.ir) == {'y': 5}
    assert emit(compiled.ir) == emit(literal.ir)


def test_generate_generic_modulo_stays_compile_time_for_large_regions():
    compiled = compile_source('''fn value<K>()->u8{K}
        module W{out y:vec<128,u8> y=generate(k in 0..128) value<K=k%3>()}''')
    assert list(simulate(compiled.ir)['y']) == [i % 3 for i in range(128)]


def test_reset_initialized_register_is_not_a_compile_time_operand():
    with pytest.raises(SemanticError, match='runtime remainder is not supported'):
        compile_source('module W{clock clk reset rst reg q:u8=5 out y:u8 y=q%3}')


def test_zero_divisor_keeps_exact_condition_line_and_source_unit():
    source = 'module W {\n out y:bit\n if 5%0 == 0 { y=1 } else { y=0 }\n}'
    with pytest.raises(SemanticError, match='modulo by zero') as caught:
        compile_source(source, source_unit='modulo.zhl')
    assert caught.value.primary.span.start_line == 3
    assert caught.value.primary.source_unit == 'modulo.zhl'
