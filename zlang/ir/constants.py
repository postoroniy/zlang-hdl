"""Fail-closed extraction of specialization-time typed values.

The helper deliberately accepts only the small expression shapes that can own
ROM contents.  It is shared by semantic validation, simulation, and companion
image emission so aggregate layout is not rediscovered independently by each
consumer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


from zlang.ir import expressions as expr
from zlang.ir import packing
from zlang.ir.runtime_values import TaggedUnionValue
from zlang.ir.functional import FunctionalLoweringError, vector_leaf_shape
from zlang.ir.functional_regions import FunctionalTable, evaluate_compile_time
from zlang.ir.types import HardwareType, StructType, TupleType, VecType


class ConstantExpressionError(ValueError):
    """A typed expression is not a recursively specialization-time constant."""


def constant_runtime_value(expression: expr.Expression) -> object:
    """Return the immutable runtime value represented by *expression*.

    Literal scalars, concrete aggregates, and exact representation/collection
    operations over those values are admitted. Calls and value references are
    rejected even if a caller happens to know their current value; semantic
    elaboration must first specialize them into one of these concrete shapes.
    """

    return _constant_runtime_value(expression, {}, {}, {})


def _constant_runtime_value(
    expression: expr.Expression,
    binder_values: dict[str, int],
    captures: dict[str, object],
    tables: dict[str, FunctionalTable],
) -> object:
    """Evaluate one constant expression in a compact-region environment."""

    def evaluate(value: expr.Expression) -> object:
        return _constant_runtime_value(value, binder_values, captures, tables)

    if isinstance(expression, expr.Constant):
        return expression.value
    if isinstance(expression, expr.EnumEncode):
        return evaluate(expression.expression)
    if isinstance(expression, expr.EnumValid):
        return int(expression.enum_type.is_valid_code(evaluate(expression.expression)))
    if isinstance(expression, expr.EnumDecode):
        raw = evaluate(expression.expression)
        return (
            raw
            if expression.type.is_valid_code(raw)
            else evaluate(expression.fallback)
        )
    if isinstance(expression, expr.FunctionalCaptureRef):
        try:
            return captures[expression.identity]
        except KeyError as error:
            raise ConstantExpressionError(
                f"functional capture '{expression.display_name}' is not bound"
            ) from error
    if isinstance(expression, expr.FunctionalTableLookup):
        table = tables.get(expression.table_name)
        if table is None or table.type != expression.type:
            raise ConstantExpressionError(
                f"functional table '{expression.table_name}' is not bound exactly"
            )
        try:
            index = evaluate_compile_time(expression.index, binder_values)
        except ValueError as error:
            raise ConstantExpressionError(str(error)) from error
        offset = index - table.start
        if not 0 <= offset < len(table.values):
            raise ConstantExpressionError(
                f"functional table index {index} is outside "
                f"{table.start}..{table.stop}"
            )
        return evaluate(table.values[offset])
    if isinstance(expression, expr.FunctionalRegion):
        region_captures = dict(captures)
        for reference, value in expression.captures:
            region_captures[reference.identity] = evaluate(value)
        region_tables = {**tables, **{table.name: table for table in expression.tables}}
        result: list[object] = []
        for index in range(expression.binder.start, expression.binder.stop):
            iteration_binders = {
                **binder_values,
                expression.binder.identity: index,
            }
            result.append(
                _constant_runtime_value(
                    expression.template,
                    iteration_binders,
                    region_captures,
                    region_tables,
                )
            )
        return tuple(result)
    if isinstance(expression, expr.StructConstruct):
        return {
            name: evaluate(value)
            for name, value in expression.fields
        }
    if isinstance(expression, expr.TupleConstruct):
        return tuple(evaluate(value) for value in expression.elements)
    if isinstance(expression, expr.TupleProject):
        value = evaluate(expression.expression)
        if not isinstance(value, tuple):
            raise ConstantExpressionError(
                "constant tuple projection source is not a tuple"
            )
        return value[expression.index]
    if isinstance(expression, expr.UnionConstruct):
        return TaggedUnionValue(
            expression.type,
            expression.variant,
            tuple((name, evaluate(value)) for name, value in expression.fields),
        )
    if isinstance(expression, (expr.Generate, expr.Map)):
        return tuple(evaluate(element) for element in expression.elements)
    if isinstance(expression, expr.VectorIndex):
        vector = evaluate(expression.expression)
        if not isinstance(expression.expression.type, VecType):
            raise ConstantExpressionError(
                "constant vector index source is not a vector"
            )
        items = _vector_items(
            expression.expression.type,
            vector,
            "vector index",
        )
        try:
            index = (
                expression.index
                if isinstance(expression.index, int)
                else evaluate_compile_time(expression.index, binder_values)
            )
        except ValueError as error:
            raise ConstantExpressionError(str(error)) from error
        if not 0 <= index < len(items):
            raise ConstantExpressionError(
                f"constant vector index {index} is out of range"
            )
        return items[index]
    if isinstance(expression, expr.VectorUpdate):
        vector = evaluate(expression.expression)
        items = list(_vector_items(expression.type, vector, "vector update"))
        index = evaluate(expression.index)
        if not isinstance(index, int) or not 0 <= index < expression.vector_length:
            raise ConstantExpressionError(
                f"constant vector update index {index!r} is out of range"
            )
        items[index] = evaluate(expression.value)
        return tuple(items)
    if isinstance(expression, expr.Slice):
        value = evaluate(expression.expression)
        try:
            source_width = packing.packed_width(expression.expression.type)
            raw = packing.pack_runtime(expression.expression.type, value)
            return packing.slice_runtime(
                raw, source_width, expression.msb, expression.lsb
            )
        except packing.PackingError as error:
            raise _packing_error("slice", error) from error
    if isinstance(expression, expr.Concat):
        try:
            parts = tuple(
                (
                    packing.pack_runtime(
                        operand.type, evaluate(operand)
                    ),
                    packing.packed_width(operand.type),
                )
                for operand in expression.operands
            )
            return packing.concat_runtime(parts)
        except packing.PackingError as error:
            raise _packing_error("concat", error) from error
    if isinstance(expression, (expr.Bitcast, expr.Pack, expr.Unpack)):
        value = evaluate(expression.expression)
        operation = type(expression).__name__.lower()
        try:
            source_width = packing.packed_width(expression.expression.type)
            target_width = packing.packed_width(expression.type)
            if source_width != target_width:
                raise ConstantExpressionError(
                    f"constant {operation} requires equal packed widths, got "
                    f"{source_width} and {target_width}"
                )
            raw = packing.pack_runtime(expression.expression.type, value)
            result = packing.unpack_runtime(expression.type, raw)
            return _freeze_runtime(expression.type, result)
        except packing.PackingError as error:
            raise _packing_error(operation, error) from error
    if isinstance(expression, expr.VectorConcat):
        if not isinstance(expression.type, VecType):
            raise ConstantExpressionError(
                "constant vector concat result is not a vector"
            )
        result: list[object] = []
        for operand in expression.operands:
            if (
                not isinstance(operand.type, VecType)
                or operand.type.element_type != expression.type.element_type
            ):
                raise ConstantExpressionError(
                    "constant vector concat has incompatible operand type "
                    f"{operand.type} for result {expression.type}"
                )
            value = evaluate(operand)
            items = _vector_items(operand.type, value, "vector concat")
            result.extend(items)
        if len(result) != expression.type.length:
            raise ConstantExpressionError(
                "constant vector concat produced "
                f"{len(result)} elements for {expression.type}"
            )
        return tuple(result)
    if isinstance(expression, expr.Reshape):
        source_type = expression.expression.type
        target_type = expression.type
        if not isinstance(source_type, VecType) or not isinstance(target_type, VecType):
            raise ConstantExpressionError(
                "constant reshape requires vector source and target types"
            )
        try:
            source_count, source_leaf = vector_leaf_shape(source_type)
            target_count, target_leaf = vector_leaf_shape(target_type)
        except FunctionalLoweringError as error:
            raise ConstantExpressionError(str(error)) from error
        if source_count != target_count or source_leaf != target_leaf:
            raise ConstantExpressionError(
                "constant reshape must preserve exact leaf count and type"
            )
        value = evaluate(expression.expression)
        leaves = _flatten_vector(source_type, value)
        result, consumed = _rebuild_vector(target_type, leaves, 0)
        if consumed != len(leaves):
            raise ConstantExpressionError(
                "constant reshape did not consume every source leaf"
            )
        return result
    raise ConstantExpressionError(
        "ROM initializer contains a runtime or unsupported expression "
        f"{type(expression).__name__}"
    )


def _packing_error(operation: str, error: packing.PackingError) -> ConstantExpressionError:
    return ConstantExpressionError(
        f"constant {operation} is not a legal packed representation: {error}"
    )


def _vector_items(
    type_: VecType, value: object, operation: str
) -> tuple[object, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != type_.length
    ):
        raise ConstantExpressionError(
            f"constant {operation} value does not contain exactly "
            f"{type_.length} elements"
        )
    return tuple(value)


def _flatten_vector(type_: VecType, value: object) -> list[object]:
    items = _vector_items(type_, value, "reshape source")
    if not isinstance(type_.element_type, VecType):
        return list(items)
    leaves: list[object] = []
    for item in items:
        leaves.extend(_flatten_vector(type_.element_type, item))
    return leaves


def _rebuild_vector(
    type_: VecType, leaves: list[object], offset: int
) -> tuple[tuple[object, ...], int]:
    if isinstance(type_.element_type, VecType):
        result: list[object] = []
        for _ in range(type_.length):
            item, offset = _rebuild_vector(type_.element_type, leaves, offset)
            result.append(item)
        return tuple(result), offset
    end = offset + type_.length
    if end > len(leaves):
        raise ConstantExpressionError(
            "constant reshape target requires more leaves than its source"
        )
    return tuple(leaves[offset:end]), end


def _freeze_runtime(type_: HardwareType, value: object) -> object:
    """Use deterministic immutable vectors for constant aggregate values."""

    if isinstance(type_, VecType):
        items = _vector_items(type_, value, "bitcast result")
        return tuple(
            _freeze_runtime(type_.element_type, item) for item in items
        )
    if isinstance(type_, StructType):
        if not isinstance(value, Mapping):
            raise ConstantExpressionError(
                f"constant bitcast result does not match struct {type_}"
            )
        expected = {field.name for field in type_.fields}
        if set(value) != expected:
            raise ConstantExpressionError(
                f"constant bitcast result does not contain exactly {expected}"
            )
        return {
            field.name: _freeze_runtime(field.type, value[field.name])
            for field in type_.fields
        }
    if isinstance(type_, TupleType):
        if not isinstance(value, tuple) or len(value) != len(type_.elements):
            raise ConstantExpressionError(
                f"constant bitcast result does not match tuple {type_}"
            )
        return tuple(
            _freeze_runtime(element_type, element)
            for element_type, element in zip(
                type_.elements, value, strict=True
            )
        )
    return value


__all__ = ["ConstantExpressionError", "constant_runtime_value"]
