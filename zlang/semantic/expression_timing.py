# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Cycle-latency provenance and alignment validation for typed expressions."""

from __future__ import annotations

from zlang.ir import expressions as ir_expr
from zlang.ir import traversal as ir_traversal

from .errors import SemanticError


def expression_latency(expression: ir_expr.Expression) -> int | None:
    if isinstance(expression, ir_expr.Constant):
        return None
    if isinstance(
        expression,
        (
            ir_expr.InputRef,
            ir_expr.ParameterRef,
            ir_expr.FunctionalCaptureRef,
            ir_expr.FunctionalValue,
            ir_expr.FunctionalTableLookup,
            ir_expr.RegisterRef,
            ir_expr.ReadyValidRef,
            ir_expr.CreditRef,
            ir_expr.PacketRef,
            ir_expr.VirtualChannelCreditRef,
            ir_expr.RequestResponseRef,
            ir_expr.FifoRef,
            ir_expr.MemoryRef,
            ir_expr.RomRef,
        ),
    ):
        return 0
    if isinstance(expression, ir_expr.InstanceOutputRef):
        return 0
    if isinstance(
        expression,
        (
            ir_expr.EnumEncode,
            ir_expr.EnumValid,
            ir_expr.UnionTag,
            ir_expr.UnionField,
            ir_expr.Extend,
            ir_expr.Truncate,
            ir_expr.FixedConvert,
            ir_expr.FieldAccess,
            ir_expr.TupleProject,
            ir_expr.VectorIndex,
            ir_expr.Slice,
            ir_expr.Bitcast,
            ir_expr.Reshape,
            ir_expr.Pack,
            ir_expr.Unpack,
        ),
    ):
        return expression_latency(expression.expression)
    if isinstance(expression, ir_expr.Delay):
        return (expression_latency(expression.expression) or 0) + expression.cycles
    if isinstance(expression, ir_expr.Pipeline):
        return (expression_latency(expression.expression) or 0) + expression.stages
    if isinstance(expression, ir_expr.FunctionalRegion):
        children = (
            *(value for table in expression.tables for value in table.values),
            *(value for _, value in expression.captures),
        )
        description = f"functional {expression.kind.value}"
    elif isinstance(expression, ir_expr.Dot):
        children = expression.products
        description = "dot"
    elif isinstance(expression, ir_expr.Reduce):
        return expression_latency(expression.collection)
    elif isinstance(expression, ir_expr.ImplementationChoice):
        # Semantic validation proves every implementation arm has identical
        # cycle timing before an automatic policy is extracted.
        return expression.alternatives[0].semantics.latency
    else:
        children = ir_traversal.expression_children(expression)
        if isinstance(expression, ir_expr.Binary):
            description = expression.operator.value
        elif isinstance(expression, ir_expr.Add):
            description = "+"
        elif isinstance(expression, ir_expr.Call):
            description = f"call to {expression.function}"
        elif isinstance(expression, (ir_expr.Generate, ir_expr.Map)):
            description = type(expression).__name__.lower()
        else:
            description = _LATENCY_DESCRIPTIONS.get(type(expression))
        if description is None:
            raise SemanticError(f"cannot determine latency of {expression!r}")
    return _aligned_latency(
        description,
        tuple(expression_latency(value) for value in children),
    )


def _aligned_latency(description: str, latencies: tuple[int | None, ...]) -> int | None:
    concrete = {latency for latency in latencies if latency is not None}
    if len(concrete) > 1:
        rendered = ", ".join(str(latency) for latency in sorted(concrete))
        raise SemanticError(
            f"latency mismatch in {description}: operands have latencies {rendered}",
            code="ZL-TIMING-MISMATCH",
            fixes=("align operands explicitly before combining them",),
        )
    return next(iter(concrete), None)

_LATENCY_DESCRIPTIONS = {
    ir_expr.EnumDecode: "enum decode", ir_expr.Concat: "concat", ir_expr.VectorConcat: "concat",
    ir_expr.RuntimeIndex: "runtime index", ir_expr.VectorUpdate: "vector update", ir_expr.Mux: "mux",
    ir_expr.Switch: "switch", ir_expr.StructConstruct: "struct construction",
    ir_expr.TupleConstruct: "tuple construction", ir_expr.UnionConstruct: "tagged-union construction",
}
