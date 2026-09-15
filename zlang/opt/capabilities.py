"""Closed optimizer capabilities and semantic rewrite barriers.

Absence from this table is an explicit fail-closed decision.  The metadata is
compiler-internal: it neither adds source traits nor changes canonical identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from zlang.ir import expressions as expr
from zlang.ir.module import Module
from zlang.opt.ir import ExpressionOp


class RewriteBarrier(str, Enum):
    WIDTH_CHANGE = "width_change"
    CARRY_GROWTH = "carry_growth"
    TRUNCATION = "truncation"
    QUANTIZATION = "quantization"
    FIXED_RESCALE = "fixed_rescale"
    ROUNDING = "rounding"
    SATURATION = "saturation"
    OVERFLOW_POLICY = "overflow_policy"
    SIGNEDNESS_CHANGE = "signedness_change"
    BIT_REINTERPRETATION = "bit_reinterpretation"
    STATE_OR_EFFECT = "state_or_effect"
    CLOCK_DOMAIN_CROSSING = "clock_domain_crossing"


class ArchitectureInterest(str, Enum):
    """Target-neutral expression shapes useful to later resource matching."""

    MULTIPLY = "multiply"
    MULTIPLY_ADD = "multiply_add"
    MULTIPLY_SUBTRACT = "multiply_subtract"
    PREADDER_MULTIPLY = "preadder_multiply"
    CONSTANT_MULTIPLY = "constant_multiply"
    BALANCED_REDUCTION = "balanced_reduction"


@dataclass(frozen=True)
class ExpressionCapability:
    pure_value: bool = False
    egraph_exact: bool = False
    schedulable_scalar: bool = False
    zero_delay_structural: bool = False
    resource_matchable: bool = False
    formal_observable: bool = False
    barriers: frozenset[RewriteBarrier] = frozenset()


_EGRAPH_VALUE = ExpressionCapability(
    pure_value=True,
    egraph_exact=True,
    schedulable_scalar=True,
)
_SCHEDULABLE_VALUE = ExpressionCapability(
    pure_value=True,
    egraph_exact=True,
    schedulable_scalar=True,
)
_SCHEDULER_ONLY_VALUE = ExpressionCapability(
    pure_value=True,
    schedulable_scalar=True,
)
_SCHEDULER_ONLY_STRUCTURAL = ExpressionCapability(
    pure_value=True,
    schedulable_scalar=True,
    zero_delay_structural=True,
)
_STRUCTURAL_VALUE = ExpressionCapability(
    pure_value=True,
    egraph_exact=True,
    schedulable_scalar=True,
    zero_delay_structural=True,
)


EXPRESSION_CAPABILITIES: dict[ExpressionOp, ExpressionCapability] = {
    ExpressionOp.INPUT: _EGRAPH_VALUE,
    ExpressionOp.PARAMETER: _EGRAPH_VALUE,
    ExpressionOp.CONSTANT: _EGRAPH_VALUE,
    ExpressionOp.ADD: ExpressionCapability(
        pure_value=True,
        egraph_exact=True,
        schedulable_scalar=True,
        resource_matchable=True,
        barriers=frozenset({RewriteBarrier.CARRY_GROWTH}),
    ),
    ExpressionOp.BINARY: ExpressionCapability(
        pure_value=True,
        egraph_exact=True,
        schedulable_scalar=True,
        resource_matchable=True,
        barriers=frozenset({RewriteBarrier.OVERFLOW_POLICY}),
    ),
    ExpressionOp.EXTEND: replace(
        _STRUCTURAL_VALUE,
        barriers=frozenset({RewriteBarrier.WIDTH_CHANGE}),
    ),
    ExpressionOp.TRUNCATE: replace(
        _STRUCTURAL_VALUE,
        barriers=frozenset(
            {RewriteBarrier.WIDTH_CHANGE, RewriteBarrier.TRUNCATION}
        ),
    ),
    ExpressionOp.FIXED_CONVERT: ExpressionCapability(
        pure_value=True,
        egraph_exact=True,
        schedulable_scalar=True,
        barriers=frozenset(
            {
                RewriteBarrier.WIDTH_CHANGE,
                RewriteBarrier.QUANTIZATION,
                RewriteBarrier.FIXED_RESCALE,
                RewriteBarrier.ROUNDING,
                RewriteBarrier.SATURATION,
                RewriteBarrier.OVERFLOW_POLICY,
            }
        ),
    ),
    ExpressionOp.MUX: _SCHEDULABLE_VALUE,
    ExpressionOp.SLICE: replace(
        _STRUCTURAL_VALUE,
        barriers=frozenset({RewriteBarrier.WIDTH_CHANGE}),
    ),
    ExpressionOp.CONCAT: _STRUCTURAL_VALUE,
    ExpressionOp.BITCAST: replace(
        _STRUCTURAL_VALUE,
        barriers=frozenset({RewriteBarrier.BIT_REINTERPRETATION}),
    ),
    # Existing exact-pipeline scheduler surface. E-graph eligibility remains
    # separately explicit, so this table does not broaden equality saturation.
    ExpressionOp.REGISTER_REF: ExpressionCapability(schedulable_scalar=True),
    ExpressionOp.ENUM_ENCODE: _SCHEDULER_ONLY_STRUCTURAL,
    ExpressionOp.ENUM_VALID: _SCHEDULER_ONLY_VALUE,
    ExpressionOp.ENUM_DECODE: _SCHEDULER_ONLY_VALUE,
    ExpressionOp.SWITCH: _SCHEDULER_ONLY_VALUE,
    ExpressionOp.FIELD: _SCHEDULER_ONLY_STRUCTURAL,
    ExpressionOp.STRUCT_CONSTRUCT: _SCHEDULER_ONLY_STRUCTURAL,
    ExpressionOp.TUPLE_CONSTRUCT: _SCHEDULER_ONLY_STRUCTURAL,
    ExpressionOp.TUPLE_PROJECT: _SCHEDULER_ONLY_STRUCTURAL,
    ExpressionOp.VECTOR_INDEX: _SCHEDULER_ONLY_STRUCTURAL,
    ExpressionOp.RUNTIME_INDEX: _SCHEDULER_ONLY_VALUE,
    ExpressionOp.VECTOR_CONCAT: _SCHEDULER_ONLY_STRUCTURAL,
    ExpressionOp.RESHAPE: _SCHEDULER_ONLY_STRUCTURAL,
    ExpressionOp.PACK: _SCHEDULER_ONLY_STRUCTURAL,
    ExpressionOp.UNPACK: _SCHEDULER_ONLY_STRUCTURAL,
}


_SEMANTIC_EXPRESSION_OPS: tuple[tuple[type[expr.Expression], ExpressionOp], ...] = (
    (expr.InputRef, ExpressionOp.INPUT),
    (expr.ParameterRef, ExpressionOp.PARAMETER),
    (expr.Constant, ExpressionOp.CONSTANT),
    (expr.RegisterRef, ExpressionOp.REGISTER_REF),
    (expr.Add, ExpressionOp.ADD),
    (expr.Binary, ExpressionOp.BINARY),
    (expr.Extend, ExpressionOp.EXTEND),
    (expr.Truncate, ExpressionOp.TRUNCATE),
    (expr.FixedConvert, ExpressionOp.FIXED_CONVERT),
    (expr.Mux, ExpressionOp.MUX),
    (expr.Switch, ExpressionOp.SWITCH),
    (expr.FieldAccess, ExpressionOp.FIELD),
    (expr.StructConstruct, ExpressionOp.STRUCT_CONSTRUCT),
    (expr.TupleConstruct, ExpressionOp.TUPLE_CONSTRUCT),
    (expr.TupleProject, ExpressionOp.TUPLE_PROJECT),
    (expr.VectorIndex, ExpressionOp.VECTOR_INDEX),
    (expr.RuntimeIndex, ExpressionOp.RUNTIME_INDEX),
    (expr.Slice, ExpressionOp.SLICE),
    (expr.Concat, ExpressionOp.CONCAT),
    (expr.VectorConcat, ExpressionOp.VECTOR_CONCAT),
    (expr.Bitcast, ExpressionOp.BITCAST),
    (expr.Reshape, ExpressionOp.RESHAPE),
    (expr.Pack, ExpressionOp.PACK),
    (expr.Unpack, ExpressionOp.UNPACK),
    (expr.EnumEncode, ExpressionOp.ENUM_ENCODE),
    (expr.EnumValid, ExpressionOp.ENUM_VALID),
    (expr.EnumDecode, ExpressionOp.ENUM_DECODE),
    (expr.Call, ExpressionOp.CALL),
    (expr.Delay, ExpressionOp.DELAY),
    (expr.Pipeline, ExpressionOp.PIPELINE),
)


def expression_capability(op: ExpressionOp) -> ExpressionCapability | None:
    """Return explicit capability metadata, or ``None`` for unsupported ops."""

    return EXPRESSION_CAPABILITIES.get(op)


def rewrite_barriers(op: ExpressionOp) -> frozenset[RewriteBarrier]:
    capability = expression_capability(op)
    if capability is None:
        return frozenset({RewriteBarrier.STATE_OR_EFFECT})
    return capability.barriers


def semantic_expression_op(value: expr.Expression) -> ExpressionOp | None:
    """Map typed semantic values to the shared capability key."""

    return next(
        (
            operation
            for expression_type, operation in _SEMANTIC_EXPRESSION_OPS
            if isinstance(value, expression_type)
        ),
        None,
    )


def module_rewrite_barriers(module: Module) -> frozenset[RewriteBarrier]:
    """Return module-level barriers that value-local rewrites cannot cross.

    CDC is represented by typed connection IR rather than an expression node.
    Keeping this query beside expression capabilities prevents optimizers and
    physical schedulers from treating the absence of a CDC expression node as
    permission to move logic through a crossing.
    """

    if (
        any(connection.crossing is not None for connection in module.connections)
        or any(memory.async_memory for memory in module.memories)
    ):
        return frozenset({RewriteBarrier.CLOCK_DOMAIN_CROSSING})
    return frozenset()


def architecture_interests(
    value: expr.Expression,
) -> frozenset[ArchitectureInterest]:
    """Return structural hints without selecting any target resource."""

    interests: set[ArchitectureInterest] = set()
    if isinstance(value, expr.Binary) and value.operator is expr.BinaryOperator.MULTIPLY:
        interests.add(ArchitectureInterest.MULTIPLY)
        if isinstance(value.left, expr.Constant) or isinstance(value.right, expr.Constant):
            interests.add(ArchitectureInterest.CONSTANT_MULTIPLY)
        if any(_is_add_or_subtract(operand) for operand in (value.left, value.right)):
            interests.add(ArchitectureInterest.PREADDER_MULTIPLY)
    if isinstance(value, expr.Add) and any(
        isinstance(operand, expr.Binary)
        and operand.operator is expr.BinaryOperator.MULTIPLY
        for operand in (value.left, value.right)
    ):
        interests.add(ArchitectureInterest.MULTIPLY_ADD)
    if (
        isinstance(value, expr.Binary)
        and value.operator is expr.BinaryOperator.SUBTRACT
        and any(
            isinstance(operand, expr.Binary)
            and operand.operator is expr.BinaryOperator.MULTIPLY
            for operand in (value.left, value.right)
        )
    ):
        interests.add(ArchitectureInterest.MULTIPLY_SUBTRACT)
    if isinstance(value, expr.Reduce) and value.operator is expr.ReductionOperator.ADD:
        interests.add(ArchitectureInterest.BALANCED_REDUCTION)
    return frozenset(interests)


def _is_add_or_subtract(value: expr.Expression) -> bool:
    return isinstance(value, expr.Add) or (
        isinstance(value, expr.Binary)
        and value.operator is expr.BinaryOperator.SUBTRACT
    )


__all__ = [
    "EXPRESSION_CAPABILITIES",
    "ArchitectureInterest",
    "ExpressionCapability",
    "RewriteBarrier",
    "architecture_interests",
    "expression_capability",
    "module_rewrite_barriers",
    "rewrite_barriers",
    "semantic_expression_op",
]
