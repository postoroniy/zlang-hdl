from __future__ import annotations

from collections.abc import Callable

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.functional_regions import builtin_exact_add_result_type
from zlang.ir.numeric import (
    BinaryTypeRule,
    NumericTypeError,
    NumericTypeErrorReason,
    addition_rule,
    bitwise_rule,
    comparison_rule,
    multiplication_rule,
    subtraction_rule,
)
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedOverflowPolicy,
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
)
from zlang.semantic import SemanticError


@pytest.mark.parametrize("left_width", range(1, 6))
@pytest.mark.parametrize("right_width", range(1, 6))
def test_integer_rules_are_exact_for_all_small_width_pairs(
    left_width: int,
    right_width: int,
) -> None:
    widest = max(left_width, right_width)
    total = left_width + right_width

    unsigned_left = UIntType(left_width)
    unsigned_right = UIntType(right_width)
    assert addition_rule(unsigned_left, unsigned_right).result_type == UIntType(
        widest + 1
    )
    # Unsigned subtraction is deliberately modular and does not add a sign bit.
    assert subtraction_rule(
        unsigned_left, unsigned_right
    ).result_type == UIntType(widest)
    assert multiplication_rule(
        unsigned_left, unsigned_right
    ).result_type == UIntType(total)
    assert bitwise_rule(unsigned_left, unsigned_right).result_type == UIntType(
        widest
    )
    assert comparison_rule(
        unsigned_left, unsigned_right, equality=False
    ).operand_type == UIntType(widest)

    signed_left = SIntType(left_width)
    signed_right = SIntType(right_width)
    assert addition_rule(signed_left, signed_right).result_type == SIntType(
        widest + 1
    )
    assert subtraction_rule(signed_left, signed_right).result_type == SIntType(
        widest + 1
    )
    assert multiplication_rule(signed_left, signed_right).result_type == SIntType(
        total
    )
    assert bitwise_rule(signed_left, signed_right).result_type == SIntType(widest)
    assert comparison_rule(
        signed_left, signed_right, equality=False
    ).operand_type == SIntType(widest)


@pytest.mark.parametrize("fixed_type", (FixedType, UFixedType))
def test_fixed_rules_are_exhaustive_over_small_compatible_scales(
    fixed_type: type[FixedType] | type[UFixedType],
) -> None:
    for left_width in range(2, 6):
        for right_width in range(2, 6):
            for fraction in range(min(left_width, right_width)):
                left = fixed_type(
                    left_width,
                    fraction,
                    FixedOverflowPolicy.SATURATE,
                )
                right = fixed_type(right_width, fraction)
                widest = max(left_width, right_width)
                assert addition_rule(left, right).result_type == fixed_type(
                    widest + 1,
                    fraction,
                )
                subtraction_width = widest + (
                    1 if fixed_type is FixedType else 0
                )
                assert subtraction_rule(left, right).result_type == fixed_type(
                    subtraction_width,
                    fraction,
                )
                assert comparison_rule(
                    left,
                    right,
                    equality=False,
                ).operand_type == fixed_type(widest, fraction)

            for left_fraction in range(left_width):
                for right_fraction in range(right_width):
                    left = fixed_type(left_width, left_fraction)
                    right = fixed_type(right_width, right_fraction)
                    assert multiplication_rule(left, right).result_type == fixed_type(
                        left_width + right_width,
                        left_fraction + right_fraction,
                    )


@pytest.mark.parametrize(
    "rule",
    (addition_rule, subtraction_rule),
)
def test_fixed_add_sub_scale_mismatch_is_structured_and_semantically_preserved(
    rule: Callable[[HardwareType, HardwareType], BinaryTypeRule],
) -> None:
    with pytest.raises(NumericTypeError) as captured:
        rule(FixedType(8, 4), FixedType(8, 3))
    assert captured.value.reason is NumericTypeErrorReason.FRACTION_MISMATCH

    operator = "+" if rule is addition_rule else "-"
    description = "addition" if operator == "+" else "subtraction"
    with pytest.raises(
        SemanticError,
        match=(
            f"fixed-point {description} requires identical fractional widths; "
            "use explicit quantize/rescale before the operator"
        ),
    ):
        compile_source(
            "module BadScale { in a:fixed<8,4> in b:fixed<8,3> "
            f"out y:fixed<9,4> y=a{operator}b }}",
            include_clash=False,
        )


def test_bitwise_and_comparison_rules_preserve_family_boundaries() -> None:
    with pytest.raises(NumericTypeError) as captured:
        bitwise_rule(UIntType(4), BitsType(4))
    assert captured.value.reason is NumericTypeErrorReason.FAMILY_MISMATCH

    assert bitwise_rule(BitType(), BitType()).result_type == BitType()
    assert comparison_rule(
        BitsType(3), BitsType(7), equality=True
    ).operand_type == BitsType(7)
    with pytest.raises(NumericTypeError) as captured:
        comparison_rule(BitsType(3), BitsType(7), equality=False)
    assert captured.value.reason is NumericTypeErrorReason.ORDERED_BITS

    with pytest.raises(NumericTypeError) as captured:
        comparison_rule(FixedType(8, 4), FixedType(8, 3), equality=True)
    assert captured.value.reason is NumericTypeErrorReason.FRACTION_MISMATCH


def test_enum_comparison_is_nominal_and_equality_only() -> None:
    first = EnumType("First", ("A", "B"), "test::First")
    second = EnumType("Second", ("A", "B"), "test::Second")
    assert comparison_rule(first, first, equality=True).operand_type == first
    with pytest.raises(NumericTypeError) as captured:
        comparison_rule(first, second, equality=True)
    assert captured.value.reason is NumericTypeErrorReason.NOMINAL_ENUM_MISMATCH
    with pytest.raises(NumericTypeError) as captured:
        comparison_rule(first, first, equality=False)
    assert captured.value.reason is NumericTypeErrorReason.ORDERED_ENUM


def test_semantic_binary_ir_matches_the_shared_rules() -> None:
    module = compile_source(
        """
        module NumericParity {
            in a : u2
            in b : u5
            out added : u6
            out subtracted : u5
            out multiplied : u7
            out masked : u5
            out compared : bit
            added = a + b
            subtracted = a - b
            multiplied = a * b
            masked = a & b
            compared = a < b
        }
        """,
        include_clash=False,
    ).ir
    assignments = {
        assignment.target.name: assignment.expression
        for assignment in module.assignments
    }
    left = UIntType(2)
    right = UIntType(5)
    assert assignments["added"].type == addition_rule(left, right).result_type
    for name, rule in (
        ("subtracted", subtraction_rule(left, right)),
        ("multiplied", multiplication_rule(left, right)),
        ("masked", bitwise_rule(left, right)),
        ("compared", comparison_rule(left, right, equality=False)),
    ):
        expression = assignments[name]
        assert isinstance(expression, expr.Binary)
        assert expression.operand_type == rule.operand_type
        assert expression.type == rule.result_type


def test_exact_reduction_compatibility_entry_point_uses_shared_addition() -> None:
    for left_width in range(1, 6):
        for right_width in range(1, 6):
            left = UIntType(left_width)
            right = UIntType(right_width)
            assert builtin_exact_add_result_type(
                left,
                right,
            ) == addition_rule(left, right).result_type
