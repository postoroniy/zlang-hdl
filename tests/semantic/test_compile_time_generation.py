"""Focused coverage for the bounded structural compile-time slice."""

import importlib

import pytest

from zlang.compiler import compile_source
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.semantic import compile_time_real as ct_real
from zlang.ir import expressions as ir_expr
from zlang.ir.callables import expand_callable_calls
from zlang.ir.functional import materialize_functional_region
from zlang.ir.types import BitType, SIntType, UIntType, VecType
from zlang.semantic import SemanticError


semantic_analyze = importlib.import_module("zlang.semantic.analyze")


def _expanded(result, expression):
    return expand_callable_calls(
        expression,
        (*result.ir.functions, *result.ir.callable_definitions),
    )


def test_parameterized_generate_and_range_expression_are_concrete() -> None:
    result = compile_source(
        "module G<N=8,START=2> { "
        "out y:vec<2,u3> "
        "y=generate(i in START..(N / 2)) i }"
    )
    expression = _expanded(result, result.ir.assignments[0].expression)
    assert isinstance(expression.type, VecType)
    assert expression.type.length == 2
    assert expression.start == 2
    assert expression.stop == 4


def test_pure_generic_function_returns_constant_vector() -> None:
    result = compile_source(
        "fn indices<N>() { generate(i in 0..N) i } "
        "module G<N=4> { out y:vec<4,uint<2>> y=indices<N>() }"
    )
    expression = _expanded(result, result.ir.assignments[0].expression)
    assert isinstance(expression.type, VecType)
    assert [item.value for item in expression.elements] == [0, 1, 2, 3]
    assert result.ir.generic_specializations


def test_generate_iterator_is_a_generic_value_specialization_constant() -> None:
    result = compile_source(
        "fn value<K>() -> u3 { K } "
        "module G { out y:vec<4,u3> "
        "y=generate(k in 0..4) value<K=k>() }"
    )
    expression = _expanded(result, result.ir.assignments[0].expression)
    assert [item.value for item in expression.elements] == [0, 1, 2, 3]
    arguments = [item.arguments for item in result.ir.generic_specializations]
    assert sorted(arguments) == [
        (("K", "0"),),
        (("K", "1"),),
        (("K", "2"),),
        (("K", "3"),),
    ]
    identities = [item.identity for item in result.ir.generic_specializations]
    assert identities == sorted(identities)
    assert len(identities) == len(set(identities)) == 4


def test_generate_iterator_specialization_accepts_constant_expressions() -> None:
    result = compile_source(
        "fn value<K>() -> u3 { K } "
        "module G { out y:vec<4,u3> "
        "y=generate(k in 0..4) value<K=k+1>() }"
    )
    expression = _expanded(result, result.ir.assignments[0].expression)
    assert [item.value for item in expression.elements] == [1, 2, 3, 4]


def test_functional_iterator_cannot_shadow_module_parameter() -> None:
    with pytest.raises(SemanticError, match="shadows an existing name"):
        compile_source(
            "module G<N=4> { out y:vec<4,u3> "
            "y=generate(N in 0..4) N }"
        )


def test_runtime_name_is_not_a_generic_value_specialization_constant() -> None:
    with pytest.raises(SemanticError, match="requires an integer"):
        compile_source(
            "fn value<K>() -> u3 { K } "
            "module G { in x:u3 out y:u3 y=value<K=x>() }"
        )


def test_specialization_if_selects_only_the_live_branch() -> None:
    result = compile_source(
        "module G<N=1> { out y:u8 "
        "if N == 1 { y=1 } else { y=1 / 0 } }"
    )
    assert result.ir.assignments[0].expression.value == 1


def test_type_condition_accepts_parameterized_fixed_type_values() -> None:
    result = compile_source(
        "fn choose<type T>(x:T) { "
        "if T == fixed<16,8> { x } else { x } } "
        "module G { in a:fixed<16,8> out y:fixed<16,8> y=choose(a) }"
    )
    assert result.ir.assignments[0].expression.type.width == 16


def test_type_condition_distinguishes_fixed_overflow_policy() -> None:
    result = compile_source(
        "fn choose<type T>(x:T) { "
        "if T == fixed<16,8> { 1 } else { 0 } } "
        "module G { in a:SF_Sat8.8 out y:u1 y=choose(a) }"
    )
    assert _expanded(result, result.ir.assignments[0].expression).value == 0


def test_type_condition_accepts_exact_vector_shape() -> None:
    result = compile_source(
        "fn choose<type T>(x:T) { "
        "if T == vec<4,u8> { 1 } else { 0 } } "
        "module G { in a:vec<4,u8> out y:u1 y=choose(a) }"
    )
    assert _expanded(result, result.ir.assignments[0].expression).value == 1


def test_type_condition_accepts_nominal_generic_struct_specialization() -> None:
    result = compile_source(
        "struct Box<type T>{ value:T } "
        "fn choose<type T>(x:T) { if T == Box<u8> { 1 } else { 0 } } "
        "module G { in a:Box<u8> out y:u1 y=choose(a) }"
    )
    assert _expanded(result, result.ir.assignments[0].expression).value == 1


def test_runtime_if_condition_is_rejected() -> None:
    with pytest.raises(SemanticError, match="compile-time condition"):
        compile_source(
            "module G { in x:bit out y:u8 "
            "y=if x { 1 } else { 0 } }"
        )


def test_compile_time_if_cannot_change_public_abi() -> None:
    with pytest.raises(SemanticError, match="public ABI"):
        compile_source(
            "module G<N=1> { if N == 1 { out x:u8 } out y:u8 y=0 }"
        )


def test_structural_intrinsics_have_exact_results() -> None:
    result = compile_source(
        "module G { in x:vec<4,u8> out y:u3 "
        "y=length(x) }"
    )
    assert result.ir.assignments[0].expression.value == 4
    assert result.ir.assignments[0].expression.type == UIntType(3)

    for intrinsic, expected in (
        ("floor_log2(8)", 3),
        ("ceil_log2(9)", 4),
    ):
        result = compile_source(f"module G {{ out y:u3 y={intrinsic} }}")
        assert result.ir.assignments[0].expression.value == expected

    result = compile_source("module G { out y:bit y=is_power_of_two(8) }")
    assert result.ir.assignments[0].expression.value == 1
    assert result.ir.assignments[0].expression.type == BitType()


def test_over_budget_parameterized_range_fails_without_truncation() -> None:
    with pytest.raises(SemanticError, match="generation limit"):
        compile_source(
            "module G<N=4097> { out y:vec<4097,u1> "
            "y=generate(i in 0..N) 0 }"
        )


def test_cached_specialization_replays_logical_generation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantic_analyze, "_TOTAL_GENERATED_LIMIT", 7)
    with pytest.raises(SemanticError, match="generation exceeds 7 elements"):
        analyze(
            parse(
                "fn indices<N>() { generate(i in 0..N) i } "
                "module G { out y:vec<8,u2> "
                "y=concat(indices<4>(),indices<4>()) }"
            )
        )


def test_real_intrinsics_quantize_to_fixed_constants() -> None:
    result = analyze(parse(
        "module G { "
        "out c:SF_Sat2.14 = quantize<SF_Sat2.14>(cos(pi() / 4)){round nearest_even overflow saturate} "
        "out s:SF_Sat2.14 = quantize<SF_Sat2.14>(sin((-1 * pi()) / 4)){round nearest_even overflow saturate} "
        "out qn:SF_Sat2.14 = quantize<SF_Sat2.14>(sin((-3 * pi()) / 4)){round nearest_even overflow saturate} "
        "out qc:SF_Sat2.14 = quantize<SF_Sat2.14>(cos((-3 * pi()) / 4)){round nearest_even overflow saturate} "
        "out p:SF4.12 = quantize<SF4.12>(pi()){round nearest_even overflow wrap} "
        "}"
    ))
    assignments = {item.target.name: item.expression for item in result.assignments}
    assert isinstance(assignments["c"], ir_expr.Constant)
    assert isinstance(assignments["s"], ir_expr.Constant)
    assert isinstance(assignments["p"], ir_expr.Constant)
    assert assignments["c"].value == 11585
    assert assignments["s"].value == -11585
    assert assignments["qn"].value == -11585
    assert assignments["qc"].value == -11585
    assert assignments["p"].value == 12868
    assert "sin" not in repr(result)
    assert "cos" not in repr(result)


def test_real_cardinals_and_integral_logs_escape_only_as_exact_integers() -> None:
    result = analyze(parse(
        "module G { "
        "out a:u2 = sin(pi() / 2) "
        "out b:s2 = cos(pi()) "
        "out c:u3 = log2(8) "
        "out d:u3 = log(2,8) "
        "out e:u1 "
        "if log2(8) == 3 { e = 1 } else { e = 0 } "
        "}"
    ))
    assignments = {item.target.name: item.expression for item in result.assignments}
    assert assignments["a"].value == 1
    assert assignments["b"].value == -1
    assert assignments["c"].value == 3
    assert assignments["d"].value == 3
    assert assignments["e"].value == 1


def test_negative_integral_real_intrinsic_uses_minimum_signed_width() -> None:
    result = analyze(parse("module G { value=cos(pi()) out y:s1 y=value }"))
    assert result.locals[0].expression == ir_expr.Constant(
        -1,
        SIntType(1),
        origin=result.locals[0].expression.origin,
    )


def test_nonintegral_real_cannot_escape_without_quantize() -> None:
    with pytest.raises(SemanticError, match="use explicit quantize"):
        compile_source("module G { out y:u8 y=pi() }")
    with pytest.raises(SemanticError, match="use explicit quantize"):
        compile_source("module G { out y:SF2.14 y=sin(pi() / 2) }")
    with pytest.raises(SemanticError, match="non-integral"):
        compile_source("module G { out y:u8 y=log2(3) }")


def test_real_intrinsic_domain_and_runtime_operand_diagnostics() -> None:
    for source, pattern in (
        ("module G { out y:SF2.14 y=quantize<SF2.14>(log2(0)){round floor overflow wrap} }", "positive"),
        ("module G { out y:SF2.14 y=quantize<SF2.14>(log(1,8)){round floor overflow wrap} }", "base"),
        ("module G { out y:SF2.14 y=quantize<SF2.14>(log(2,0)){round floor overflow wrap} }", "positive"),
        ("module G { in x:SF2.14 out y:SF2.14 y=quantize<SF2.14>(sin(x)){round nearest_even overflow wrap} }", "runtime value 'x'"),
    ):
        with pytest.raises(SemanticError, match=pattern):
            compile_source(source)


def test_real_evaluator_identity_is_stable_and_versioned() -> None:
    first = compile_source(
        "module G { out y:SF2.14 y=quantize<SF2.14>(cos(pi()/3)){round nearest_even overflow wrap} }"
    ).ir.assignments[0].expression
    second = compile_source(
        "module G { out y:SF2.14 y=quantize<SF2.14>(cos(pi()/3)){round nearest_even overflow wrap} }"
    ).ir.assignments[0].expression
    assert isinstance(first, ir_expr.Constant)
    assert isinstance(second, ir_expr.Constant)
    assert first.value == second.value == 8192
    schema, dependency, runtime = ct_real.dependency_identity()
    assert schema == "zlang-ct-real-v2"
    assert dependency == "stdlib-decimal"
    assert runtime[0] == "python"


def test_periodic_exact_pi_angles_share_one_compilation_local_quantization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ct_real.quantize_to_raw
    evaluated: list[tuple[object, ...]] = []

    def observed(value, **arguments):
        evaluated.append(value.identity)
        return original(value, **arguments)

    monkeypatch.setattr(ct_real, "quantize_to_raw", observed)
    source = (
        "module G { "
        "out c0:SF2.14 = quantize<SF2.14>(cos(pi()/3))"
        "{round nearest_even overflow wrap} "
        "out c1:SF2.14 = quantize<SF2.14>(cos((7*pi())/3))"
        "{round nearest_even overflow wrap} "
        "out s0:SF2.14 = quantize<SF2.14>(sin(pi()/3))"
        "{round nearest_even overflow wrap} "
        "out s1:SF2.14 = quantize<SF2.14>(sin((-5*pi())/3))"
        "{round nearest_even overflow wrap} "
        "}"
    )

    first = compile_source(source, include_clash=False).ir
    assert [item.expression.value for item in first.assignments] == [
        8192,
        8192,
        14189,
        14189,
    ]
    assert evaluated == [
        ("cos-pi-mod-2", 1, 3),
        ("sin-pi-mod-2", 1, 3),
    ]

    # A second top-level compilation owns a fresh cache. Nothing survives in a
    # process-global mapping even though decimal/pi implementation caches are
    # bounded host optimizations.
    compile_source(source, include_clash=False)
    assert len(evaluated) == 4


def test_periodic_quantization_cache_replays_logical_operation_cost() -> None:
    first_source = (
        "module G { out y:SF2.14 = quantize<SF2.14>(cos(pi()/3))"
        "{round nearest_even overflow wrap} }"
    )
    periodic_source = (
        "module G { out y:SF2.14 = quantize<SF2.14>(cos((7*pi())/3))"
        "{round nearest_even overflow wrap} }"
    )
    combined_source = (
        "module G { "
        "out a:SF2.14 = quantize<SF2.14>(cos(pi()/3))"
        "{round nearest_even overflow wrap} "
        "out b:SF2.14 = quantize<SF2.14>(cos((7*pi())/3))"
        "{round nearest_even overflow wrap} "
        "}"
    )

    def operations(source: str) -> int:
        budget = semantic_analyze._CompileTimeBudget()
        semantic_analyze.analyze(parse(source), compile_time_budget=budget)
        return budget.operations

    assert operations(combined_source) == (
        operations(first_source) + operations(periodic_source)
    )


def test_real_intrinsic_precision_retries_use_compile_time_operation_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(semantic_analyze, "_COMPILE_TIME_OPERATION_LIMIT", 4)
    with pytest.raises(SemanticError, match="compile-time evaluator exceeded"):
        compile_source(
            "module G { out y:SF2.14 "
            "y=quantize<SF2.14>(cos(pi() / 4)){round nearest_even overflow wrap} }"
        )


def test_generated_instance_bindings_have_distinct_physical_identity() -> None:
    source = (
        "module Child { in x:u8 out y:u8 y=x } "
        "module Top<N=2> { inst c[N]:Child "
        "generate(i in 0..N) { c[i].x = i } }"
    )
    module = analyze(parse(source))
    assert [item.instance for item in module.instance_bindings] == ["c[0]", "c[1]"]
    assert len({item.instance_identity for item in module.elaborated_instances}) == 2
    assert len({item.specialization_identity for item in module.elaborated_instances}) == 1


def test_compile_time_branch_is_spliced_into_source_order() -> None:
    result = analyze(parse(
        "module G<N=1> { "
        "a:u8 = 1 "
        "if N == 1 { b:u8 = a } else { b:u8 = 0 } "
        "c:u8 = b out y:u8 y=c }"
    ))
    assert [binding.name for binding in result.locals] == ["a", "b", "c"]
    assert result.assignments[0].expression.type == UIntType(8)
    assert result.assignments[0].expression.value == 1
    assert "CompileTimeIf" not in repr(result)


def test_nested_compile_time_branches_preserve_order() -> None:
    result = analyze(parse(
        "module G<N=1> { "
        "a:u8 = 1 "
        "if N == 1 { b:u8 = a if N == 1 { c:u8 = b } else { c:u8 = 0 } } "
        "else { b:u8 = 0 c:u8 = b } "
        "out y:u8 y=c }"
    ))
    assert [binding.name for binding in result.locals] == ["a", "b", "c"]


def test_generation_binder_shadowing_is_rejected_before_path_substitution() -> None:
    with pytest.raises(SemanticError, match="shadows an existing visible symbol"):
        compile_source(
            "module Child { in x:u8 out y:u8 y=x } "
            "module Top<N=1> { inst i:Child "
            "generate(i in 0..N) { i.x = 0 } }"
        )


def test_generated_hierarchical_path_replaces_only_the_index_segment() -> None:
    module = analyze(
        parse(
            "module Child { in x:u8 out y:u8 y=x } "
            "module Top<N=2> { inst c[N]:Child "
            "generate(j in 0..N) { c[j].x = j } }"
        )
    )
    assert [(binding.instance, binding.port, binding.expression.value) for binding in module.instance_bindings] == [
        ("c[0]", "x", 0),
        ("c[1]", "x", 1),
    ]


def test_literal_and_parameterized_512_ranges_have_same_concrete_shape() -> None:
    literal = compile_source(
        "module G { out y:vec<512,u9> y=generate(i in 0..512) i }"
    )
    parameterized = compile_source(
        "module G<N=512> { out y:vec<N,u9> y=generate(i in 0..N) i }"
    )
    left = literal.ir.assignments[0].expression
    right = parameterized.ir.assignments[0].expression
    assert left.type == right.type == VecType(512, UIntType(9))
    # Large bounded ranges are retained as compact functional regions.  The
    # compatibility assertion is about their concrete logical contents, not
    # the former eager storage representation.
    assert isinstance(left, ir_expr.FunctionalRegion)
    assert isinstance(right, ir_expr.FunctionalRegion)
    assert [item.value for item in materialize_functional_region(left)] == [
        item.value for item in materialize_functional_region(right)
    ]


@pytest.mark.parametrize("source", [
    "module G { out y:vec<4096,u1> y=generate(i in 0..4096) 0 }",
    "module G<N=4096> { out y:vec<N,u1> y=generate(i in 0..N) 0 }",
])
def test_4096_element_functional_range_is_accepted(source: str) -> None:
    result = compile_source(source)
    assert result.ir.assignments[0].expression.type == VecType(4096, UIntType(1))


@pytest.mark.parametrize("source", [
    "module G { out y:vec<4097,u1> y=generate(i in 0..4097) 0 }",
    "module G<N=4097> { out y:vec<N,u1> y=generate(i in 0..N) 0 }",
])
def test_4097_element_functional_range_is_rejected_for_both_spellings(source: str) -> None:
    with pytest.raises(SemanticError, match="limit is 4096"):
        compile_source(source)


def test_empty_functional_range_is_rejected() -> None:
    with pytest.raises(SemanticError, match="functional range 0..0 is empty"):
        compile_source("module G { out y:vec<1,u1> y=generate(i in 0..0) 0 }")


def test_empty_structural_range_is_a_noop() -> None:
    result = compile_source(
        "module G { in a:u8 out y:u8 "
        "generate(i in 0..0) { connect a -> y } y=0 }"
    )
    assert result.ir.connections == ()


def test_reversed_structural_range_is_rejected() -> None:
    with pytest.raises(SemanticError, match="generated range 1..0 is reversed"):
        compile_source(
            "module G { in a:u8 out y:u8 "
            "generate(i in 1..0) { connect a -> y } y=0 }"
        )


def test_shared_generation_budget_covers_multiple_functional_ranges() -> None:
    values = " ".join(
        f"v{index}:vec<4096,u1>=generate(j{index} in 0..4096) 0"
        for index in range(17)
    )
    with pytest.raises(SemanticError, match="exceeds 65536 elements"):
        compile_source(f"module G {{ {values} out y:u1 y=0 }}")


def test_compile_time_function_call_depth_is_bounded() -> None:
    functions = " ".join(
        f"fn f{index}<type T>(x:T)->T {{ f{index + 1}(x) }}"
        for index in range(64)
    )
    source = (
        functions
        + " fn f64<type T>(x:T)->T { x } module G { out y:u1 y=f0(0) }"
    )
    with pytest.raises(SemanticError, match="nesting exceeds 64 calls"):
        compile_source(source)
