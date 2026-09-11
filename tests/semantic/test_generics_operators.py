from __future__ import annotations

from dataclasses import fields, is_dataclass

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.callables import CallableKind, expand_callable_calls
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


PAIR = "struct Pair<type T>{left:T right:T} "


def test_generic_struct_constructor_and_nested_inference() -> None:
    result = compile_source(
        PAIR
        + "struct Box<type T>{value:T} "
        + "module Top{in a:u8 in b:u8 out y:Box<Pair<u8>> "
        + "y=Box{value=Pair{left=a right=b}}}"
    )
    assert str(result.ir.assignments[0].expression.type) == "Box<Pair<u8>>"


def test_generic_function_inference_explicit_specialization_and_inferred_return() -> None:
    inferred = compile_source(
        "fn duplicate<type T>(x:T){x+x} module Top{in x:u8 out y:u9 y=duplicate(x)}"
    )
    explicit = compile_source(
        "fn duplicate<type T>(x:T){x+x} module Top{in x:u8 out y:u9 y=duplicate<u8>(x)}"
    )
    assert isinstance(inferred.ir.assignments[0].expression, expr.Call)
    expanded = expand_callable_calls(
        inferred.ir.assignments[0].expression,
        (*inferred.ir.functions, *inferred.ir.callable_definitions),
    )
    assert isinstance(expanded, expr.Add)
    assert inferred.ir.generic_specializations[0].identity == explicit.ir.generic_specializations[0].identity


def test_callable_immutable_bindings_lower_to_existing_expression_ir() -> None:
    concise = compile_source(
        "fn accumulate<type T>(a:T,b:T){"
        "exact=a+b widened=exact+extend<9>(b) widened} "
        "module Top{in a:u8 in b:u8 out y:u10 y=accumulate(a,b)}",
    )
    nested = compile_source(
        "fn accumulate<type T>(a:T,b:T){(a+b)+extend<9>(b)} "
        "module Top{in a:u8 in b:u8 out y:u10 y=accumulate(a,b)}",
    )
    concise_body = concise.ir.callable_definitions[0].body
    nested_body = nested.ir.callable_definitions[0].body
    assert isinstance(concise_body, expr.Add)
    assert concise_body == nested_body
    assert not any(
        field.name == "bindings"
        for node in (concise_body,)
        for field in fields(node)
    )


def test_callable_bindings_are_sequential_immutable_and_inferred() -> None:
    with pytest.raises(SemanticError, match="unknown input 'later'"):
        compile_source(
            "fn bad<type T>(x:T){first=later later=x first} "
            "module Top{in x:u8 out y:u8 y=bad(x)}",
        )
    with pytest.raises(SemanticError, match="duplicate callable binding 'x'"):
        compile_source(
            "fn bad<type T>(x:T){x=x x} "
            "module Top{in x:u8 out y:u8 y=bad(x)}",
        )


def test_value_parameter_inference_and_named_explicit_specialization() -> None:
    inferred = compile_source(
        "fn first<type T,N>(x:vec<N,T>){x[0]} "
        "module Top{in x:vec<8,u16> out y:u16 y=first(x)}"
    )
    explicit = compile_source(
        "fn first<type T,N>(x:vec<N,T>){x[0]} "
        "module Top{in x:vec<8,u16> out y:u16 y=first<T=u16,N=8>(x)}"
    )
    assert inferred.ir.generic_specializations[0].identity == explicit.ir.generic_specializations[0].identity


def test_complex_multiply_is_exact_and_visible_as_concrete_arithmetic() -> None:
    result = compile_source(
        "import std.math.complex "
        "module Top{in a:Complex<fixed<18,16>> in b:Complex<fixed<16,14>> "
        "out y:Complex<fixed<35,30>> y=a*b}"
    )
    specialization = next(item for item in result.ir.generic_specializations if item.name == "operator*")
    assert str(specialization.return_type) == "Complex<fixed<35,30>>"
    root = result.ir.assignments[0].expression
    assert isinstance(root, expr.Call)
    expanded = expand_callable_calls(
        root,
        (*result.ir.functions, *result.ir.callable_definitions),
    )
    assert isinstance(expanded, expr.StructConstruct)
    assert any(isinstance(value, expr.Binary) for _, value in expanded.fields)
    assert restore(lower(result.ir)) == result.ir


def test_complex_add_rejects_fractional_scale_mismatch() -> None:
    with pytest.raises(SemanticError, match="identical fractional widths"):
        compile_source(
            "import std.math.complex "
            "module Top{in a:Complex<fixed<18,16>> in b:Complex<fixed<16,14>> "
            "out y:Complex<fixed<19,16>> y=a+b}"
        )


def test_concrete_operator_beats_generic_and_equal_rank_is_ambiguous() -> None:
    source = (
        PAIR
        + "operator +<type T>(a:Pair<T>,b:Pair<T>){Pair{left=a.left+b.left right=a.right+b.right}} "
        + "operator +(a:Pair<u8>,b:Pair<u8>){Pair{left=a.left right=b.right}} "
        + "module Top{in a:Pair<u8> in b:Pair<u8> out y:Pair<u8> y=a+b}"
    )
    assert str(compile_source(source).ir.assignments[0].expression.type) == "Pair<u8>"
    with pytest.raises(SemanticError, match="ambiguous operator"):
        compile_source(
            PAIR
            + "operator +(a:Pair<u8>,b:Pair<u8>){a} "
            + "operator +(a:Pair<u8>,b:Pair<u8>){b} "
            + "module Top{in a:Pair<u8> in b:Pair<u8> out y:Pair<u8> y=a+b}"
        )


def test_builtin_protection_and_nominal_owner_coherence() -> None:
    with pytest.raises(SemanticError, match="built-in scalar and fixed"):
        compile_source(
            "operator +<type T>(a:T,b:T){a+b} module Top{in a:u8 in b:u8 out y:u9 y=a+b}"
        )
    with pytest.raises(SemanticError, match="nominal-owner coherence"):
        compile_source(
            "import std.math.complex "
            "operator +(a:Complex<u8>,b:Complex<u8>){a} "
            "module Top{in a:Complex<u8> in b:Complex<u8> out y:Complex<u8> y=a+b}"
        )


def test_recursive_operator_specialization_is_rejected() -> None:
    with pytest.raises(SemanticError, match="recursive generic specialization cycle"):
        compile_source(
            PAIR
            + "operator -<type T>(x:Pair<T>){-x} "
            + "module Top{in x:Pair<u8> out y:Pair<u8> y=-x}"
        )


def test_recursive_generic_function_specialization_is_rejected() -> None:
    with pytest.raises(SemanticError, match="recursive generic specialization cycle"):
        compile_source(
            "fn recurse<type T>(x:T)->T{recurse(x)} "
            "module Top{in x:u8 out y:u8 y=recurse(x)}",
        )


def test_generic_declared_return_error_has_call_and_declaration_origins() -> None:
    source = """fn bad<type T>(x:T) -> u8 { x + x }
module Top {
    in x : u8
    out y : u8
    y = bad(x)
}
"""
    with pytest.raises(SemanticError) as captured:
        compile_source(
            source,
            source_unit="generic-return.zhl",
        )

    error = captured.value
    assert str(error) == "'bad' returns u9, expected u8"
    assert error.primary is not None
    assert error.primary.source_unit == "generic-return.zhl"
    assert error.primary.construct == "call bad"
    assert error.primary.span.start_line == 5
    assert error.notes == (
        "callable declared at generic-return.zhl:"
        "1:1-1:36:function bad declaration",
    )


def test_operator_inference_error_has_call_and_declaration_origins() -> None:
    source = """struct Pair<type T> { value : T }
operator +<type T>(left:Pair<T>, right:Pair<T>) -> Pair<T> { left }
module Top {
    in a : Pair<u8>
    in b : Pair<u16>
    out y : Pair<u8>
    y = a + b
}
"""
    with pytest.raises(SemanticError) as captured:
        compile_source(
            source,
            source_unit="operator-inference.zhl",
        )

    error = captured.value
    assert str(error) == (
        "no exact overload for operator '+' with (Pair<u8>, Pair<u16>); "
        "candidates rejected: conflicting inference for type 'T': u8 and u16"
    )
    assert error.primary is not None
    assert error.primary.source_unit == "operator-inference.zhl"
    assert error.primary.construct == "operator +"
    assert error.primary.span.start_line == 7
    assert error.notes == (
        "callable declared at operator-inference.zhl:"
        "2:1-2:68:operator + declaration",
    )


def test_exact_generic_specialization_is_typed_once_and_calls_keep_origins() -> None:
    module = analyze(
        parse(
            "fn identity<type T>(x:T)->T{x} "
            "module Top { in a:u8 in b:u8 out x:u8 out y:u8 "
            "x=identity(a) y=identity(b) }"
        )
    )
    assert len(module.callable_definitions) == 1
    assert len(module.generic_specializations) == 1
    definition = module.callable_definitions[0]
    assert definition.metadata is not None
    assert definition.metadata.kind is CallableKind.FUNCTION
    assert definition.metadata.source_name == "identity"
    assert definition.metadata.arguments == (("T", "u8"),)
    assert isinstance(definition.body, expr.ParameterRef)

    calls = tuple(assignment.expression for assignment in module.assignments)
    assert all(isinstance(call, expr.Call) for call in calls)
    assert {call.callee_identity for call in calls} == {definition.callee_identity}
    assert {call.function for call in calls} == {definition.name}
    assert calls[0].origin is not None and calls[1].origin is not None
    assert calls[0].origin.construct == calls[1].origin.construct == "call identity"
    assert calls[0].origin.span != calls[1].origin.span
    assert definition.body.origin is not None
    assert definition.body.origin.construct == "name x"


def test_type_and_value_specializations_are_distinct_and_published_sorted() -> None:
    module = analyze(
        parse(
            "fn tag<type T,K>(x:T)->T{x} "
            "module Top { in a:u8 in b:u16 out x:u8 out y:u8 out z:u16 "
            "x=tag<T=u8,K=1>(a) y=tag<T=u8,K=2>(a) "
            "z=tag<T=u16,K=1>(b) }"
        )
    )
    assert len(module.callable_definitions) == 3
    identities = tuple(item.callee_identity for item in module.callable_definitions)
    assert identities == tuple(sorted(identities))
    assert {
        definition.metadata.arguments
        for definition in module.callable_definitions
        if definition.metadata is not None
    } == {
        (("T", "u8"), ("K", "1")),
        (("T", "u8"), ("K", "2")),
        (("T", "u16"), ("K", "1")),
    }
    assert {
        assignment.expression.callee_identity for assignment in module.assignments
    } == set(identities)


def test_callable_publication_is_independent_of_call_site_order() -> None:
    prefix = "fn tag<type T,K>(x:T)->T{x} "
    first = analyze(
        parse(
            prefix
            + "module Top { in a:u8 out x:u8 out y:u8 "
            "x=tag<K=1>(a) y=tag<K=2>(a) }"
        )
    )
    second = analyze(
        parse(
            prefix
            + "module Top { in a:u8 out x:u8 out y:u8 "
            "y=tag<K=2>(a) x=tag<K=1>(a) }"
        )
    )
    first_definitions = tuple(
        (item.callee_identity, item.name, item.metadata, item.body)
        for item in first.callable_definitions
    )
    second_definitions = tuple(
        (item.callee_identity, item.name, item.metadata, item.body)
        for item in second.callable_definitions
    )
    assert first_definitions == second_definitions


def test_unary_nominal_operator_and_deep_explicit_type_argument() -> None:
    explicit = compile_source(
        PAIR
        + "struct Box<type T>{value:T} "
        + "fn identity<type T>(x:T)->T{x} "
        + "module Top{in x:Box<Pair<s8>> out y:Box<Pair<s8>> "
        + "y=identity<Box<Pair<s8>>>(x)}"
    )
    unary = compile_source(
        PAIR
        + "operator -<type T>(x:Pair<T>){Pair{left=-x.left right=-x.right}} "
        + "module Top{in x:Pair<s8> out y:Pair<s9> y=-x}"
    )
    assert str(explicit.ir.assignments[0].expression.type) == "Box<Pair<s8>>"
    assert str(unary.ir.assignments[0].expression.type) == "Pair<s9>"


def test_fixed_builtin_overload_is_rejected_explicitly() -> None:
    with pytest.raises(SemanticError, match="built-in scalar and fixed"):
        compile_source(
            "operator +(a:fixed<8,4>,b:fixed<8,4>){a+b} "
            "module Top{in a:fixed<8,4> in b:fixed<8,4> out y:fixed<9,4> y=a+b}"
        )


def test_overlapping_generic_operator_patterns_are_rejected_at_environment_build() -> None:
    with pytest.raises(SemanticError, match="overlapping generic operator"):
        compile_source(
            PAIR
            + "operator +<type A>(a:Pair<A>,b:Pair<A>){a} "
            + "operator +<type X,type Y>(a:Pair<X>,b:Pair<Y>){b} "
            + "module Top{in a:Pair<u8> out y:Pair<u8> y=a}"
        )


def test_aliases_inside_generic_struct_specializations_are_canonicalized() -> None:
    result = compile_source(
        "struct Box<type T>{value:T} type Sample=fixed<18,16> "
        "type SampleBox=Box<Sample> "
        "module Top{in x:SampleBox out y:Box<fixed<18,16>> y=x}"
    )
    assert str(result.ir.ports[0].type) == "Box<fixed<18,16>>"
    assert result.ir.ports[0].type == result.ir.ports[1].type
