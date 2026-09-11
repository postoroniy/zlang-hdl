from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.callables import expand_callable_calls
from zlang.opt import lower, restore
from zlang.reductions import expand_reduction
from zlang.semantic import SemanticError
from zlang.simulate import simulate


def test_complex_sum_retains_exact_overload_expansion_and_type() -> None:
    result = compile_source(
        "import std.math.complex "
        "module Top{in x:vec<8,Complex<fixed<35,30>>> "
        "out y:Complex<fixed<38,30>> y=sum(x)}",
    )
    reduction = result.ir.assignments[0].expression
    assert isinstance(reduction, expr.Reduce)
    assert reduction.type.name == "Complex<fixed<38,30>>"
    assert isinstance(reduction.expanded, expr.Call)
    expanded = expand_callable_calls(
        reduction.expanded,
        (*result.ir.functions, *result.ir.callable_definitions),
    )
    assert isinstance(expanded, expr.StructConstruct)
    operator_definitions = tuple(
        definition
        for definition in result.ir.callable_definitions
        if definition.metadata is not None
        and definition.metadata.source_name == "operator+"
    )
    # Eight leaves have three exact-width tree levels. Each monomorphic
    # overload is typed once even though it is used multiple times per level.
    assert len(operator_definitions) == 3
    assert {str(item.return_type) for item in operator_definitions} == {
        "Complex<fixed<36,30>>",
        "Complex<fixed<37,30>>",
        "Complex<fixed<38,30>>",
    }
    assert restore(lower(result.ir)) == result.ir
    # M32 must not reassociate an overload-resolved nominal tree.
    assert expand_reduction(reduction) == ()


def test_complex_sum_stdlib_spelling_matches_language_sum() -> None:
    direct_module = compile_source(
        "import std.math.complex "
        "module Top{in x:vec<4,Complex<s8>> out y:Complex<s10> y=sum(x)}",
    ).ir
    helper_module = compile_source(
        "import std.math.complex "
        "module Top{in x:vec<4,Complex<s8>> "
        "out y:Complex<s10> y=complex_sum(x)}",
    ).ir
    expanded_helper = expand_callable_calls(
        helper_module.assignments[0].expression,
        (*helper_module.functions, *helper_module.callable_definitions),
    )
    expanded_direct = expand_callable_calls(
        direct_module.assignments[0].expression,
        (*direct_module.functions, *direct_module.callable_definitions),
    )
    assert expanded_helper == expanded_direct


def test_complex_dot_uses_same_exact_nominal_reduction() -> None:
    result = compile_source(
        "import std.math.complex "
        "module Top{"
        "in a:vec<8,Complex<fixed<18,16>>> "
        "in b:vec<8,Complex<fixed<16,14>>> "
        "out y:Complex<fixed<38,30>> y=dot(a,b)}",
    )
    reduction = result.ir.assignments[0].expression
    assert isinstance(reduction, expr.Reduce)
    assert isinstance(reduction.collection, expr.Dot)
    assert reduction.type.name == "Complex<fixed<38,30>>"
    assert reduction.expanded is not None


def test_nominal_sum_uses_owner_operator_not_struct_field_magic() -> None:
    result = compile_source(
        "struct Pair<type T>{left:T right:T} "
        "operator +<type A,type B>(a:Pair<A>,b:Pair<B>){"
        "Pair{left=a.left right=b.right}} "
        "module Top{in x:vec<4,Pair<u8>> out y:Pair<u8> y=sum(x)}",
    )
    reduction = result.ir.assignments[0].expression
    assert isinstance(reduction, expr.Reduce)
    assert reduction.type.name == "Pair<u8>"
    assert isinstance(reduction.expanded, expr.Call)
    expanded = expand_callable_calls(
        reduction.expanded,
        (*result.ir.functions, *result.ir.callable_definitions),
    )
    assert isinstance(expanded, expr.StructConstruct)


def test_nominal_sum_rejects_missing_or_non_closed_exact_overload() -> None:
    with pytest.raises(SemanticError, match="no exact overload"):
        compile_source(
            "struct Pair{left:u8 right:u8} "
            "module Top{in x:vec<2,Pair> out y:Pair y=sum(x)}",
        )
    with pytest.raises(SemanticError, match="balanced-tree operator"):
        compile_source(
            "struct Pair<type T>{left:T right:T} "
            "operator +(a:Pair<u8>,b:Pair<u8>){"
            "Pair{left=a.left+b.left right=a.right+b.right}} "
            "module Top{in x:vec<4,Pair<u8>> out y:Pair<u10> y=sum(x)}",
        )


def test_nominal_reduce_support_is_additive_only() -> None:
    with pytest.raises(SemanticError, match="only additive sum"):
        compile_source(
            "import std.math.complex "
            "module Top{in x:vec<2,Complex<u8>> "
            "out y:Complex<u16> y=reduce(*,x)}",
        )


@pytest.mark.parametrize(
    ("length", "root_width", "levels"),
    ((32, 40, 5), (33, 41, 6)),
)
def test_compact_nominal_sum_preserves_odd_even_exact_widening(
    length: int,
    root_width: int,
    levels: int,
) -> None:
    result = compile_source(
        "import std.math.complex "
        f"module Top{{in x:vec<{length},Complex<fixed<35,30>>> "
        f"out y:Complex<fixed<{root_width},30>> "
        f"y=sum(generate(i in 0..{length}) x[i])}}",
    )
    reduction = result.ir.assignments[0].expression
    assert isinstance(reduction, expr.Reduce)
    assert isinstance(reduction.collection, expr.FunctionalRegion)
    assert reduction.expanded is None
    assert reduction.plan is not None
    assert len(reduction.plan.levels) == levels
    assert reduction.type.name == f"Complex<fixed<{root_width},30>>"
    assert expand_reduction(reduction) == ()


def test_compact_nominal_sum_preserves_non_associative_midpoint_order() -> None:
    result = compile_source(
        "struct Trace{v:u8} "
        "operator +(a:Trace,b:Trace){Trace{v=a.v-b.v}} "
        "module Top{in x:vec<33,Trace> out y:Trace "
        "y=sum(generate(i in 0..33) x[i])}",
    )
    reduction = result.ir.assignments[0].expression
    assert isinstance(reduction, expr.Reduce)
    assert reduction.plan is not None

    def midpoint(values: tuple[int, ...]) -> int:
        if len(values) == 1:
            return values[0]
        middle = len(values) // 2
        return (midpoint(values[:middle]) - midpoint(values[middle:])) & 0xFF

    values = tuple(range(33))
    assert simulate(result.ir, x=[{"v": value} for value in values]) == {
        "y": {"v": midpoint(values)}
    }


def test_compact_nominal_sum_reports_missing_intermediate_overload() -> None:
    with pytest.raises(SemanticError, match="balanced-tree operator"):
        compile_source(
            "struct Pair<type T>{left:T right:T} "
            "operator +(a:Pair<u8>,b:Pair<u8>){"
            "Pair{left=a.left+b.left right=a.right+b.right}} "
            "module Top{in x:vec<32,Pair<u8>> out y:Pair<u13> "
            "y=sum(generate(i in 0..32) x[i])}",
        )


def test_functional_binder_identity_is_source_relocation_insensitive() -> None:
    source = (
        "module Top{in x:vec<32,u8> out y:vec<32,u8> "
        "y=generate(i in 0..32) x[i]}"
    )
    first = compile_source(
        source,
        source_unit="first/location.zhl",
    )
    second = compile_source(
        source,
        source_unit="second/location.zhl",
    )
    reformatted = compile_source(
        """
        module Top {
            in x : vec<32,u8>

            out y : vec<32,u8>
            // Formatting and provenance are not functional binder identity.
            y = generate(i in 0..32) x[i]
        }
        """,
        source_unit="third/location.zhl",
    )
    identities = tuple(
        result.ir.assignments[0].expression.binder.identity
        for result in (first, second, reformatted)
    )
    assert identities[0] == identities[1] == identities[2]
    assert first.high_level_ir_identity == second.high_level_ir_identity
    assert first.high_level_ir_identity == reformatted.high_level_ir_identity


def test_distinct_functional_declarations_have_distinct_binder_identities() -> None:
    module = compile_source(
        "module Top{in x:vec<32,u8> out a:vec<32,u8> out b:vec<32,u8> "
        "a=generate(i in 0..32)x[i] b=generate(i in 0..32)x[i]}",
    ).ir
    regions = tuple(assignment.expression for assignment in module.assignments)
    assert all(isinstance(region, expr.FunctionalRegion) for region in regions)
    assert regions[0].binder.identity != regions[1].binder.identity


def test_compaction_never_retains_an_erased_local_reference() -> None:
    module = compile_source(
        "module Top{clock clk reset rst reg state:bits<32>=0 "
        "out y:vec<32,bit> state_bits:vec<32,bit>="
        "bitcast<vec<32,bit>>(state) "
        "y=generate(i in 0..32) state_bits[i]}",
    ).ir
    generated = module.assignments[0].expression
    assert isinstance(generated, expr.Generate)
    assert module.locals == ()
    assert all(
        not isinstance(item.expression, expr.InputRef)
        or item.expression.name != "state_bits"
        for item in generated.elements
        if isinstance(item, expr.VectorIndex)
    )
