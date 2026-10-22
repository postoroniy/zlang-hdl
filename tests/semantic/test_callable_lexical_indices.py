"""Callee loop binders/refinements never capture the caller's lexical names."""

import pytest

from zlang import sim
from zlang.compiler import compile_source
from zlang.semantic import SemanticError


def source(inner='i'):
    return f'''fn choose<N>(v:vec<N,u8>,key:u3)->u8 {{
 reduce(|,generate({inner} in 0..N)(key=={inner} ? v[{inner}] : 0))
}}
module Top {{in a:vec<2,u8> out y:vec<2,u8>
 y=generate(i in 0..2) choose<N=2>(a,extend<3>(i))}}
'''


@pytest.mark.parametrize('inner',('i','j'))
def test_callee_index_is_independent_of_caller_index(tmp_path,inner):
    path = tmp_path/'top.zhl'
    path.write_text(source(inner))
    with sim.compile(path).create() as machine:
        machine.set('a',[17,249])
        machine.eval()
        assert list(machine.get('y')) == [17,249]


def test_callee_cannot_capture_undeclared_caller_index():
    text = '''fn leaked<N>(x:u8)->u8{truncate<8>(x+extend<8>(i))}
module Top {in a:u8 out y:vec<2,u8> y=generate(i in 0..2) leaked<N=2>(a)}'''
    with pytest.raises(SemanticError,match='i'):
        compile_source(text)


def test_same_callable_scope_still_rejects_shadowed_index():
    text = '''fn bad<N>(v:vec<N,u8>)->vec<N,vec<N,u8>> {
 generate(i in 0..N) generate(i in 0..N) v[i]}
module Top {in a:vec<2,u8> out y:vec<2,vec<2,u8>> y=bad<N=2>(a)}'''
    with pytest.raises(SemanticError,match='shadows'):
        compile_source(text)


def test_symbolic_generic_arguments_survive_lexical_isolation(tmp_path):
    text = '''fn prefix<N>(v:vec<4,u8>)->vec<N,u8> {generate(i in 0..N) v[i]}
module Top {in a:vec<4,u8> out y:vec<3,u8>
 y=generate(i in 0..3) truncate<8>(sum(prefix<N=i+1>(a)))}'''
    path = tmp_path/'top.zhl'
    path.write_text(text)
    with sim.compile(path).create() as machine:
        machine.set('a',[3,7,11,19])
        machine.eval()
        assert list(machine.get('y')) == [3,10,21]


def test_callee_does_not_inherit_same_named_caller_index_range():
    text = '''fn pick<N>(v:vec<N,u8>,key:u3)->u8{v[key]}
module Top {clock clk reset rst in a:vec<4,u8> in key:u3 out y:u8
 reg result:u8=0 when key<4 {result <- pick<N=4>(a,key)} y=result}'''
    with pytest.raises(SemanticError,match='range|index'):
        compile_source(text)
