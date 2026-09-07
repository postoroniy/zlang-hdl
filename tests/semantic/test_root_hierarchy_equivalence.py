"""Bounded typed whole-root value materialization for M36/M38."""

from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.compiler import compile_source
from zlang.cross_backend import validate_module_route
from zlang.ir.cross_backend import CrossBackendError
from zlang.ir.hierarchical_values import (
    HierarchicalValueError,
    materialize_pure_hierarchical_output,
)
from zlang.ir.expressions import Constant
from zlang.ir.module import (
    Assignment,
    HierarchicalConnection,
    Module,
    ProtocolEndpoint,
)
from zlang.ir.types import UIntType
from zlang.opt import CanonicalizationError, lower, restore
from zlang.semantic import SemanticError
from zlang.simulate import simulate


PURE_SOURCE = """
module Child {
    in a : u8
    in b : u8
    out sum : u9
    sum = a + b
}

module Top {
    in a : u8
    in b : u8
    in bias : u9
    out y : u11
    inst child : Child { a b }
    y = extend<10>(child.sum) + extend<10>(bias)
}
"""


def _module(source: str = PURE_SOURCE, *, top: str = "Top") -> Module:
    return compile_source(source, top=top, include_clash=False).ir


def test_materializes_exact_child_value_through_typed_bindings() -> None:
    module = _module()
    value = materialize_pure_hierarchical_output(module, "y")

    assert value.output.name == "y"
    assert tuple(port.name for port in value.inputs) == ("a", "b", "bias")
    assert value.expression.type == value.output.type
    assert "InstanceOutputRef" not in repr(value.expression)
    assert value.expression.origin == module.assignments[0].expression.origin

    reference = Module(
        "Reference",
        (*value.inputs, value.output),
        (Assignment(value.output, value.expression),),
    )
    for inputs in (
        {"a": 0, "b": 0, "bias": 0},
        {"a": 1, "b": 2, "bias": 4},
        {"a": 255, "b": 255, "bias": 511},
    ):
        assert simulate(reference, **inputs) == simulate(module, **inputs)
    # The legacy raw-artifact M38 entry remains fail-closed for hierarchy.  A
    # hierarchy may execute only through the compiler-owned preparation route,
    # which flattens and namespaces each backend artifact before M38.
    with pytest.raises(CrossBackendError, match="hierarchical modules"):
        validate_module_route(module)


def test_materialization_rejects_incomplete_or_duplicate_typed_bindings() -> None:
    module = _module()
    missing = replace(module, instance_bindings=module.instance_bindings[:-1])
    with pytest.raises(HierarchicalValueError, match="input bindings are incomplete"):
        materialize_pure_hierarchical_output(missing, "y")

    duplicate = replace(
        module,
        instance_bindings=(*module.instance_bindings, module.instance_bindings[0]),
    )
    with pytest.raises(HierarchicalValueError, match="duplicate input bindings"):
        materialize_pure_hierarchical_output(duplicate, "y")


def test_semantic_and_canonical_hierarchy_require_exact_scalar_bindings() -> None:
    with pytest.raises(SemanticError, match="child wire input 'child.b' has no driver"):
        _module(PURE_SOURCE.replace("inst child : Child { a b }", "inst child : Child { a }"))

    canonical = lower(_module())
    first = canonical.instance_bindings[0]
    malformed = (
        (canonical.instance_bindings[:-1], "child wire input 'child.b' has no driver"),
        ((*canonical.instance_bindings, first), "child.a.*multiple bindings"),
        ((replace(first, instance="missing"), *canonical.instance_bindings[1:]), "unknown physical child 'missing'"),
        ((replace(first, port="missing"), *canonical.instance_bindings[1:]), "does not name a child port"),
        ((replace(first, port="sum"), *canonical.instance_bindings[1:]), "does not name a child input"),
        (
            (
                replace(first, expression=Constant(0, UIntType(9))),
                *canonical.instance_bindings[1:],
            ),
            "has type u9, expected u8",
        ),
    )
    for bindings, message in malformed:
        with pytest.raises(CanonicalizationError, match=message):
            restore(replace(canonical, instance_bindings=bindings))


def test_canonical_hierarchy_revalidates_nested_scalar_bindings() -> None:
    module = _module("""
        module Leaf { in x:u8 out y:u8 y=x }
        module Branch {
            in x:u8 out y:u8
            inst leaf:Leaf { x }
            y=leaf.y
        }
        module Top {
            in x:u8 out y:u8
            inst branch:Branch { x }
            y=branch.y
        }
    """)
    canonical = lower(module)
    branch = canonical.children[0]
    malformed_branch = replace(branch, instance_bindings=())

    with pytest.raises(CanonicalizationError, match="leaf.x.*has no driver"):
        restore(replace(canonical, children=(malformed_branch,)))

    top_input = branch.inputs[0]
    child_input = branch.children[0].inputs[0]
    forged_connection = HierarchicalConnection(
        ProtocolEndpoint(
            branch.name,
            top_input.name,
            top_input.direction,
            top_input.protocol,
            top_input.type,
            top_input.capacity,
            top_input.domain,
        ),
        ProtocolEndpoint(
            branch.elaborated_instances[0].instance.name,
            child_input.name,
            child_input.direction,
            child_input.protocol,
            child_input.type,
            child_input.capacity,
            branch.children[0].clock,
        ),
    )
    with pytest.raises(
        CanonicalizationError,
        match="does not name an aggregate scalar protocol member",
    ):
        restore(replace(
            canonical,
            children=(replace(
                malformed_branch,
                hierarchical_connections=(forged_connection,),
            ),),
        ))


@pytest.mark.parametrize(
    ("source", "detail"),
    (
        (
            """
            module Counter {
                clock clk reset rst
                in x:u8 out y:u8
                reg q:u8=0
                q <- x
                y=q
            }
            module Top {
                clock clk reset rst
                in x:u8 out y:u8
                inst child:Counter { x }
                y=child.y
            }
            """,
            "clock/reset domains",
        ),
        (
            """
            module Pass { in i:rv<u8> out o:rv<u8> i -> o }
            module Top { in i:rv<u8> out o:rv<u8> inst child:Pass i -> child.i child.o -> o }
            """,
            "protocol ports",
        ),
        (
            """
            module Leaf { in x:u8 out y:u8 y=x }
            module Branch { in x:u8 out y:u8 inst leaf:Leaf { x } y=leaf.y }
            module Top { in x:u8 out y:u8 inst branch:Branch { x } y=branch.y }
            """,
            "nested hierarchy",
        ),
        (
            """
            module Lane { in x:u8 out y:u8 y=x }
            module Top {
                in x:u8 out y:u8
                inst lane[2]:Lane
                generate(i in 0..2) { lane[i].x=x }
                y=lane[0].y
            }
            """,
            "exactly one physical child",
        ),
        (
            """
            struct Pair { left:u8 right:u8 }
            module Child { in p:Pair out y:u8 y=p.left }
            module Top { in p:Pair out y:u8 inst child:Child { p } y=child.y }
            """,
            "aggregate ports",
        ),
    ),
)
def test_materialization_fails_closed_outside_the_frozen_slice(
    source: str, detail: str
) -> None:
    module = _module(source)
    with pytest.raises(HierarchicalValueError, match=detail):
        materialize_pure_hierarchical_output(module, "y" if detail != "protocol ports" else "o")
