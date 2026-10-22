"""Independent exhaustive scalar/bitwise constant-predicate boundary checks."""

from itertools import product
import operator

import pytest

from zlang.ir import expressions as expr
from zlang.ir.constants import constant_runtime_value, ConstantExpressionError
from zlang.ir.types import BitType, BitsType, UIntType, SIntType, FixedType, UFixedType, VecType


@pytest.mark.parametrize("type_,values", (
    (BitType(), range(2)), (BitsType(3), range(8)), (UIntType(3), range(8)),
    (SIntType(3), range(-4, 4)), (FixedType(4, 2), range(-8, 8)),
    (UFixedType(4, 2), range(16)),
))
def test_constant_comparisons_exhaustive(type_, values):
    for left, right in product(values, repeat=2):
        for operation, expected in (
            (expr.BinaryOperator.EQUAL, operator.eq), (expr.BinaryOperator.NOT_EQUAL, operator.ne),
            (expr.BinaryOperator.LESS, operator.lt), (expr.BinaryOperator.LESS_EQUAL, operator.le),
            (expr.BinaryOperator.GREATER, operator.gt), (expr.BinaryOperator.GREATER_EQUAL, operator.ge),
        ):
            value = expr.Binary(operation, expr.Constant(left, type_), expr.Constant(right, type_), type_, BitType())
            assert constant_runtime_value(value) == int(expected(left, right))


@pytest.mark.parametrize("operation,combine", ((expr.ReductionOperator.BIT_AND, operator.and_),
    (expr.ReductionOperator.BIT_OR, operator.or_), (expr.ReductionOperator.BIT_XOR, operator.xor)))
@pytest.mark.parametrize("signed", (False, True))
def test_constant_bitwise_reductions_exhaustive(operation, combine, signed):
    type_ = SIntType(3) if signed else BitsType(3)
    for values in product(range(-4, 4) if signed else range(8), repeat=3):
        collection = expr.Generate("literal", 0, 3, tuple(expr.Constant(v, type_) for v in values), VecType(3, type_))
        value = expr.Reduce(operation, collection, type_)
        expected = combine(combine(values[0], values[1]), values[2]) & 7
        if signed and expected >= 4:
            expected -= 8
        assert constant_runtime_value(value) == expected


def test_constant_predicate_never_accepts_runtime_guard_even_if_result_cancels():
    value = expr.Binary(expr.BinaryOperator.BIT_AND, expr.Constant(0, BitType()),
                        expr.InputRef("runtime", BitType()), BitType(), BitType())
    with pytest.raises(ConstantExpressionError):
        constant_runtime_value(value)
