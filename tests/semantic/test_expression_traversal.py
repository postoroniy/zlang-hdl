from __future__ import annotations

from typing import get_args

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.functional import materialize_functional_region
from zlang.ir.functional_regions import (
    CompileTimeBinderRef,
    CompileTimeExpr,
    FunctionalRegionKind,
)
from zlang.ir.traversal import (
    ExpressionTraversalError,
    SUPPORTED_EXPRESSION_TYPES,
    expression_children,
    walk_expression,
)
from zlang.simulate import simulate_cycles
from zlang.ir.types import UIntType, VecType


def test_expression_traversal_covers_the_closed_expression_union() -> None:
    assert set(SUPPORTED_EXPRESSION_TYPES) == set(get_args(expr.Expression))
    with pytest.raises(ExpressionTraversalError, match="no child policy"):
        expression_children(object())  # type: ignore[arg-type]


def test_nested_enum_conversions_publish_and_advance_all_delays() -> None:
    module = compile_source(
        """
        enum E : bits<2> { A=0 B=2 }
        module EnumDelay {
            clock clk
            reset rst
            in raw : bits<2>
            out encoded : bits<2>
            out valid : bit
            encoded = enum_encode(
                enum_decode<E>(delay<1>(raw), E.A)
            )
            valid = enum_valid<E>(delay<1>(raw))
        }
        """,
    ).ir

    delay_instances = {
        node.instance
        for assignment in module.assignments
        for node in walk_expression(assignment.expression)
        if isinstance(node, expr.Delay)
    }
    assert delay_instances == {0, 1}
    assert simulate_cycles(
        module,
        ({"raw": 2}, {"raw": 1}, {"raw": 0}),
    ) == [
        {"encoded": 0, "valid": 1},
        {"encoded": 2, "valid": 1},
        {"encoded": 0, "valid": 0},
    ]


def test_executable_materialization_is_owned_by_functional_lowering() -> None:
    u4 = UIntType(4)
    binder = CompileTimeBinderRef("fixture:traversal", "i", 0, 16)
    expression = expr.FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        binder,
        expr.FunctionalValue(CompileTimeExpr.ref(binder), u4),
        (),
        (),
        VecType(16, u4),
    )
    children = materialize_functional_region(expression)
    assert len(children) == 16
    assert all(isinstance(child, expr.Constant) for child in children)
