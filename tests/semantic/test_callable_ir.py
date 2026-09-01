from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.ir.callables import (
    CallableExpansionError,
    CallableKind,
    CallableMetadata,
    expand_callable_calls,
)
from zlang.ir.expressions import (
    Add,
    Call,
    Constant,
    FunctionalRegion,
    InputRef,
    ParameterRef,
    Reduce,
    ReductionOperator,
)
from zlang.ir.module import (
    Assignment,
    Function,
    FunctionParameter,
    Module,
    Port,
    PortDirection,
)
from zlang.ir.types import UIntType, VecType
from zlang.compiler import compile_source
from zlang.dependencies import DependencyClosure, DependencyModuleIdentity
from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA, canonical_ir_identity
from zlang.opt.ir import ExpressionOp
from zlang.opt.lowering import lower, restore
from zlang.semantic import SemanticError


U8 = UIntType(8)
U9 = UIntType(9)


def _definition(
    identity: str = "specialization:add-u8",
    *,
    name: str = "zlang_spec_add_u8",
) -> Function:
    parameters = (
        FunctionParameter("left", U8),
        FunctionParameter("right", U8),
    )
    metadata = CallableMetadata(
        CallableKind.OPERATOR,
        "operator+",
        "stdlib/math/pair.zl:operator+",
        identity,
        (("A", "u8"), ("B", "u8")),
    )
    return Function(
        name,
        parameters,
        U9,
        Add(ParameterRef("left", U8), ParameterRef("right", U8), U9),
        identity,
        metadata,
    )


def _module(definition: Function | None = None) -> Module:
    definition = definition or _definition()
    left = Port(PortDirection.INPUT, "left", U8)
    right = Port(PortDirection.INPUT, "right", U8)
    output = Port(PortDirection.OUTPUT, "result", U9)
    return Module(
        "CallableTop",
        (left, right, output),
        (
            Assignment(
                output,
                Call(
                    definition.name,
                    (InputRef("left", U8), InputRef("right", U8)),
                    U9,
                    definition.callee_identity,
                ),
            ),
        ),
        callable_definitions=(definition,),
    )


def test_specialized_callable_has_stable_metadata_and_expands_once() -> None:
    definition = _definition()
    call = Call(
        definition.name,
        (Constant(3, U8), Constant(5, U8)),
        U9,
        definition.callee_identity,
    )
    expanded = expand_callable_calls(call, (definition,))
    assert expanded == Add(Constant(3, U8), Constant(5, U8), U9)
    assert definition.metadata is not None
    assert definition.metadata.arguments == (("A", "u8"), ("B", "u8"))


def test_legacy_function_constructor_derives_a_stable_callee_identity() -> None:
    first = Function("identity", (FunctionParameter("x", U8),), U8, ParameterRef("x", U8))
    second = Function("identity", (FunctionParameter("x", U8),), U8, ParameterRef("x", U8))
    changed = Function("identity", (FunctionParameter("x", U9),), U9, ParameterRef("x", U9))
    assert first.callee_identity == second.callee_identity
    assert first.callee_identity != changed.callee_identity


def test_callable_definition_and_call_round_trip_through_canonical_ir() -> None:
    module = _module()
    canonical = lower(module)
    assert len(canonical.callable_definitions) == 1
    definition = canonical.callable_definitions[0]
    call = next(node for node in canonical.expressions if node.op is ExpressionOp.CALL)
    assert call.attribute("callee_identity") == definition.callee_identity
    assert definition.metadata == module.callable_definitions[0].metadata
    assert restore(canonical) == module


def test_callable_metadata_participates_in_current_canonical_identity() -> None:
    assert CANONICAL_IR_IDENTITY_SCHEMA == "zlang-canonical-ir-content-v11"
    original = lower(_module())
    definition = _definition("specialization:add-u8-v2")
    changed = lower(_module(definition))
    assert canonical_ir_identity(original) != canonical_ir_identity(changed)


def test_callable_diagnostic_source_relocation_does_not_change_identity() -> None:
    source = (
        "fn identity<type T>(x:T)->T{x} "
        "module Top{in x:u8 out y:u8 y=identity(x)}"
    )
    first = compile_source(
        source, source_unit="first/location.zl", include_clash=False
    )
    second = compile_source(
        source, source_unit="second/location.zl", include_clash=False
    )
    assert (
        first.ir.callable_definitions[0].metadata.declaration_identity
        != second.ir.callable_definitions[0].metadata.declaration_identity
    )
    assert first.high_level_ir_identity == second.high_level_ir_identity


def test_callable_functional_binder_identity_ignores_unrelated_function_order() -> None:
    target = (
        "fn target(x:vec<32,u8>)->vec<32,u8>{"
        "generate(i in 0..32) x[i]} "
    )
    unrelated = (
        "fn unrelated(x:vec<32,u8>)->vec<32,u8>{"
        "generate(j in 0..32) x[j]} "
    )
    top = "module Top{in x:vec<32,u8> out y:vec<32,u8> y=target(x)}"
    first = compile_source(
        target + unrelated + top,
        source_unit="callable-order.zl",
        include_clash=False,
    )
    reordered = compile_source(
        unrelated + target + top,
        source_unit="callable-order.zl",
        include_clash=False,
    )

    first_definition = next(
        function for function in first.ir.functions if function.name == "target"
    )
    reordered_definition = next(
        function for function in reordered.ir.functions
        if function.name == "target"
    )
    assert isinstance(first_definition.body, FunctionalRegion)
    assert isinstance(reordered_definition.body, FunctionalRegion)
    assert (
        first_definition.callee_identity
        == reordered_definition.callee_identity
    )
    assert (
        first_definition.body.binder.identity
        == reordered_definition.body.binder.identity
    )
    assert tuple(
        capture.identity for capture, _expression in first_definition.body.captures
    ) == tuple(
        capture.identity
        for capture, _expression in reordered_definition.body.captures
    )
    assert restore(lower(first.ir)) == first.ir
    assert restore(lower(reordered.ir)) == reordered.ir


def test_specialization_identity_is_sensitive_to_dependency_closure() -> None:
    source = (
        "fn identity<type T>(x:T)->T{x} "
        "module Top{in x:u8 out y:u8 y=identity(x)}"
    )

    def closure(digest: str) -> DependencyClosure:
        return DependencyClosure(
            1,
            "e" * 64,
            (
                DependencyModuleIdentity(
                    "math.Helper",
                    digest,
                    "c" * 64,
                    "1" * 40,
                ),
            ),
        )

    first = compile_source(
        source,
        dependency_closure=closure("d" * 64),
        include_clash=False,
    )
    repeated = compile_source(
        source,
        dependency_closure=closure("d" * 64),
        include_clash=False,
    )
    changed = compile_source(
        source,
        dependency_closure=closure("f" * 64),
        include_clash=False,
    )

    first_id = first.ir.callable_definitions[0].callee_identity
    assert first_id == repeated.ir.callable_definitions[0].callee_identity
    assert first_id != changed.ir.callable_definitions[0].callee_identity


def test_shared_callable_body_preserves_each_call_site_origin() -> None:
    result = compile_source(
        """fn identity<type T>(value:T)->T { value }
module Top {
    in value : u8
    out first : u8
    out second : u8
    first = identity(value)
    second = identity(value)
}
""",
        source_unit="call-sites.zl",
        include_clash=False,
    )
    assert len(result.ir.callable_definitions) == 1
    definition = result.ir.callable_definitions[0]
    assert definition.body.origin is not None
    assert definition.body.origin.span.start_line == 1

    first, second = (item.expression for item in result.ir.assignments)
    assert isinstance(first, Call) and isinstance(second, Call)
    assert first.callee_identity == second.callee_identity == definition.callee_identity
    assert first.origin is not None and second.origin is not None
    assert first.origin.span.start_line == 6
    assert second.origin.span.start_line == 7

    canonical_calls = tuple(
        node for node in result.high_level_ir.expressions
        if node.op is ExpressionOp.CALL
    )
    assert len(canonical_calls) == 2


def test_canonical_restore_rejects_unknown_or_mistyped_callee() -> None:
    canonical = lower(_module())
    call_index = next(
        index
        for index, node in enumerate(canonical.expressions)
        if node.op is ExpressionOp.CALL
    )
    call = canonical.expressions[call_index]
    unknown = replace(
        call,
        attributes=tuple(
            (name, "missing") if name == "callee_identity" else (name, value)
            for name, value in call.attributes
        ),
    )
    with pytest.raises(ValueError, match="unknown callable"):
        replace(
            canonical,
            expressions=(
                *canonical.expressions[:call_index],
                unknown,
                *canonical.expressions[call_index + 1 :],
            ),
        )

    wrong_type = replace(
        call,
        type=U8,
        metadata=replace(call.metadata, width=U8.width),
    )
    with pytest.raises(ValueError, match="invalid return type"):
        replace(
            canonical,
            expressions=(
                *canonical.expressions[:call_index],
                wrong_type,
                *canonical.expressions[call_index + 1 :],
            ),
        )

    wrong_arguments = replace(call, operands=(call.operands[0],))
    with pytest.raises(ValueError, match="invalid argument types"):
        replace(
            canonical,
            expressions=(
                *canonical.expressions[:call_index],
                wrong_arguments,
                *canonical.expressions[call_index + 1 :],
            ),
        )


def test_canonical_ir_rejects_a_recursive_callable_graph() -> None:
    canonical = lower(_module())
    definition = canonical.callable_definitions[0]
    original_body = canonical.expressions[definition.body]
    recursive_body = replace(
        original_body,
        id=len(canonical.expressions),
        op=ExpressionOp.CALL,
        operands=original_body.operands,
        attributes=(
            ("function", definition.name),
            ("callee_identity", definition.callee_identity),
        ),
    )
    with pytest.raises(ValueError, match="canonical callable cycle"):
        replace(
            canonical,
            expressions=(*canonical.expressions, recursive_body),
            callable_definitions=(
                replace(definition, body=recursive_body.id),
            ),
        )


def test_callable_expansion_rejects_cycles_and_bounds_growth() -> None:
    left_metadata = CallableMetadata(
        CallableKind.FUNCTION, "left", "fixture:left", "left-id"
    )
    right_metadata = CallableMetadata(
        CallableKind.FUNCTION, "right", "fixture:right", "right-id"
    )
    left = Function(
        "left",
        (FunctionParameter("x", U8),),
        U8,
        Call("right", (ParameterRef("x", U8),), U8, "right-id"),
        "left-id",
        left_metadata,
    )
    right = Function(
        "right",
        (FunctionParameter("x", U8),),
        U8,
        Call("left", (ParameterRef("x", U8),), U8, "left-id"),
        "right-id",
        right_metadata,
    )
    root = Call("left", (Constant(1, U8),), U8, "left-id")
    with pytest.raises(CallableExpansionError, match="cycle"):
        expand_callable_calls(root, (left, right))
    with pytest.raises(CallableExpansionError, match="exceeds 1 expression nodes"):
        expand_callable_calls(root, (left, right), max_nodes=1)


def test_callable_expansion_keeps_nominal_reduce_implementation_opaque() -> None:
    identity = Function(
        "identity9",
        (FunctionParameter("x", U9),),
        U9,
        ParameterRef("x", U9),
    )
    frozen = Call(
        identity.name,
        (Constant(3, U9),),
        U9,
        identity.callee_identity,
    )
    reduction = Reduce(
        ReductionOperator.ADD,
        InputRef("values", VecType(2, U8)),
        U9,
        frozen,
    )
    expanded = expand_callable_calls(reduction, (identity,))
    assert isinstance(expanded, Reduce)
    assert expanded.expanded == frozen


def test_canonical_callable_definitions_are_sorted_deterministically() -> None:
    first = _definition("b", name="second_in_source")
    second = _definition("a", name="first_in_canonical_order")
    module = replace(
        _module(first),
        callable_definitions=(first, second),
    )
    canonical = lower(module)
    assert tuple(
        definition.callee_identity for definition in canonical.callable_definitions
    ) == ("a", "b")
    with pytest.raises(ValueError, match="ordered by callee identity"):
        replace(
            canonical,
            callable_definitions=tuple(reversed(canonical.callable_definitions)),
        )


def test_ordinary_function_may_call_a_retained_generic_specialization() -> None:
    result = compile_source(
        """fn identity<type T>(value:T)->T { value }
fn wrapper(value:u8)->u8 { identity(value) }
module Top {
    in value : u8
    out result : u8
    result = wrapper(value)
}
""",
        source_unit="ordinary-calls-generic.zl",
        include_clash=False,
    )

    wrapper = next(
        function for function in result.ir.functions if function.name == "wrapper"
    )
    specialization = result.ir.callable_definitions[0]
    assert isinstance(wrapper.body, Call)
    assert wrapper.body.callee_identity == specialization.callee_identity
    assert restore(lower(result.ir)) == result.ir


def test_recursion_across_ordinary_and_generic_functions_is_rejected() -> None:
    source = """fn wrapper(value:u8)->u8 { helper(value) }
fn helper<type T>(value:T)->T { wrapper(value) }
module Bad {
    in value : u8
    out result : u8
    result = wrapper(value)
}
"""
    with pytest.raises(SemanticError, match="recursive function call"):
        compile_source(
            source,
            source_unit="ordinary-generic-cycle.zl",
            include_clash=False,
        )
