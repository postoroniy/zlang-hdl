"""Backend-owned typed constant folding for Direct SystemVerilog emission.

The semantic IR remains the source of truth.  This pass is deliberately local
to backend preparation: it folds only expressions whose typed operands prove the
result exactly, and it returns the original immutable expression object whenever
no change is made so existing DAG sharing survives rendering.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import fields, is_dataclass, replace

from zlang.fixed_point import quantize_rational
from zlang.ir import expressions as expr
from zlang.ir import packing
from zlang.ir.functional_regions import CompileTimeExpr, evaluate_compile_time
from zlang.ir.types import (
    FixedType,
    HardwareType,
    SIntType,
    VecType,
)


class BackendConstantFolder:
    """Fold typed constant subexpressions with module-local object memoization."""

    def __init__(self) -> None:
        self._memo: dict[int, tuple[expr.Expression, expr.Expression]] = {}

    def fold(self, expression: expr.Expression) -> expr.Expression:
        cached = self._memo.get(id(expression))
        if cached is not None and cached[0] is expression:
            return cached[1]
        folded = self._fold_uncached(expression)
        self._memo[id(expression)] = (expression, folded)
        return folded

    def _fold_uncached(self, expression: expr.Expression) -> expr.Expression:
        value = self._fold_children(expression)
        folded = self._fold_operator(value)
        return folded

    def _fold_children(self, expression: expr.Expression) -> expr.Expression:
        updates: dict[str, object] = {}
        for field in fields(expression):
            if field.name in {"type", "origin"}:
                continue
            value = getattr(expression, field.name)
            folded = self._fold_value(value)
            if folded is not value:
                updates[field.name] = folded
        if not updates:
            return expression
        return replace(expression, **updates)

    def _fold_value(self, value: object) -> object:
        if isinstance(value, expr.FunctionalRegion):
            return value
        if isinstance(value, expr.Expression):
            return self.fold(value)
        if isinstance(value, tuple):
            changed = False
            items: list[object] = []
            for item in value:
                folded = self._fold_value(item)
                changed |= folded is not item
                items.append(folded)
            return tuple(items) if changed else value
        if is_dataclass(value) and not isinstance(value, type):
            updates: dict[str, object] = {}
            for field in fields(value):
                if field.name in {"type", "origin"}:
                    continue
                item = getattr(value, field.name)
                folded = self._fold_value(item)
                if folded is not item:
                    updates[field.name] = folded
            return replace(value, **updates) if updates else value
        return value

    def _fold_operator(self, expression: expr.Expression) -> expr.Expression:
        if isinstance(expression, expr.Constant):
            return expression
        if isinstance(expression, expr.Add):
            return self._fold_add(expression)
        if isinstance(expression, expr.Binary):
            return self._fold_binary(expression)
        if isinstance(expression, (expr.Extend, expr.Truncate, expr.Bitcast, expr.Pack, expr.Unpack)):
            return self._fold_representation(expression)
        if isinstance(expression, expr.FixedConvert):
            return self._fold_fixed_convert(expression)
        if isinstance(expression, expr.Mux):
            return self._fold_mux(expression)
        if isinstance(expression, expr.Switch):
            return self._fold_switch(expression)
        if isinstance(expression, expr.EnumEncode):
            return self._fold_unary_raw(expression.expression, expression.type, expression)
        if isinstance(expression, expr.EnumValid):
            if isinstance(expression.expression, expr.Constant):
                return self._constant(
                    int(expression.enum_type.is_valid_code(expression.expression.value)),
                    expression.type,
                    expression,
                )
            return expression
        if isinstance(expression, expr.EnumDecode):
            if isinstance(expression.expression, expr.Constant):
                if expression.type.is_valid_code(expression.expression.value):
                    return self._constant(
                        expression.expression.value, expression.type, expression
                    )
                return expression.fallback
            return expression
        if isinstance(expression, expr.FieldAccess):
            return self._fold_field_access(expression)
        if isinstance(expression, expr.TupleProject):
            return self._fold_tuple_project(expression)
        if isinstance(expression, expr.UnionTag):
            if isinstance(expression.expression, expr.Constant):
                raw = _unsigned_raw(expression.expression.value, expression.expression.type)
                tag = raw >> expression.expression.type.payload_width
                return self._constant(tag, expression.type, expression)
            return expression
        if isinstance(expression, expr.UnionField):
            return self._fold_union_field(expression)
        if isinstance(expression, expr.VectorIndex):
            return self._fold_vector_index(expression)
        if isinstance(expression, expr.RuntimeIndex):
            return self._fold_runtime_index(expression)
        if isinstance(expression, expr.VectorUpdate):
            return self._fold_vector_update(expression)
        if isinstance(expression, expr.Slice):
            return self._fold_slice(expression)
        if isinstance(expression, expr.Concat):
            return self._fold_concat(expression)
        if isinstance(expression, expr.VectorConcat):
            return self._fold_vector_concat(expression)
        if isinstance(expression, expr.Reshape):
            return self._fold_unary_raw(expression.expression, expression.type, expression)
        if isinstance(expression, (expr.StructConstruct, expr.TupleConstruct, expr.UnionConstruct)):
            return self._fold_construct(expression)
        return expression

    def _fold_add(self, expression: expr.Add) -> expr.Expression:
        left = _constant_value(expression.left)
        right = _constant_value(expression.right)
        if left is not None and right is not None:
            return self._constant(left + right, expression.type, expression)
        if right == 0 and expression.left.type == expression.type:
            return expression.left
        if left == 0 and expression.right.type == expression.type:
            return expression.right
        return expression

    def _fold_binary(self, expression: expr.Binary) -> expr.Expression:
        left = _constant_value(expression.left)
        right = _constant_value(expression.right)
        if left is not None and right is not None:
            folded = _evaluate_binary(expression.operator, left, right, expression.operand_type)
            if folded is not None:
                return self._constant(folded, expression.type, expression)
        if expression.left.type == expression.type:
            if right == 0 and expression.operator in {
                expr.BinaryOperator.SUBTRACT,
                expr.BinaryOperator.BIT_OR,
                expr.BinaryOperator.BIT_XOR,
                expr.BinaryOperator.SHIFT_LEFT,
                expr.BinaryOperator.SHIFT_RIGHT,
            }:
                return expression.left
            if right == 1 and expression.operator is expr.BinaryOperator.MULTIPLY:
                return expression.left
        if expression.right.type == expression.type:
            if left == 0 and expression.operator in {
                expr.BinaryOperator.BIT_OR,
                expr.BinaryOperator.BIT_XOR,
            }:
                return expression.right
            if left == 1 and expression.operator is expr.BinaryOperator.MULTIPLY:
                return expression.right
        if expression.operator is expr.BinaryOperator.MULTIPLY and (left == 0 or right == 0):
            return self._constant(0, expression.type, expression)
        if expression.operator is expr.BinaryOperator.BIT_AND and (left == 0 or right == 0):
            return self._constant(0, expression.type, expression)
        return expression

    def _fold_representation(
        self,
        expression: expr.Extend | expr.Truncate | expr.Bitcast | expr.Pack | expr.Unpack,
    ) -> expr.Expression:
        return self._fold_unary_raw(expression.expression, expression.type, expression)

    def _fold_fixed_convert(self, expression: expr.FixedConvert) -> expr.Expression:
        if not isinstance(expression.expression, expr.Constant):
            return expression
        target_signed = isinstance(expression.type, FixedType)
        if expression.kind in {expr.FixedConversionKind.FROM_RAW, expr.FixedConversionKind.TO_RAW}:
            return self._constant(expression.expression.value, expression.type, expression)
        if expression.rational_denominator is not None:
            value = quantize_rational(
                expression.expression.value,
                expression.rational_denominator,
                fraction=expression.type.fraction,
                width=expression.type.width,
                signed=target_signed,
                rounding=expression.rounding,
                overflow=expression.overflow,
            )
            return self._constant(value, expression.type, expression)
        return expression

    def _fold_mux(self, expression: expr.Mux) -> expr.Expression:
        condition = _constant_value(expression.condition)
        if condition is None:
            return expression
        return expression.when_true if condition != 0 else expression.when_false

    def _fold_switch(self, expression: expr.Switch) -> expr.Expression:
        selector = _constant_value(expression.selector)
        if selector is None:
            return expression
        for case in expression.cases:
            if case.key == selector:
                return case.expression
        return expression.default

    def _fold_field_access(self, expression: expr.FieldAccess) -> expr.Expression:
        if isinstance(expression.expression, expr.StructConstruct):
            for name, value in expression.expression.fields:
                if name == expression.field:
                    return value
        if isinstance(expression.expression, expr.Constant):
            try:
                raw = _unsigned_raw(expression.expression.value, expression.expression.type)
                offset = expression.expression.type.width
                for field in expression.expression.type.fields:
                    offset -= field.type.width
                    if field.name == expression.field:
                        value = (raw >> offset) & packing.bit_mask(field.type.width)
                        return self._constant(value, expression.type, expression)
            except (AttributeError, packing.PackingError):
                return expression
        return expression

    def _fold_tuple_project(self, expression: expr.TupleProject) -> expr.Expression:
        if isinstance(expression.expression, expr.TupleConstruct):
            return expression.expression.elements[expression.index]
        if isinstance(expression.expression, expr.Constant):
            lsb = packing.tuple_element_lsb(expression.expression.type, expression.index)
            raw = _unsigned_raw(expression.expression.value, expression.expression.type)
            value = (raw >> lsb) & packing.bit_mask(expression.type.width)
            return self._constant(value, expression.type, expression)
        return expression

    def _fold_union_field(self, expression: expr.UnionField) -> expr.Expression:
        if isinstance(expression.expression, expr.UnionConstruct):
            if expression.expression.variant != expression.variant:
                return expression
            for name, value in expression.expression.fields:
                if name == expression.field:
                    return value
        return expression

    def _fold_vector_index(self, expression: expr.VectorIndex) -> expr.Expression:
        index = _static_index(expression.index)
        if index is None:
            return expression
        if isinstance(expression.expression, (expr.Generate, expr.Map)):
            return expression.expression.elements[index]
        if isinstance(expression.expression, expr.Constant):
            return self._fold_packed_vector_read(
                expression.expression.value,
                expression.expression.type,
                index,
                expression.type,
                expression,
            )
        return expression

    def _fold_runtime_index(self, expression: expr.RuntimeIndex) -> expr.Expression:
        index = _constant_value(expression.index)
        if index is None or not 0 <= index < expression.vector_length:
            return expression
        if isinstance(expression.expression, (expr.Generate, expr.Map)):
            return expression.expression.elements[index]
        if isinstance(expression.expression, expr.Constant):
            return self._fold_packed_vector_read(
                expression.expression.value,
                expression.expression.type,
                index,
                expression.type,
                expression,
            )
        return expression

    def _fold_packed_vector_read(
        self,
        value: int,
        vector: HardwareType,
        index: int,
        result_type: HardwareType,
        origin: expr.Expression,
    ) -> expr.Expression:
        if not isinstance(vector, VecType):
            return origin
        lsb = packing.vector_element_lsb(vector, index)
        raw = _unsigned_raw(value, vector)
        element = (raw >> lsb) & packing.bit_mask(result_type.width)
        return self._constant(element, result_type, origin)

    def _fold_vector_update(self, expression: expr.VectorUpdate) -> expr.Expression:
        source = _constant_value(expression.expression)
        index = _constant_value(expression.index)
        value = _constant_value(expression.value)
        if source is None or index is None or value is None:
            return expression
        if not isinstance(expression.type, VecType) or not 0 <= index < expression.vector_length:
            return expression
        element_width = expression.type.element_type.width
        shift = packing.vector_element_lsb(expression.type, index)
        mask = packing.bit_mask(element_width) << shift
        raw = _unsigned_raw(source, expression.type)
        updated = (raw & ~mask) | ((_unsigned_raw(value, expression.value.type) << shift) & mask)
        return self._constant(updated, expression.type, expression)

    def _fold_slice(self, expression: expr.Slice) -> expr.Expression:
        value = _constant_value(expression.expression)
        if value is None:
            return expression
        raw = _unsigned_raw(value, expression.expression.type)
        return self._constant(
            packing.slice_runtime(raw, expression.expression.type.width, expression.msb, expression.lsb),
            expression.type,
            expression,
        )

    def _fold_concat(self, expression: expr.Concat) -> expr.Expression:
        values = tuple(_constant_value(operand) for operand in expression.operands)
        if any(value is None for value in values):
            return expression
        raw = packing.concat_runtime(
            (
                _unsigned_raw(value, operand.type),
                operand.type.width,
            )
            for value, operand in zip(values, expression.operands, strict=True)
        )
        return self._constant(raw, expression.type, expression)

    def _fold_vector_concat(self, expression: expr.VectorConcat) -> expr.Expression:
        if not isinstance(expression.type, VecType):
            return expression
        values = tuple(_constant_value(operand) for operand in expression.operands)
        if any(value is None for value in values):
            return expression
        offset = 0
        raw = 0
        for value, operand in zip(values, expression.operands, strict=True):
            raw |= _unsigned_raw(value, operand.type) << offset
            offset += operand.type.width
        return self._constant(raw, expression.type, expression)

    def _fold_construct(
        self,
        expression: expr.StructConstruct | expr.TupleConstruct | expr.UnionConstruct,
    ) -> expr.Expression:
        if isinstance(expression, expr.StructConstruct):
            values = tuple(_constant_value(value) for _, value in expression.fields)
            if any(value is None for value in values):
                return expression
            raw = _aggregate_raw_bits(
                (
                    _unsigned_raw(value, value_expr.type),
                    value_expr.type.width,
                )
                for value, (_, value_expr) in zip(values, expression.fields, strict=True)
            )
            if raw is None:
                return expression
            return self._constant(raw, expression.type, expression)
        if isinstance(expression, expr.TupleConstruct):
            values = tuple(_constant_value(value) for value in expression.elements)
            if any(value is None for value in values):
                return expression
            raw = 0
            for index, (value, element) in enumerate(
                zip(values, expression.elements, strict=True)
            ):
                raw |= _unsigned_raw(value, element.type) << packing.tuple_element_lsb(
                    expression.type, index
                )
            return self._constant(raw, expression.type, expression)
        values = tuple(_constant_value(value) for _, value in expression.fields)
        if any(value is None for value in values):
            return expression
        variant = expression.type.variant(expression.variant)
        if variant is None:
            return expression
        payload = _aggregate_raw_bits(
            (
                _unsigned_raw(value, value_expr.type),
                value_expr.type.width,
            )
            for value, (_, value_expr) in zip(values, expression.fields, strict=True)
        ) if values else 0
        assert payload is not None
        padding = expression.type.payload_width - variant.payload_width
        raw = (expression.type.tag(expression.variant) << expression.type.payload_width) | (
            payload << padding
        )
        return self._constant(raw, expression.type, expression)
    def _fold_unary_raw(
        self,
        operand: expr.Expression,
        result_type: HardwareType,
        origin: expr.Expression,
    ) -> expr.Expression:
        value = _constant_value(operand)
        if value is None:
            return origin
        return self._constant(value, result_type, origin)

    def _constant(
        self,
        value: int,
        type_: HardwareType,
        origin: expr.Expression,
    ) -> expr.Constant:
        return expr.Constant(_typed_value(value, type_), type_, origin=origin.origin)


def fold_constants(expression: expr.Expression) -> expr.Expression:
    """Fold constants in *expression* with a fresh local memo table."""

    return BackendConstantFolder().fold(expression)


def _aggregate_raw_bits(parts: Iterable[tuple[int, int]]) -> int | None:
    """Pack backend aggregate fields without redefining source ``concat``.

    ``packing.concat_runtime`` deliberately rejects fewer than two operands,
    matching the source ``concat`` contract. A struct or tagged-union payload
    is an aggregate layout operation instead: one-field aggregates are legal
    and have exactly their field's raw bits, while a zero-field struct is not
    packable and therefore remains unfurled here.
    """

    packed = tuple(parts)
    if not packed:
        return None
    if len(packed) == 1:
        value, width = packed[0]
        return value & packing.bit_mask(width)
    return packing.concat_runtime(packed)


def _constant_value(expression: expr.Expression) -> int | None:
    if not isinstance(expression, expr.Constant):
        return None
    return expression.value


def _static_index(index: int | CompileTimeExpr) -> int | None:
    if isinstance(index, int):
        return index
    try:
        return evaluate_compile_time(index, {})
    except ValueError:
        return None


def _evaluate_binary(
    operator: expr.BinaryOperator,
    left: int,
    right: int,
    operand_type: HardwareType,
) -> int | None:
    left_value = _typed_value(left, operand_type)
    right_value = _typed_value(right, operand_type)
    if operator is expr.BinaryOperator.SUBTRACT:
        return left_value - right_value
    if operator is expr.BinaryOperator.MULTIPLY:
        return left_value * right_value
    if operator is expr.BinaryOperator.BIT_AND:
        return _unsigned_raw(left_value, operand_type) & _unsigned_raw(right_value, operand_type)
    if operator is expr.BinaryOperator.BIT_OR:
        return _unsigned_raw(left_value, operand_type) | _unsigned_raw(right_value, operand_type)
    if operator is expr.BinaryOperator.BIT_XOR:
        return _unsigned_raw(left_value, operand_type) ^ _unsigned_raw(right_value, operand_type)
    if operator is expr.BinaryOperator.SHIFT_LEFT:
        return _unsigned_raw(left_value, operand_type) << max(0, right_value)
    if operator is expr.BinaryOperator.SHIFT_RIGHT:
        if right_value < 0:
            return None
        return left_value >> right_value if _is_signed(operand_type) else (
            _unsigned_raw(left_value, operand_type) >> right_value
        )
    if operator is expr.BinaryOperator.EQUAL:
        return int(left_value == right_value)
    if operator is expr.BinaryOperator.NOT_EQUAL:
        return int(left_value != right_value)
    if operator is expr.BinaryOperator.LESS:
        return int(left_value < right_value)
    if operator is expr.BinaryOperator.LESS_EQUAL:
        return int(left_value <= right_value)
    if operator is expr.BinaryOperator.GREATER:
        return int(left_value > right_value)
    if operator is expr.BinaryOperator.GREATER_EQUAL:
        return int(left_value >= right_value)
    return None


def _typed_value(value: int, type_: HardwareType) -> int:
    raw = _unsigned_raw(value, type_)
    if _is_signed(type_) and raw >= (1 << (type_.width - 1)):
        return raw - (1 << type_.width)
    return raw


def _unsigned_raw(value: int, type_: HardwareType) -> int:
    return value & packing.bit_mask(type_.width)


def _is_signed(type_: HardwareType) -> bool:
    return isinstance(type_, (SIntType, FixedType))


__all__ = ["BackendConstantFolder", "fold_constants"]
