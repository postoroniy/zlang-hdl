from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

import zlang.ir.callables as callables
from zlang.ir.callables import CallableKind, CallableMetadata, CallableReachabilityError
from zlang.ir.expressions import Call, Constant, ParameterRef
from zlang.ir.module import Assignment, Function, FunctionParameter, Module, Port, PortDirection
from zlang.ir.types import UIntType
from zlang.opt.identity import canonical_ir_identity
from zlang.opt.lowering import lower, restore
from zlang.source import SourceOrigin, SourceSpan


U8 = UIntType(8)
PARAMETER = FunctionParameter("x", U8)


def _leaf(name: str = "leaf") -> Function:
    return Function(name, (PARAMETER,), U8, ParameterRef("x", U8))


def _call(definition: Function) -> Call:
    return Call(
        definition.name, (Constant(1, U8),), U8, definition.callee_identity
    )


def _module(definitions: tuple[Function, ...], root: Function) -> Module:
    output = Port(PortDirection.OUTPUT, "result", U8)
    return Module(
        "CallableRepresentativeTop",
        (output,),
        (Assignment(output, _call(root)),),
        functions=definitions,
    )


def test_singleton_selection_skips_only_ordering_and_retains_self_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = SourceOrigin(
        SourceSpan(3, 5, 3, 12), "call", "singleton-origin.zhl", "a" * 64
    )
    leaf = _leaf()
    parent = Function(
        "parent", (PARAMETER,), U8,
        Call(leaf.name, (ParameterRef("x", U8),), U8, leaf.callee_identity,
             origin=origin),
    )
    unused = _leaf("unused")
    module = _module((leaf, parent), parent)
    before = canonical_ir_identity(lower(module))
    key_calls: list[Function] = []
    validations: list[tuple[Function, Function]] = []
    original_key = callables._callable_definition_order_key
    original_equivalent = callables._callable_definitions_equivalent

    def order_key(value):
        key_calls.append(value)
        return original_key(value)

    def equivalent(left, right, definitions, *, seen=None):
        validations.append((left, right))
        return original_equivalent(left, right, definitions, seen=seen)

    monkeypatch.setattr(callables, "_callable_definition_order_key", order_key)
    monkeypatch.setattr(callables, "_callable_definitions_equivalent", equivalent)
    selected = callables.reachable_callable_definitions(
        (unused, parent, leaf), (_call(parent),)
    )

    # The recursive-equivalence helper still resolves the left and right leaf
    # independently. No top-level singleton contributes an ordering-key call.
    assert len(key_calls) == 2
    assert all(value is leaf for value in key_calls)
    assert len(validations) == 4
    assert all(left is right for left, right in validations)
    assert Counter(id(left) for left, _right in validations) == {
        id(leaf): 2, id(parent): 1, id(unused): 1,
    }
    assert len(selected) == 2
    assert selected[0] is leaf
    assert selected[1] is parent
    assert selected[1].body.origin is origin

    canonical = lower(replace(module, functions=selected))
    assert canonical_ir_identity(canonical) == before
    restored = restore(canonical)
    assert restored.functions[1].body.origin == origin
    assert canonical_ir_identity(lower(restored)) == before


def test_multiple_candidates_keep_exact_key_selected_representative_in_both_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tuple(
        Function(
            f"nested_{suffix}", (PARAMETER,), U8, ParameterRef("x", U8),
            metadata=CallableMetadata(
                CallableKind.FUNCTION, "identity", "test:identity",
                specialization_identity=f"nested-{suffix}",
                arguments=(("T", "u8"),),
            ),
        )
        for suffix in ("a", "b")
    )
    metadata = CallableMetadata(
        CallableKind.FUNCTION, "outer", "test:outer",
        specialization_identity="outer-id",
    )
    outer = tuple(
        Function(
            "outer", (PARAMETER,), U8,
            Call(
                child.name, (ParameterRef("x", U8),), U8, child.callee_identity,
                origin=SourceOrigin(
                    SourceSpan(index + 1, 1, index + 1, 8), "call",
                    f"context-{index}.zhl",
                ),
            ),
            metadata=metadata,
        )
        for index, child in enumerate(nested)
    )
    original_key = callables._callable_definition_order_key
    assert original_key(outer[0]) != original_key(outer[1])
    expected = min(outer, key=original_key)
    expected_child = nested[outer.index(expected)]
    before = canonical_ir_identity(lower(_module((expected_child, expected), expected)))
    key_calls: list[Function] = []

    def order_key(value):
        key_calls.append(value)
        return original_key(value)

    monkeypatch.setattr(callables, "_callable_definition_order_key", order_key)
    definitions = (*nested, *outer)
    for candidates in (definitions, tuple(reversed(definitions))):
        key_calls.clear()
        selected = callables.reachable_callable_definitions(
            candidates, (_call(expected),)
        )
        assert len(selected) == 2
        assert selected[0] is expected_child
        assert selected[1] is expected
        assert selected[1].body.origin is expected.body.origin
        assert [id(item) for item in key_calls if item.name == "outer"] == [
            id(item) for item in candidates if item.name == "outer"
        ]
        assert canonical_ir_identity(lower(_module(selected, expected))) == before


def test_equal_key_duplicates_retain_first_candidate_and_its_source_origin() -> None:
    definitions = tuple(
        replace(
            _leaf(),
            body=ParameterRef(
                "x", U8,
                origin=SourceOrigin(SourceSpan(1, 1, 1, 2), "parameter", source),
            ),
        )
        for source in ("first.zhl", "second.zhl")
    )
    assert definitions[0] is not definitions[1]
    assert callables._callable_definition_order_key(definitions[0]) == (
        callables._callable_definition_order_key(definitions[1])
    )
    before = canonical_ir_identity(lower(_module((definitions[0],), definitions[0])))
    for candidates in (definitions, tuple(reversed(definitions))):
        selected = callables.reachable_callable_definitions(candidates, (_call(candidates[0]),))
        assert len(selected) == 1
        assert selected[0] is candidates[0]
        assert selected[0].body.origin is candidates[0].body.origin
        assert canonical_ir_identity(lower(_module(selected, selected[0]))) == before


@pytest.mark.parametrize(
    "case",
    ("conflict", "malformed_unused", "malformed_reachable", "unknown", "recursive"),
)
def test_singleton_and_duplicate_selection_preserve_exact_diagnostics(case: str) -> None:
    leaf = _leaf()
    if case == "conflict":
        definitions = (leaf, replace(leaf, body=Constant(0, U8)))
        roots = (_call(leaf),)
        expected = f"callable identity '{leaf.callee_identity}' has conflicting definitions"
    elif case.startswith("malformed"):
        malformed = Function(
            "outer", (PARAMETER,), U8,
            Call("wrong", (ParameterRef("x", U8),), U8, leaf.callee_identity),
        )
        definitions = (leaf, malformed)
        roots = () if case == "malformed_unused" else (_call(malformed),)
        expected = (
            f"callable identity '{malformed.callee_identity}' has conflicting definitions"
        )
    elif case == "unknown":
        unknown = Function(
            "outer", (PARAMETER,), U8,
            Call("missing", (ParameterRef("x", U8),), U8, "missing-id"),
        )
        definitions = (unknown,)
        roots = (_call(unknown),)
        expected = "call 'missing' references unknown typed callable 'missing-id'"
    else:
        definitions = (replace(
            leaf,
            body=Call(leaf.name, (ParameterRef("x", U8),), U8, leaf.callee_identity),
        ),)
        roots = (_call(leaf),)
        expected = (
            f"recursive typed callable cycle: {leaf.callee_identity}"
            f" -> {leaf.callee_identity}"
        )
    for candidates in (definitions, tuple(reversed(definitions))):
        with pytest.raises(CallableReachabilityError) as caught:
            callables.reachable_callable_definitions(candidates, roots)
        assert str(caught.value) == expected
