from __future__ import annotations

from zlang.backend.expression_constant_folding import BackendConstantFolder
from zlang.ir import expressions as expr
from zlang.ir.types import BitsType, SIntType, StructField, StructType, UIntType, VecType


def _fold(expression: expr.Expression) -> expr.Expression:
    return BackendConstantFolder().fold(expression)


def _constant(value: int, width: int) -> expr.Constant:
    return expr.Constant(value, UIntType(width))


def test_constant_folder_folds_full_width_integer_arithmetic() -> None:
    u4 = UIntType(4)
    folded = _fold(expr.Add(_constant(15, 4), _constant(3, 4), u4))
    assert isinstance(folded, expr.Constant)
    assert folded.value == 2
    assert folded.type == u4

    product = _fold(expr.Binary(
        expr.BinaryOperator.MULTIPLY,
        _constant(7, 4),
        _constant(5, 4),
        u4,
        u4,
    ))
    assert isinstance(product, expr.Constant)
    assert product.value == 3


def test_constant_folder_folds_bitwise_shift_and_compare() -> None:
    u8 = UIntType(8)
    bit = UIntType(1)
    folded_and = _fold(expr.Binary(
        expr.BinaryOperator.BIT_AND,
        _constant(0b1010_1100, 8),
        _constant(0b0011_1110, 8),
        u8,
        u8,
    ))
    assert isinstance(folded_and, expr.Constant)
    assert folded_and.value == 0b0010_1100

    shifted = _fold(expr.Binary(
        expr.BinaryOperator.SHIFT_LEFT,
        _constant(0b0001_0011, 8),
        _constant(3, 8),
        u8,
        u8,
    ))
    assert isinstance(shifted, expr.Constant)
    assert shifted.value == 0b1001_1000

    signed_less = _fold(expr.Binary(
        expr.BinaryOperator.LESS,
        expr.Constant(-2, SIntType(8)),
        expr.Constant(3, SIntType(8)),
        SIntType(8),
        bit,
    ))
    assert isinstance(signed_less, expr.Constant)
    assert signed_less.value == 1


def test_constant_folder_folds_mux_switch_concat_slice_and_vector_ops() -> None:
    u8 = UIntType(8)
    vector = VecType(4, u8)

    mux = _fold(expr.Mux(_constant(0, 1), _constant(12, 8), _constant(34, 8), u8))
    assert isinstance(mux, expr.Constant)
    assert mux.value == 34

    switch = _fold(expr.Switch(
        _constant(2, 2),
        (expr.SwitchCase(1, _constant(11, 8)), expr.SwitchCase(2, _constant(22, 8))),
        _constant(33, 8),
        u8,
    ))
    assert isinstance(switch, expr.Constant)
    assert switch.value == 22

    concat = _fold(expr.Concat((_constant(0xA, 4), _constant(0x3, 4)), UIntType(8)))
    assert isinstance(concat, expr.Constant)
    assert concat.value == 0xA3

    sliced = _fold(expr.Slice(expr.Constant(0xABCD, BitsType(16)), 11, 4, u8))
    assert isinstance(sliced, expr.Constant)
    assert sliced.value == 0xBC

    vector_value = expr.Constant(0x44332211, vector)
    indexed = _fold(expr.VectorIndex(vector_value, 2, u8))
    assert isinstance(indexed, expr.Constant)
    assert indexed.value == 0x33

    updated = _fold(expr.VectorUpdate(
        vector_value,
        _constant(1, 2),
        _constant(0xAA, 8),
        4,
        expr.ValueRange(0, 3, "test"),
        vector,
    ))
    assert isinstance(updated, expr.Constant)
    assert updated.value == 0x4433AA11


def test_constant_folder_folds_single_field_struct_without_concat() -> None:
    wrapper = StructType("Wrapper", (StructField("value", UIntType(8)),))
    folded = _fold(
        expr.StructConstruct(
            "Wrapper",
            (("value", _constant(0xA5, 8)),),
            wrapper,
        )
    )

    assert isinstance(folded, expr.Constant)
    assert folded.value == 0xA5
    assert folded.type == wrapper


def test_constant_folder_preserves_runtime_expression_identity_when_unchanged() -> None:
    u8 = UIntType(8)
    runtime = expr.InputRef("value", u8)
    expression = expr.Add(runtime, _constant(2, 8), u8)

    assert BackendConstantFolder().fold(expression) is expression


def test_constant_folder_applies_safe_algebraic_identity_only_with_exact_type() -> None:
    u8 = UIntType(8)
    runtime = expr.InputRef("value", u8)
    expression = expr.Add(runtime, _constant(0, 8), u8)

    assert BackendConstantFolder().fold(expression) is runtime
