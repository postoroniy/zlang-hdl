# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Conservative compiler-owned value-range analysis."""

from __future__ import annotations

from zlang.ir import expressions as ir_expr
from zlang.ir import functional_regions
from zlang.ir import types as ir_types


def unsigned_type_range(type_: ir_types.HardwareType) -> ir_expr.ValueRange | None:
    if isinstance(type_, (ir_types.UIntType, ir_types.BitsType)):
        return ir_expr.ValueRange(0, (1 << type_.width) - 1, "static_type")
    return None


def static_value_range(
    expression: ir_expr.Expression,
    refinements: dict[str, ir_expr.ValueRange] | None = None,
) -> ir_expr.ValueRange | None:
    """Compute a cheap conservative interval for an unsigned value expression."""

    if isinstance(expression, ir_expr.Constant):
        if isinstance(expression.type, (ir_types.UIntType, ir_types.BitsType)):
            return ir_expr.ValueRange(expression.value, expression.value, "constant")
        return None
    if isinstance(expression, ir_expr.FunctionalValue):
        minimum, maximum = functional_regions.compile_time_range(expression.expression)
        if minimum < 0 or not isinstance(expression.type, (ir_types.UIntType, ir_types.BitsType)):
            return None
        return ir_expr.ValueRange(minimum, maximum, "functional_binder")
    if isinstance(expression, (ir_expr.InputRef, ir_expr.RegisterRef, ir_expr.ParameterRef)):
        if refinements is not None and expression.name in refinements:
            return refinements[expression.name]
        return unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.FieldAccess):
        aggregate_type = expression.expression.type
        if not isinstance(aggregate_type, ir_types.StructType):
            return None
        declared = aggregate_type.field(expression.field)
        if declared is None or declared.type != expression.type:
            return None
        if isinstance(expression.expression, ir_expr.StructConstruct):
            matching = tuple(
                value
                for name, value in expression.expression.fields
                if name == expression.field
            )
            if len(matching) != 1:
                return None
            concrete = static_value_range(matching[0], refinements)
            if concrete is not None:
                return concrete
        # A stored aggregate may have any legal value of its declared field
        # type.  That exact type interval remains a sound conservative proof
        # even when the aggregate came from a register or child output.
        return unsigned_type_range(declared.type)
    if isinstance(expression, ir_expr.Truncate):
        return (
            ir_expr.ValueRange(0, (1 << expression.type.width) - 1, "truncate")
            if isinstance(expression.type, (ir_types.UIntType, ir_types.BitsType)) else None
        )
    if isinstance(expression, ir_expr.Extend):
        operand = static_value_range(expression.expression, refinements)
        return (
            ir_expr.ValueRange(operand.minimum, operand.maximum, "extend")
            if operand is not None and isinstance(expression.type, (ir_types.UIntType, ir_types.BitsType))
            else None
        )
    if isinstance(expression, (ir_expr.Slice, ir_expr.Concat, ir_expr.Bitcast)):
        return unsigned_type_range(expression.type)
    if isinstance(expression, (ir_expr.Pack, ir_expr.Unpack)):
        return unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.FunctionalTableLookup):
        return expression.value_range or unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.VectorIndex):
        source = expression.expression
        # A statically selected element of an explicitly retained vector has
        # the element's own interval.  Otherwise its exact value is unknown,
        # but the canonical element type is still a sound conservative proof.
        elements = (
            source.elements
            if isinstance(source, (ir_expr.Generate, ir_expr.Map))
            else None
        )
        if elements is not None:
            minimum, maximum = functional_regions.compile_time_range(expression.index)
            selected_ranges = tuple(
                static_value_range(elements[index], refinements)
                for index in range(minimum, maximum + 1)
            )
            if selected_ranges and all(item is not None for item in selected_ranges):
                concrete = tuple(
                    item for item in selected_ranges if item is not None
                )
                return ir_expr.ValueRange(
                    min(item.minimum for item in concrete),
                    max(item.maximum for item in concrete),
                    "constant_table",
                )
        return unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.RuntimeIndex):
        source = expression.expression
        elements = (
            source.elements
            if isinstance(source, (ir_expr.Generate, ir_expr.Map))
            else None
        )
        if elements is not None:
            ranges = tuple(
                static_value_range(element, refinements) for element in elements
            )
            if ranges and all(item is not None for item in ranges):
                concrete = tuple(item for item in ranges if item is not None)
                return ir_expr.ValueRange(
                    min(item.minimum for item in concrete),
                    max(item.maximum for item in concrete),
                    "constant_table",
                )
        return unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.Add):
        left = static_value_range(expression.left, refinements)
        right = static_value_range(expression.right, refinements)
        if left is not None and right is not None and isinstance(expression.type, ir_types.UIntType):
            return ir_expr.ValueRange(
                left.minimum + right.minimum,
                left.maximum + right.maximum,
                "arithmetic",
            )
        return None
    if isinstance(expression, ir_expr.Binary):
        left = static_value_range(expression.left, refinements)
        right = static_value_range(expression.right, refinements)
        if left is None or right is None or not isinstance(expression.type, (ir_types.UIntType, ir_types.BitsType)):
            return None
        if expression.operator is ir_expr.BinaryOperator.SUBTRACT:
            if left.minimum >= right.maximum:
                return ir_expr.ValueRange(
                    left.minimum - right.maximum,
                    left.maximum - right.minimum,
                    "arithmetic",
                )
            return unsigned_type_range(expression.type)
        if expression.operator is ir_expr.BinaryOperator.MULTIPLY:
            return ir_expr.ValueRange(
                left.minimum * right.minimum,
                left.maximum * right.maximum,
                "arithmetic",
            )
        if expression.operator is ir_expr.BinaryOperator.SHIFT_RIGHT:
            if right.minimum == right.maximum:
                return ir_expr.ValueRange(
                    left.minimum >> right.minimum,
                    left.maximum >> right.minimum,
                    "arithmetic",
                )
        if expression.operator is ir_expr.BinaryOperator.SHIFT_LEFT:
            if right.minimum == right.maximum:
                shifted_maximum = left.maximum << right.minimum
                type_maximum = (1 << expression.type.width) - 1
                if shifted_maximum <= type_maximum:
                    return ir_expr.ValueRange(
                        left.minimum << right.minimum,
                        shifted_maximum,
                        "arithmetic",
                    )
        return unsigned_type_range(expression.type)
    if isinstance(expression, ir_expr.Mux):
        true_range = static_value_range(expression.when_true, refinements)
        false_range = static_value_range(expression.when_false, refinements)
        if true_range is not None and false_range is not None:
            return ir_expr.ValueRange(
                min(true_range.minimum, false_range.minimum),
                max(true_range.maximum, false_range.maximum),
                "union",
            )
        return None
    if isinstance(expression, ir_expr.Switch):
        ranges = [
            *(
                static_value_range(case.expression, refinements)
                for case in expression.cases
            ),
            static_value_range(expression.default, refinements),
        ]
        if all(item is not None for item in ranges):
            concrete = [item for item in ranges if item is not None]
            return ir_expr.ValueRange(
                min(item.minimum for item in concrete),
                max(item.maximum for item in concrete),
                "union",
            )
    return None


def guard_range_refinements(
    expression: ir_expr.Expression,
) -> dict[str, ir_expr.ValueRange]:
    """Extract sound unsigned intervals from a bounded rule guard.

    Conjunction and comparisons against exact unsigned constants are
    recognized.  The explicit ``predicate == 0`` shape produced by logical
    negation is inverted only where one interval remains exact.  Unsupported
    boolean structure contributes no fact; disjunction is never approximated.
    """

    def reference_name(value: ir_expr.Expression) -> str | None:
        if isinstance(
            value, (ir_expr.InputRef, ir_expr.RegisterRef, ir_expr.ParameterRef)
        ) and isinstance(value.type, (ir_types.UIntType, ir_types.BitsType)):
            return value.name
        return None

    def exact_constant(value: ir_expr.Expression) -> int | None:
        if isinstance(value, ir_expr.Constant) and isinstance(
            value.type, (ir_types.UIntType, ir_types.BitsType)
        ):
            return value.value
        return None

    def intersect(
        left: dict[str, ir_expr.ValueRange],
        right: dict[str, ir_expr.ValueRange],
    ) -> dict[str, ir_expr.ValueRange]:
        result = dict(left)
        for name, candidate in right.items():
            previous = result.get(name)
            if previous is None:
                result[name] = candidate
                continue
            minimum = max(previous.minimum, candidate.minimum)
            maximum = min(previous.maximum, candidate.maximum)
            if minimum <= maximum:
                result[name] = ir_expr.ValueRange(
                    minimum, maximum, "guard_conjunction"
                )
        return result

    def visit(
        value: ir_expr.Expression,
        truth: bool = True,
    ) -> dict[str, ir_expr.ValueRange]:
        if not isinstance(value, ir_expr.Binary):
            return {}
        if value.operator is ir_expr.BinaryOperator.BIT_AND:
            if not truth:
                # ``!(a & b)`` is a disjunction and has no single sound
                # interval in the bounded refinement representation.
                return {}
            return intersect(visit(value.left), visit(value.right))

        # Logical NOT is lowered to an exact comparison with a bit zero.
        if value.operator is ir_expr.BinaryOperator.EQUAL:
            if (
                isinstance(value.right, ir_expr.Constant)
                and isinstance(value.right.type, ir_types.BitType)
                and value.right.value in {0, 1}
                and isinstance(value.left.type, ir_types.BitType)
            ):
                return visit(value.left, truth == bool(value.right.value))
            if (
                isinstance(value.left, ir_expr.Constant)
                and isinstance(value.left.type, ir_types.BitType)
                and value.left.value in {0, 1}
                and isinstance(value.right.type, ir_types.BitType)
            ):
                return visit(value.right, truth == bool(value.left.value))

        operator = value.operator
        name = reference_name(value.left)
        constant = exact_constant(value.right)
        referenced = value.left
        if name is None or constant is None:
            reverse = {
                ir_expr.BinaryOperator.LESS: ir_expr.BinaryOperator.GREATER,
                ir_expr.BinaryOperator.LESS_EQUAL: ir_expr.BinaryOperator.GREATER_EQUAL,
                ir_expr.BinaryOperator.GREATER: ir_expr.BinaryOperator.LESS,
                ir_expr.BinaryOperator.GREATER_EQUAL: ir_expr.BinaryOperator.LESS_EQUAL,
                ir_expr.BinaryOperator.EQUAL: ir_expr.BinaryOperator.EQUAL,
                ir_expr.BinaryOperator.NOT_EQUAL: ir_expr.BinaryOperator.NOT_EQUAL,
            }
            name = reference_name(value.right)
            constant = exact_constant(value.left)
            referenced = value.right
            operator = reverse.get(operator)
        if name is None or constant is None or operator is None:
            return {}
        if not truth:
            operator = {
                ir_expr.BinaryOperator.LESS: ir_expr.BinaryOperator.GREATER_EQUAL,
                ir_expr.BinaryOperator.LESS_EQUAL: ir_expr.BinaryOperator.GREATER,
                ir_expr.BinaryOperator.GREATER: ir_expr.BinaryOperator.LESS_EQUAL,
                ir_expr.BinaryOperator.GREATER_EQUAL: ir_expr.BinaryOperator.LESS,
                ir_expr.BinaryOperator.EQUAL: ir_expr.BinaryOperator.NOT_EQUAL,
                ir_expr.BinaryOperator.NOT_EQUAL: ir_expr.BinaryOperator.EQUAL,
            }.get(operator)
        if operator is None:
            return {}

        base = unsigned_type_range(referenced.type)
        if base is None:
            return {}
        minimum, maximum = base.minimum, base.maximum
        if operator is ir_expr.BinaryOperator.LESS:
            maximum = min(maximum, constant - 1)
        elif operator is ir_expr.BinaryOperator.LESS_EQUAL:
            maximum = min(maximum, constant)
        elif operator is ir_expr.BinaryOperator.GREATER:
            minimum = max(minimum, constant + 1)
        elif operator is ir_expr.BinaryOperator.GREATER_EQUAL:
            minimum = max(minimum, constant)
        elif operator is ir_expr.BinaryOperator.EQUAL:
            minimum = max(minimum, constant)
            maximum = min(maximum, constant)
        elif operator is ir_expr.BinaryOperator.NOT_EQUAL:
            if constant == minimum:
                minimum += 1
            elif constant == maximum:
                maximum -= 1
            else:
                return {}
        else:
            return {}
        if minimum > maximum:
            return {}
        return {name: ir_expr.ValueRange(minimum, maximum, "rule_guard")}

    return visit(expression)
