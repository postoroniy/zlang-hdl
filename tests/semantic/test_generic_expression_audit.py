import math

import pytest

from zlang.ir import (
    Add,
    Generate,
    InputRef,
    Mux,
    RegisterRef,
    RuntimeIndex,
    Truncate,
    ValueRange,
    VectorIndex,
)
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate, simulate_cycles


def _module(source: str):
    return analyze(parse(source))


@pytest.mark.parametrize(
    ("source", "index_kind", "bounds", "provenance"),
    (
        (
            "module A{in v:vec<4,u8> in i:u2 out y:u8 y=v[i]}",
            InputRef, (0, 3), "static_type",
        ),
        (
            "module A{clock c reset r in v:vec<4,u8> out y:u8 "
            "reg i:u2=0 i<-truncate<2>(i+1) y=v[i]}",
            RegisterRef, (0, 3), "static_type",
        ),
        (
            "module A{clock c reset r in v:vec<4,u8> out y:u8 "
            "reg counter:u3=0 slot=truncate<2>(counter) "
            "counter<-truncate<3>(counter+1) y=v[slot]}",
            Truncate, (0, 3), "truncate",
        ),
        (
            "module A{in v:vec<4,u8> in i:u3 out y:u8 "
            "y=v[truncate<2>(i)]}",
            Truncate, (0, 3), "truncate",
        ),
        (
            "module A{in v:vec<8,u8> in i:u2 out y:u8 y=v[i+1]}",
            Add, (1, 4), "arithmetic",
        ),
        (
            "module A{in v:vec<4,u8> in a:u2 in b:u2 in c:bit "
            "out y:u8 y=v[c?a:b]}",
            Mux, (0, 3), "union",
        ),
        (
            "fn low<IW=2>(x:u3)->uint<IW>{truncate<IW>(x)} "
            "module A{in v:vec<4,u8> in i:u3 out y:u8 y=v[low<2>(i)]}",
            Truncate, (0, 3), "truncate",
        ),
        (
            "fn narrow(x:u3)->u2{truncate<2>(x)} "
            "fn low<IW=2>(x:u3)->uint<IW>{narrow(x)} "
            "module A{in v:vec<4,u8> in i:u3 out y:u8 y=v[low<2>(i)]}",
            Truncate, (0, 3), "truncate",
        ),
    ),
)
def test_runtime_reads_accept_safe_typed_expression_provenance(
    source: str, index_kind: type, bounds: tuple[int, int], provenance: str,
) -> None:
    module = _module(source)
    access = module.assignments[0].expression
    assert isinstance(access, RuntimeIndex)
    assert isinstance(access.index, index_kind)
    assert (access.index_range.minimum, access.index_range.maximum) == bounds
    assert access.index_range.provenance == provenance


def test_immutable_local_retains_range_and_semantic_expression_identity() -> None:
    module = _module(
        "module A{clock c reset r in v:vec<4,u8> out y:u8 "
        "reg counter:u3=0 slot=truncate<2>(counter) "
        "counter<-truncate<3>(counter+1) y=v[slot]}"
    )
    local = module.locals[0]
    access = module.assignments[0].expression
    assert local.value_range == ValueRange(0, 3, "truncate")
    assert local.semantic_identity == expression_semantic_identity(local.expression)
    assert isinstance(access, RuntimeIndex)
    assert access.index == local.expression

    renamed = _module(
        "module A{clock c reset r in v:vec<4,u8> out y:u8 "
        "reg counter:u3=0 renamed=truncate<2>(counter) "
        "counter<-truncate<3>(counter+1) y=v[renamed]}"
    )
    assert expression_semantic_identity(access) == expression_semantic_identity(
        renamed.assignments[0].expression
    )


def test_compile_time_specialized_index_becomes_static_index() -> None:
    module = _module(
        "module A<I=2>{in v:vec<4,u8> out y:u8 y=v[I]}"
    )
    assert isinstance(module.assignments[0].expression, VectorIndex)
    assert module.assignments[0].expression.index == 2


@pytest.mark.parametrize(
    ("source", "diagnostic"),
    (
        (
            "module A{in v:vec<4,u8> in i:u3 out y:u8 y=v[i]}",
            r"runtime index range 0\.\.7.*vector length 4.*required 0\.\.3.*type u3",
        ),
        (
            "module A{in v:vec<6,u8> in i:u3 out y:u8 y=v[i]}",
            r"runtime index range 0\.\.7.*vector length 6.*required 0\.\.5.*type u3",
        ),
        (
            "module A{in v:vec<4,u8> in i:s2 out y:u8 y=v[i]}",
            r"unsigned integral expression.*s2",
        ),
        (
            "module A{in v:vec<4,u8> in i:u2 out y:u8 y=v[i+1]}",
            r"runtime index range 1\.\.4.*vector length 4",
        ),
        (
            "module A{in v:vec<3,u8> in i:u2 out y:u8 y=v[i-1]}",
            r"not provably within vector length 3",
        ),
    ),
)
def test_runtime_indexing_fails_closed_when_range_or_type_is_unsafe(
    source: str, diagnostic: str,
) -> None:
    with pytest.raises(SemanticError, match=diagnostic):
        _module(source)


def test_runtime_index_canonical_round_trip_preserves_expression_and_range() -> None:
    module = _module(
        "module A{in v:vec<8,u8> in i:u2 out y:u8 y=v[i+1]}"
    )
    original = module.assignments[0].expression
    restored = restore(lower(module))
    assert restored == module
    access = restored.assignments[0].expression
    assert isinstance(access, RuntimeIndex)
    assert access.index_range == ValueRange(1, 4, "arithmetic")
    assert access.origin == original.origin
    assert access.index.origin == original.index.origin


def test_simulator_evaluates_input_arithmetic_and_ternary_indices() -> None:
    arithmetic = _module(
        "module A{in v:vec<8,u8> in i:u2 out y:u8 y=v[i+1]}"
    )
    ternary = _module(
        "module A{in v:vec<4,u8> in a:u2 in b:u2 in c:bit "
        "out y:u8 y=v[c?a:b]}"
    )
    values = [10, 20, 30, 40, 50, 60, 70, 80]
    for index in range(4):
        assert simulate(arithmetic, v=values, i=index) == {"y": values[index + 1]}
    assert simulate(ternary, v=values[:4], a=1, b=3, c=1) == {"y": 20}
    assert simulate(ternary, v=values[:4], a=1, b=3, c=0) == {"y": 40}


def test_simulator_evaluates_register_and_local_derived_index() -> None:
    module = _module(
        "module A{clock c reset r in v:vec<4,u8> out y:u8 "
        "reg counter:u3=0 slot=truncate<2>(counter) "
        "counter<-truncate<3>(counter+1) y=v[slot]}"
    )
    outputs = simulate_cycles(
        module,
        ({"v": [11, 22, 33, 44]} for _ in range(6)),
        reset=(True, False, False, False, False, False),
    )
    assert [item["y"] for item in outputs] == [11, 11, 22, 33, 44, 11]


@pytest.mark.parametrize("depth", (256, 128, 64, 32, 16, 8, 4, 2))
def test_fft_relevant_specialized_twiddle_index_matrix(depth: int) -> None:
    index_width = int(math.log2(depth))
    counter_width = index_width + 1
    module = _module(
        f"module A{{clock c reset r in table:vec<{depth},u8> out y:u8 "
        f"reg phase:uint<{counter_width}>=0 "
        f"slot=truncate<{index_width}>(phase) "
        f"phase<-truncate<{counter_width}>(phase+1) y=table[slot]}}"
    )
    access = module.assignments[0].expression
    assert isinstance(access, RuntimeIndex)
    assert access.vector_length == depth
    assert access.index_range == ValueRange(0, depth - 1, "truncate")


def test_fft_depth_one_degenerate_twiddle_lookup_is_static() -> None:
    module = _module("module A{in table:vec<1,u8> out y:u8 y=table[0]}")
    assert isinstance(module.assignments[0].expression, VectorIndex)


@pytest.mark.parametrize(("length", "width"), ((2, 1), (4, 2), (8, 3)))
def test_generic_table_gather_retains_element_type_range_through_static_lookup(
    length: int, width: int,
) -> None:
    module = _module(
        "fn gather<type T,N,IW>(values:vec<N,T>, order:vec<N,uint<IW>>) { "
        "generate(i in 0..N) values[order[i]] } "
        f"module A {{ in values:vec<{length},u8> "
        f"in order:vec<{length},uint<{width}>> "
        f"out result:vec<{length},u8> "
        f"result=gather<T=u8,N={length},IW={width}>(values,order) }}"
    )
    definition = module.callable_definitions[0]
    assert isinstance(definition.body, Generate)
    accesses = definition.body.elements
    assert all(isinstance(item, RuntimeIndex) for item in accesses)
    assert all(
        item.index_range == ValueRange(0, length - 1, "static_type")
        for item in accesses
        if isinstance(item, RuntimeIndex)
    )
    assert restore(lower(module)) == module


def test_generic_table_gather_rejects_an_element_type_wider_than_destination() -> None:
    source = (
        "fn gather<type T,N,IW>(values:vec<N,T>, order:vec<N,uint<IW>>) { "
        "generate(i in 0..N) values[order[i]] } "
        "module A { in values:vec<6,u8> in order:vec<6,u3> "
        "out result:vec<6,u8> "
        "result=gather<T=u8,N=6,IW=3>(values,order) }"
    )
    with pytest.raises(
        SemanticError, match=r"runtime index range 0\.\.7.*vector length 6"
    ):
        _module(source)


def test_constant_table_proves_tighter_non_power_of_two_gather_indices() -> None:
    module = _module(
        "module A { in values:vec<6,u8> out result:vec<6,u8> "
        "order:vec<6,u3>=[0,5,1,4,2,3] "
        "result=generate(i in 0..6) values[order[i]] }"
    )
    result = module.assignments[0].expression
    assert isinstance(result, Generate)
    assert [
        item.index_range for item in result.elements
        if isinstance(item, RuntimeIndex)
    ] == [
        ValueRange(value, value, "constant_table")
        for value in (0, 5, 1, 4, 2, 3)
    ]
    assert simulate(module, values=[10, 20, 30, 40, 50, 60]) == {
        "result": [10, 60, 20, 50, 30, 40]
    }
