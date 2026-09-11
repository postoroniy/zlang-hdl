"""Shared backend expression-materialization policy tests."""

from zlang.backend.expression_materialization import (
    MaterializedExpression,
    dependency_ordered_materialization,
    expression_size,
    module_expression_roots,
    plan_materialization,
    replace_materialized,
)
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.types import FixedType, UIntType


def test_shared_large_typed_expression_is_named_once_deterministically() -> None:
    u8 = UIntType(8)
    u9 = UIntType(9)
    a = expr.InputRef("a", u8)
    b = expr.InputRef("b", u8)
    widened = expr.Add(a, b, u9)
    shared = expr.Add(widened, expr.Constant(1, u9), u9)
    root = expr.Add(shared, shared, UIntType(10))

    first = plan_materialization((root,), reserved_names=("a", "b"))
    second = plan_materialization((root,), reserved_names=("a", "b"))
    assert first == second
    assert [item.expression for item in first].count(shared) == 1
    assert expression_size(shared) == 5

    aliases = {item.expression: item.name for item in first}
    rewritten = replace_materialized(root, aliases)
    assert isinstance(rewritten, expr.Add)
    assert isinstance(rewritten.left, expr.InputRef)
    assert rewritten.left == rewritten.right
    assert rewritten.left.type == u9


def test_fixed_conversion_input_is_materialized_without_moving_boundary() -> None:
    source_type = FixedType(24, 16)
    target_type = FixedType(16, 8)
    source = expr.Add(
        expr.Add(
            expr.InputRef("a", source_type),
            expr.InputRef("b", source_type),
            source_type,
        ),
        expr.Add(
            expr.Add(
                expr.InputRef("c", source_type),
                expr.InputRef("d", source_type),
                source_type,
            ),
            expr.InputRef("e", source_type),
            source_type,
        ),
        source_type,
    )
    conversion = expr.FixedConvert(
        source,
        expr.FixedRounding.NEAREST_EVEN,
        expr.FixedOverflow.SATURATE,
        expr.FixedConversionKind.RESCALE,
        target_type,
    )

    plan = plan_materialization((conversion,))
    item = next(item for item in plan if item.expression == source)
    rewritten = replace_materialized(conversion, {source: item.name})
    assert isinstance(rewritten, expr.FixedConvert)
    assert isinstance(rewritten.expression, expr.InputRef)
    assert rewritten.expression.type == source_type
    assert rewritten.type == target_type
    assert rewritten.rounding is expr.FixedRounding.NEAREST_EVEN
    assert rewritten.overflow is expr.FixedOverflow.SATURATE


def test_resolved_state_actions_are_not_double_counted_through_legacy_rules() -> None:
    module = compile_source(
        """
module StatefulMaterialization {
    clock clk
    reset rst
    in enable : bit
    out value : u8
    reg count : u8 = 0
    rule increment when enable { count <- truncate<8>(count + 1) }
    value = count
}
""",
    ).ir
    assert module.resolved_transition is not None
    group = module.resolved_transition.action_groups[0]
    roots = module_expression_roots(module)
    assert roots.count(group.guard) == 1
    assert roots.count(group.actions[0].operands[0]) == 1


def test_procedural_materialization_orders_shared_dependencies_first() -> None:
    u8 = UIntType(8)
    u9 = UIntType(9)
    u10 = UIntType(10)
    shared = expr.Add(expr.InputRef("a", u8), expr.InputRef("b", u8), u9)
    first = expr.Add(shared, expr.Constant(1, u9), u10)
    second = expr.Add(shared, expr.Constant(2, u9), u10)

    # A shared expression may have been discovered through a different root,
    # so neither planner order nor its reversal is a general dependency order.
    scrambled = (
        MaterializedExpression(first, "parent_first"),
        MaterializedExpression(shared, "shared"),
        MaterializedExpression(second, "parent_second"),
    )
    ordered = dependency_ordered_materialization(scrambled)
    assert tuple(item.name for item in ordered) == (
        "shared",
        "parent_first",
        "parent_second",
    )
