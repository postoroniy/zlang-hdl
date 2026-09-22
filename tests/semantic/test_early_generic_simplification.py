from __future__ import annotations

from zlang.compiler import compile_source
from zlang.ir import (
    Add,
    Binary,
    Call,
    Constant,
    Extend,
    Generate,
    Reduce,
    RuntimeIndex,
    Truncate,
    VectorIndex,
)
from zlang.ir.traversal import walk_expression
from zlang.ir.normalization import normalize_selected_values
from zlang.simulate import simulate


def _expressions(value):
    return tuple(walk_expression(value))


def test_generic_scalar_constant_is_published_at_specialization_site() -> None:
    module = normalize_selected_values(compile_source(
        "fn value<K>()->u8{K} module Top{out y:u8 y=value<K=37>()}"
    ).ir)

    assert isinstance(module.assignments[0].expression, Constant)
    assert module.assignments[0].expression.value == 37
    assert len(module.generic_specializations) == 1


def test_generic_body_becomes_constant_after_parameter_substitution() -> None:
    module = normalize_selected_values(compile_source(
        "fn selected<K>(unused:u8)->u8{if K == 3 { 9 } else { 4 }} "
        "module Top{in x:u8 out y:u8 y=selected<K=3>(x)}"
    ).ir)

    assert isinstance(module.assignments[0].expression, Constant)
    assert module.assignments[0].expression.value == 9


def test_generic_body_keeps_runtime_term_but_collapses_compile_time_zero() -> None:
    module = compile_source(
        "fn c3<K>()->u3{K} "
        "fn mixed<P>(x:u3,y:u3)->u7{x + (c3<K=P>() * y)} "
        "module Top{in x:u3 in y:u3 out z:u7 z=mixed<P=0>(x,y)}"
    ).ir
    optimized = normalize_selected_values(module, prune_callables=False)
    mixed = next(
        item
        for item in optimized.callable_definitions
        if item.metadata is not None and item.metadata.source_name == "mixed"
    )

    assert isinstance(mixed.body, Extend)
    assert not any(
        isinstance(item, Binary) and item.operator.value == "*"
        for item in _expressions(mixed.body)
    )
    for x in range(8):
        assert simulate(module, x=x, y=7)["z"] == x


def test_constant_generic_vector_index_is_static_not_runtime_mux() -> None:
    module = compile_source(
        "fn pick<K>(v:vec<4,u8>)->u8{v[K]} "
        "module Top{in v:vec<4,u8> out y:u8 y=pick<K=2>(v)}"
    ).ir
    definition = module.callable_definitions[0]

    assert isinstance(definition.body, VectorIndex)
    assert definition.body.index == 2
    assert not any(isinstance(item, RuntimeIndex) for item in _expressions(definition.body))


def test_nested_constant_generic_vector_indices_are_direct_static_selections() -> None:
    module = compile_source(
        "fn pick<L,P,B>(v:vec<2,vec<3,vec<4,u8>>>)->u8{v[L][P][B]} "
        "module Top{in v:vec<2,vec<3,vec<4,u8>>> out y:u8 "
        "y=pick<L=1,P=2,B=3>(v)}"
    ).ir
    definition = module.callable_definitions[0]
    indices = tuple(
        item.index
        for item in _expressions(definition.body)
        if isinstance(item, VectorIndex)
    )

    assert indices == (3, 2, 1)
    assert not any(isinstance(item, RuntimeIndex) for item in _expressions(definition.body))


def test_generate_calling_generic_reuses_one_specialization_per_value() -> None:
    module = compile_source(
        "fn value<K>()->u3{K} module Top{out y:vec<4,u3> "
        "y=generate(i in 0..4) value<K=i>()}"
    ).ir
    generated = normalize_selected_values(module).assignments[0].expression

    assert isinstance(generated, Generate)
    assert [item.value for item in generated.elements if isinstance(item, Constant)] == [0, 1, 2, 3]
    assert len(module.generic_specializations) == 4


def test_nested_generate_generic_values_keep_source_index_order() -> None:
    module = compile_source(
        "fn value<K>()->u4{K} module Top{out y:vec<2,vec<3,u4>> "
        "y=generate(i in 0..2) generate(j in 0..3) value<K=i*3+j>()}"
    ).ir

    assert simulate(module)["y"] == [[0, 1, 2], [3, 4, 5]]
    assert len(module.generic_specializations) == 6


def test_generate_reduce_generic_function_preserves_semantics() -> None:
    module = compile_source(
        "fn mask<K>(x:u4)->u4{mux(x == K, 1, 0)} "
        "module Top{in x:u4 out y:u4 "
        "y=reduce(|,generate(i in 0..4) mask<K=i>(x))}"
    ).ir
    assert isinstance(module.assignments[0].expression, Reduce)

    assert [simulate(module, x=value)["y"] for value in range(8)] == [1, 1, 1, 1, 0, 0, 0, 0]


def test_repeated_identical_specialization_has_one_definition_and_record() -> None:
    module = compile_source(
        "fn add<K>(x:u8)->u9{x+K} module Top{in x:u8 out a:u9 out b:u9 "
        "a=add<K=1>(x) b=add<K=1>(x)}"
    ).ir

    assert len(module.generic_specializations) == 1
    assert len(module.callable_definitions) == 1
    assert all(isinstance(item.expression, Call) for item in module.assignments)


def test_zero_add_preserves_carry_width_instead_of_returning_narrow_operand() -> None:
    module = normalize_selected_values(compile_source(
        "module Top{in x:u8 out y:u9 y=x+0}"
    ).ir)
    value = module.assignments[0].expression

    assert isinstance(value, Extend)
    assert str(value.expression.type) == "u8"
    assert str(value.type) == "u9"


def test_truncate_barrier_is_not_removed_by_local_simplification() -> None:
    module = normalize_selected_values(compile_source(
        "module Top{in x:u8 out y:u8 y=truncate<8>(x+1)}"
    ).ir)
    value = module.assignments[0].expression

    assert isinstance(value, Truncate)
    assert isinstance(value.expression, Add)
    assert simulate(module, x=255)["y"] == 0


def test_range_disjoint_generic_candidates_fold_before_generate_is_retained() -> None:
    module = compile_source(
        "fn hit<D>(x:u2)->bit{extend<3>(x) == D} "
        "module Top{in x:u2 out hits:vec<8,bit> "
        "hits=generate(d in 0..8) hit<D=d>(x)}"
    ).ir
    generated = normalize_selected_values(module).assignments[0].expression

    assert isinstance(generated, Generate)
    assert all(isinstance(item, Constant) and item.value == 0 for item in generated.elements[4:])
    for value in range(4):
        assert simulate(module, x=value)["hits"] == [
            int(index == value) for index in range(8)
        ]


def test_nonzero_generate_start_static_selection_uses_logical_vector_index() -> None:
    module = normalize_selected_values(compile_source(
        "module Top{out y:u4 y=(generate(i in 5..9) i)[0]}"
    ).ir)

    assert isinstance(module.assignments[0].expression, Constant)
    assert module.assignments[0].expression.value == 5
