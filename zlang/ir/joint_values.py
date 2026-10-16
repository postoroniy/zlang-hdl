# SPDX-License-Identifier: Apache-2.0
"""Lossless scalar boundary and typed arithmetic for bounded joint values.

The boundary owns only field/input correspondence. It introduces no semantic
operation, resource, timing or protocol node into the value e-graph.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from zlang.ir import expressions as e
from zlang.ir.expression_graph import ExpressionDagIndex
from zlang.ir.numeric import addition_rule, multiplication_rule, subtraction_rule
from zlang.ir.traversal import expression_children
from zlang.ir.types import BitsType, SIntType, StructType


def arithmetic(operator: str, left: e.Expression, right: e.Expression) -> e.Expression:
    rule = {"+": addition_rule, "-": subtraction_rule, "*": multiplication_rule}[
        operator
    ](left.type, right.type)
    if operator == "*":
        return e.Binary(
            e.BinaryOperator.MULTIPLY, left, right, rule.operand_type, rule.result_type
        )
    if operator == "+":
        return e.Add(left, right, rule.result_type)
    return e.Binary(
        e.BinaryOperator.SUBTRACT, left, right, rule.operand_type, rule.result_type
    )


def map_joint_value(root: e.Expression, transform) -> e.Expression:
    # This deliberately closed adapter stops at scalar field projections.
    graph = ExpressionDagIndex(
        (root,),
        children=lambda item: ()
        if isinstance(item, e.FieldAccess)
        else expression_children(item),
    )

    def rebuild(value, children):
        replacement = transform(value)
        if replacement is not None:
            return replacement
        if isinstance(value, (e.Add, e.Binary)):
            return replace(value, left=children[0], right=children[1])
        if isinstance(value, (e.Extend, e.Truncate, e.Bitcast, e.Pipeline, e.Unpack)):
            return replace(value, expression=children[0])
        if isinstance(value, e.Concat):
            return replace(value, operands=children)
        if isinstance(value, e.StructConstruct):
            return replace(
                value,
                fields=tuple(
                    (name, child)
                    for (name, _), child in zip(value.fields, children, strict=True)
                ),
            )
        if children:
            raise ValueError(f"unsupported joint-value node {type(value).__name__}")
        return value

    return graph.root_results(rebuild)[0]


@dataclass(frozen=True)
class JointValueBoundary:
    source: e.StructConstruct
    packed: e.Concat
    bindings: tuple[tuple[str, e.Expression], ...]

    def restore(self, packed: e.Expression) -> e.Expression:
        bindings = dict(self.bindings)
        restored = map_joint_value(
            packed,
            lambda item: bindings.get(item.name)
            if isinstance(item, e.InputRef)
            else None,
        )
        if isinstance(restored, e.Concat):
            return replace(
                self.source,
                fields=tuple(
                    (name, item.expression)
                    for (name, _), item in zip(
                        self.source.fields, restored.operands, strict=True
                    )
                ),
            )
        return e.Unpack(restored, self.source.type)

    def scalarize(self, value: e.Expression) -> e.Expression:
        reverse = {value: e.InputRef(name, value.type) for name, value in self.bindings}
        return map_joint_value(
            value,
            lambda item: reverse.get(item)
            if isinstance(item, (e.InputRef, e.FieldAccess))
            else None,
        )


def joint_value_boundary(root: e.Expression) -> JointValueBoundary | None:
    if not (
        isinstance(root, e.StructConstruct)
        and isinstance(root.type, StructType)
        and 2 <= len(root.fields) <= 4
    ):
        return None
    types = tuple(value.type for _, value in root.fields)
    if not all(isinstance(item, SIntType) for item in types):
        return None
    bindings: dict[e.Expression, str] = {}

    def leaf(value):
        if isinstance(value, (e.InputRef, e.FieldAccess)):
            base = value
            while isinstance(base, e.FieldAccess):
                base = base.expression
            if not isinstance(base, e.InputRef):
                raise ValueError("joint exploration requires pure input projections")
            if not isinstance(value.type, SIntType):
                raise ValueError("joint values require signed scalar input projections")
            name = bindings.setdefault(value, f"__joint_input_{len(bindings)}")
            return e.InputRef(name, value.type)
        return None

    try:
        scalar = map_joint_value(root, leaf)
    except ValueError:
        return None
    packed = e.Concat(
        tuple(
            e.Bitcast(value, BitsType(value.type.width)) for _, value in scalar.fields
        ),
        BitsType(root.type.width),
    )
    return JointValueBoundary(
        root, packed, tuple((name, value) for value, name in bindings.items())
    )
