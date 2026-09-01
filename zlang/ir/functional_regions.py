"""Serializable compile-time regions and exact reduction schedules.

These objects describe bounded elaboration without retaining syntax AST nodes.
They are deliberately backend-independent and contain only typed semantic IR,
stable binder identities, and deterministic compile-time arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable

from zlang.ir.numeric import (
    NumericTypeError,
    NumericTypeErrorReason,
    addition_rule,
)
from zlang.ir.types import HardwareType
from zlang.source import SourceOrigin

if TYPE_CHECKING:
    from zlang.ir.expressions import Expression


class CompileTimeOperator(str, Enum):
    LITERAL = "literal"
    BINDER = "binder"
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    FLOOR_DIVIDE = "floor_divide"
    MODULO = "modulo"
    NEGATE = "negate"


class FunctionalRegionKind(str, Enum):
    GENERATE = "generate"
    MAP = "map"


@dataclass(frozen=True)
class CompileTimeBinderRef:
    """One stable compile-time iterator and its complete half-open domain."""

    identity: str
    display_name: str
    start: int
    stop: int
    origin: SourceOrigin | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.identity:
            raise ValueError("compile-time binder identity must not be empty")
        if not self.display_name:
            raise ValueError("compile-time binder display name must not be empty")
        if isinstance(self.start, bool) or not isinstance(self.start, int):
            raise ValueError("compile-time binder start must be an integer")
        if isinstance(self.stop, bool) or not isinstance(self.stop, int):
            raise ValueError("compile-time binder stop must be an integer")
        if self.stop <= self.start:
            raise ValueError("compile-time binder domain must not be empty")


@dataclass(frozen=True)
class CompileTimeExpr:
    """Small serializable integer-expression tree over bounded binders."""

    operator: CompileTimeOperator
    operands: tuple[int | CompileTimeBinderRef | "CompileTimeExpr", ...]

    def __post_init__(self) -> None:
        if not isinstance(self.operator, CompileTimeOperator):
            try:
                object.__setattr__(self, "operator", CompileTimeOperator(self.operator))
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"invalid compile-time operator {self.operator!r}"
                ) from error
        arity = {
            CompileTimeOperator.LITERAL: 1,
            CompileTimeOperator.BINDER: 1,
            CompileTimeOperator.ADD: 2,
            CompileTimeOperator.SUBTRACT: 2,
            CompileTimeOperator.MULTIPLY: 2,
            CompileTimeOperator.FLOOR_DIVIDE: 2,
            CompileTimeOperator.MODULO: 2,
            CompileTimeOperator.NEGATE: 1,
        }[self.operator]
        if len(self.operands) != arity:
            raise ValueError(
                f"compile-time operator '{self.operator.value}' requires {arity} operand(s)"
            )
        if any(
            isinstance(item, bool)
            or not isinstance(item, (int, CompileTimeBinderRef, CompileTimeExpr))
            for item in self.operands
        ):
            raise ValueError("compile-time operands must be integers or binder expressions")
        if self.operator is CompileTimeOperator.LITERAL and not isinstance(
            self.operands[0], int
        ):
            raise ValueError("compile-time literal requires one integer")
        if self.operator is CompileTimeOperator.BINDER and not isinstance(
            self.operands[0], CompileTimeBinderRef
        ):
            raise ValueError("compile-time binder expression requires one binder reference")

    @classmethod
    def literal(cls, value: int) -> "CompileTimeExpr":
        return cls(CompileTimeOperator.LITERAL, (value,))

    @classmethod
    def ref(cls, binder: CompileTimeBinderRef) -> "CompileTimeExpr":
        return cls(CompileTimeOperator.BINDER, (binder,))


CompileTimeOperand = int | CompileTimeBinderRef | CompileTimeExpr


def evaluate_compile_time(
    expression: int | CompileTimeExpr,
    bindings: dict[str, int],
) -> int:
    """Evaluate one bounded binder expression by stable binder identity."""

    if isinstance(expression, bool) or not isinstance(expression, (int, CompileTimeExpr)):
        raise ValueError("compile-time value must be an integer expression")
    if isinstance(expression, int):
        return expression
    operator = expression.operator
    if operator is CompileTimeOperator.LITERAL:
        value = expression.operands[0]
        assert isinstance(value, int)
        return value
    if operator is CompileTimeOperator.BINDER:
        binder = expression.operands[0]
        assert isinstance(binder, CompileTimeBinderRef)
        if binder.identity not in bindings:
            raise ValueError(
                f"compile-time binder '{binder.display_name}' is not bound"
            )
        value = bindings[binder.identity]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"compile-time binder '{binder.display_name}' has a non-integer value"
            )
        if not binder.start <= value < binder.stop:
            raise ValueError(
                f"compile-time binder '{binder.display_name}' escaped domain "
                f"{binder.start}..{binder.stop}"
            )
        return value
    values = tuple(
        evaluate_compile_time(_as_compile_time_expression(item), bindings)
        for item in expression.operands
    )
    if operator is CompileTimeOperator.ADD:
        return values[0] + values[1]
    if operator is CompileTimeOperator.SUBTRACT:
        return values[0] - values[1]
    if operator is CompileTimeOperator.MULTIPLY:
        return values[0] * values[1]
    if operator is CompileTimeOperator.FLOOR_DIVIDE:
        if values[1] == 0:
            raise ValueError("compile-time division by zero")
        return values[0] // values[1]
    if operator is CompileTimeOperator.MODULO:
        if values[1] == 0:
            raise ValueError("compile-time modulo by zero")
        return values[0] % values[1]
    assert operator is CompileTimeOperator.NEGATE
    return -values[0]


def compile_time_range(expression: int | CompileTimeExpr) -> tuple[int, int]:
    """Return an inclusive conservative interval for a binder expression."""

    if isinstance(expression, bool) or not isinstance(expression, (int, CompileTimeExpr)):
        raise ValueError("compile-time value must be an integer expression")
    if isinstance(expression, int):
        return expression, expression
    operator = expression.operator
    if operator is CompileTimeOperator.LITERAL:
        value = expression.operands[0]
        assert isinstance(value, int)
        return value, value
    if operator is CompileTimeOperator.BINDER:
        binder = expression.operands[0]
        assert isinstance(binder, CompileTimeBinderRef)
        return binder.start, binder.stop - 1
    ranges = tuple(
        compile_time_range(_as_compile_time_expression(item))
        for item in expression.operands
    )
    if operator is CompileTimeOperator.ADD:
        return ranges[0][0] + ranges[1][0], ranges[0][1] + ranges[1][1]
    if operator is CompileTimeOperator.SUBTRACT:
        return ranges[0][0] - ranges[1][1], ranges[0][1] - ranges[1][0]
    if operator is CompileTimeOperator.MULTIPLY:
        products = tuple(a * b for a in ranges[0] for b in ranges[1])
        return min(products), max(products)
    if operator is CompileTimeOperator.NEGATE:
        return -ranges[0][1], -ranges[0][0]
    denominator = ranges[1]
    if denominator[0] <= 0 <= denominator[1]:
        raise ValueError("compile-time divisor range may contain zero")
    if operator is CompileTimeOperator.FLOOR_DIVIDE:
        quotients = tuple(a // b for a in ranges[0] for b in denominator)
        return min(quotients), max(quotients)
    assert operator is CompileTimeOperator.MODULO
    if denominator[0] != denominator[1]:
        raise ValueError("compile-time modulo requires a constant divisor")
    divisor = denominator[0]
    if divisor <= 0:
        raise ValueError("compile-time modulo requires a positive divisor")
    return 0, divisor - 1


def _as_compile_time_expression(value: CompileTimeOperand) -> int | CompileTimeExpr:
    if isinstance(value, CompileTimeBinderRef):
        return CompileTimeExpr.ref(value)
    return value


@dataclass(frozen=True)
class FunctionalTable:
    """One typed immutable lookup table owned by a functional region."""

    name: str
    values: tuple["Expression", ...]
    type: HardwareType
    start: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("functional table name must not be empty")
        if not self.values:
            raise ValueError(f"functional table '{self.name}' must not be empty")
        if isinstance(self.start, bool) or not isinstance(self.start, int):
            raise ValueError(f"functional table '{self.name}' start must be an integer")
        if any(value.type != self.type for value in self.values):
            raise ValueError(
                f"functional table '{self.name}' values must all have type {self.type}"
            )

    @property
    def stop(self) -> int:
        return self.start + len(self.values)


class ExactReductionOperator(str, Enum):
    ADD = "+"


def builtin_exact_add_result_type(
    left: HardwareType,
    right: HardwareType,
) -> HardwareType:
    """Return the exact built-in addition type used by reduction trees.

    Exact reduction plans are serialized independently of their source
    expressions.  Re-derive the language's finite-width result type here so a
    corrupted plan cannot silently narrow an intermediate during canonical
    restoration or simulation.
    """

    try:
        return addition_rule(left, right).result_type
    except NumericTypeError as error:
        if error.reason is NumericTypeErrorReason.FRACTION_MISMATCH:
            raise ValueError(
                "built-in exact fixed-point addition requires identical "
                "fractional widths"
            ) from error
        raise ValueError(
            "built-in exact addition requires one integer or fixed-point "
            f"signedness family, got {left} and {right}"
        ) from error


@dataclass(frozen=True)
class ExactReductionCombine:
    result_type: HardwareType
    function: str | None = None
    callee_identity: str | None = None

    def __post_init__(self) -> None:
        if (self.function is None) != (self.callee_identity is None):
            raise ValueError(
                "nominal reduction combine requires both function and callee identity"
            )
        if self.function == "" or self.callee_identity == "":
            raise ValueError("nominal reduction callable identity must not be empty")


@dataclass(frozen=True)
class ExactReductionOperation:
    left_index: int
    right_index: int
    left_type: HardwareType
    right_type: HardwareType
    result_type: HardwareType
    function: str | None = None
    callee_identity: str | None = None

    def __post_init__(self) -> None:
        if self.left_index < 0 or self.right_index != self.left_index + 1:
            raise ValueError("exact reduction operands must be adjacent and ordered")
        if (self.function is None) != (self.callee_identity is None):
            raise ValueError(
                "nominal reduction operation requires function and callee identity"
            )
        if self.function == "" or self.callee_identity == "":
            raise ValueError("nominal reduction callable identity must not be empty")
        if self.function is None:
            expected = builtin_exact_add_result_type(
                self.left_type,
                self.right_type,
            )
            if self.result_type != expected:
                raise ValueError(
                    "built-in exact reduction result type must be "
                    f"{expected}, got {self.result_type}"
                )


@dataclass(frozen=True)
class ExactReductionLevel:
    """One parallel level in an exact recursive midpoint reduction tree."""

    input_types: tuple[HardwareType, ...]
    operations: tuple[ExactReductionOperation, ...]
    carry_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if len(self.input_types) < 2:
            raise ValueError("exact reduction level requires at least two inputs")
        if not self.operations:
            raise ValueError("exact reduction level must perform an operation")
        starts = tuple(operation.left_index for operation in self.operations)
        if starts != tuple(sorted(starts)) or len(starts) != len(set(starts)):
            raise ValueError("exact reduction operations must be uniquely ordered")
        consumed: set[int] = set()
        for operation in self.operations:
            if operation.right_index >= len(self.input_types):
                raise ValueError("exact reduction operation index is out of range")
            if operation.left_index in consumed or operation.right_index in consumed:
                raise ValueError("exact reduction operations must not overlap")
            if self.input_types[operation.left_index] != operation.left_type:
                raise ValueError("exact reduction left operand type does not match")
            if self.input_types[operation.right_index] != operation.right_type:
                raise ValueError("exact reduction right operand type does not match")
            consumed.update((operation.left_index, operation.right_index))
        expected_carries = tuple(
            index for index in range(len(self.input_types)) if index not in consumed
        )
        if self.carry_indices != expected_carries:
            raise ValueError(
                "exact reduction carry indices must be the ordered uncombined inputs"
            )

    @property
    def output_types(self) -> tuple[HardwareType, ...]:
        by_left = {item.left_index: item for item in self.operations}
        output: list[HardwareType] = []
        index = 0
        while index < len(self.input_types):
            operation = by_left.get(index)
            if operation is None:
                output.append(self.input_types[index])
                index += 1
            else:
                output.append(operation.result_type)
                index += 2
        return tuple(output)


@dataclass(frozen=True)
class ExactReductionPlan:
    length: int
    leaf_type: HardwareType
    root_type: HardwareType
    levels: tuple[ExactReductionLevel, ...]
    operator: ExactReductionOperator = ExactReductionOperator.ADD
    ordering: str = "balanced_source_order"

    def __post_init__(self) -> None:
        if self.length < 1:
            raise ValueError("exact reduction plan length must be positive")
        if self.operator is not ExactReductionOperator.ADD:
            raise ValueError("exact reduction fallback currently supports only add")
        if self.ordering != "balanced_source_order":
            raise ValueError("exact reduction ordering must be balanced_source_order")
        if self.length == 1:
            if self.levels or self.root_type != self.leaf_type:
                raise ValueError("single-element reduction plan must be an identity")
            return
        if not self.levels:
            raise ValueError("multi-element exact reduction plan requires levels")
        expected_types = (self.leaf_type,) * self.length
        active_spans = tuple((index, index + 1) for index in range(self.length))
        actual_joins: set[tuple[tuple[int, int], tuple[int, int]]] = set()
        for level in self.levels:
            if level.input_types != expected_types:
                raise ValueError("exact reduction level input types do not chain")
            by_left = {item.left_index: item for item in level.operations}
            next_spans: list[tuple[int, int]] = []
            index = 0
            while index < len(active_spans):
                operation = by_left.get(index)
                if operation is None:
                    next_spans.append(active_spans[index])
                    index += 1
                    continue
                left_span = active_spans[index]
                right_span = active_spans[index + 1]
                if left_span[1] != right_span[0]:
                    raise ValueError("exact reduction operation breaks source order")
                actual_joins.add((left_span, right_span))
                next_spans.append((left_span[0], right_span[1]))
                index += 2
            expected_types = level.output_types
            active_spans = tuple(next_spans)
        if expected_types != (self.root_type,) or active_spans != ((0, self.length),):
            raise ValueError("exact reduction levels do not produce the declared root")
        if actual_joins != _midpoint_joins(0, self.length):
            raise ValueError(
                "exact reduction levels do not preserve recursive midpoint topology"
            )


def _midpoint_joins(
    start: int,
    stop: int,
) -> set[tuple[tuple[int, int], tuple[int, int]]]:
    if stop - start == 1:
        return set()
    middle = start + (stop - start) // 2
    return {
        *((_midpoint_joins(start, middle))),
        *((_midpoint_joins(middle, stop))),
        ((start, middle), (middle, stop)),
    }


@dataclass(frozen=True)
class _ReductionTree:
    start: int
    stop: int
    type: HardwareType
    height: int
    left: "_ReductionTree | None" = None
    right: "_ReductionTree | None" = None
    combine: ExactReductionCombine | None = None


def build_exact_reduction_plan(
    element_type: HardwareType,
    length: int,
    combine_resolver: Callable[
        [HardwareType, HardwareType], ExactReductionCombine
    ],
) -> ExactReductionPlan:
    """Build the exact recursive-``middle = len // 2`` addition schedule."""

    if length < 1:
        raise ValueError("exact reduction length must be positive")

    def build(start: int, stop: int) -> _ReductionTree:
        if stop - start == 1:
            return _ReductionTree(start, stop, element_type, 0)
        middle = start + (stop - start) // 2
        left = build(start, middle)
        right = build(middle, stop)
        combine = combine_resolver(left.type, right.type)
        if not isinstance(combine, ExactReductionCombine):
            raise ValueError("combine resolver must return ExactReductionCombine")
        return _ReductionTree(
            start,
            stop,
            combine.result_type,
            max(left.height, right.height) + 1,
            left,
            right,
            combine,
        )

    root = build(0, length)
    if length == 1:
        return ExactReductionPlan(length, element_type, root.type, ())
    pending: dict[int, list[_ReductionTree]] = {}

    def collect(node: _ReductionTree) -> None:
        if node.left is None:
            return
        pending.setdefault(node.height, []).append(node)
        assert node.right is not None
        collect(node.left)
        collect(node.right)

    collect(root)
    active = [
        _ReductionTree(index, index + 1, element_type, 0)
        for index in range(length)
    ]
    levels: list[ExactReductionLevel] = []
    for height in sorted(pending):
        nodes = sorted(pending[height], key=lambda node: node.start)
        operations: list[ExactReductionOperation] = []
        consumed: set[int] = set()
        for node in nodes:
            assert node.left is not None and node.right is not None
            assert node.combine is not None
            left_index = next(
                (
                    index
                    for index, item in enumerate(active)
                    if item.start == node.left.start and item.stop == node.left.stop
                ),
                -1,
            )
            if left_index < 0 or left_index + 1 >= len(active):
                raise ValueError("internal exact reduction schedule is not ready")
            right = active[left_index + 1]
            if right.start != node.right.start or right.stop != node.right.stop:
                raise ValueError("internal exact reduction schedule lost topology")
            operations.append(
                ExactReductionOperation(
                    left_index,
                    left_index + 1,
                    node.left.type,
                    node.right.type,
                    node.type,
                    node.combine.function,
                    node.combine.callee_identity,
                )
            )
            consumed.update((left_index, left_index + 1))
        level = ExactReductionLevel(
            tuple(item.type for item in active),
            tuple(operations),
            tuple(index for index in range(len(active)) if index not in consumed),
        )
        levels.append(level)
        by_left = {item.left_index: item for item in operations}
        next_active: list[_ReductionTree] = []
        index = 0
        while index < len(active):
            operation = by_left.get(index)
            if operation is None:
                next_active.append(active[index])
                index += 1
                continue
            left = active[index]
            right = active[index + 1]
            node = next(
                item
                for item in nodes
                if item.start == left.start and item.stop == right.stop
            )
            next_active.append(node)
            index += 2
        active = next_active
    return ExactReductionPlan(
        length,
        element_type,
        root.type,
        tuple(levels),
    )


__all__ = [
    "CompileTimeBinderRef",
    "CompileTimeExpr",
    "CompileTimeOperator",
    "ExactReductionCombine",
    "ExactReductionLevel",
    "ExactReductionOperation",
    "ExactReductionOperator",
    "ExactReductionPlan",
    "FunctionalTable",
    "FunctionalRegionKind",
    "builtin_exact_add_result_type",
    "build_exact_reduction_plan",
    "compile_time_range",
    "evaluate_compile_time",
]
