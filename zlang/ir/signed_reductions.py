"""Typed analysis descriptors for exact ordered signed product reductions.

These records describe an already-typed arithmetic expression.  They are not a
replacement value IR and never authorize reassociation or subtraction-to-negation
rewrites.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
from hashlib import sha256

from zlang.ir import expressions as expr
from zlang.ir.numeric import (
    NumericTypeError,
    addition_rule,
    multiplication_rule,
    subtraction_rule,
)
from zlang.ir.types import (
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
)
from zlang.source import SourceOrigin


SIGNED_PRODUCT_REDUCTION_SCHEMA = "zlang-signed-product-reduction-v1"


class ProductTermSign(str, Enum):
    ADD = "add"
    SUBTRACT = "subtract"


class SignedProductJoinOperator(str, Enum):
    ADD = "add"
    SUBTRACT = "subtract"


@dataclass(frozen=True)
class SignedProductTerm:
    ordinal: int
    sign: ProductTermSign
    product_expression: expr.Binary = field(compare=False)
    product_identity: str
    product_type: HardwareType
    width: int
    fractional_scale: int
    signedness: str
    semantic_identity: str
    source_origin: SourceOrigin | None = field(default=None, compare=False)


@dataclass(frozen=True)
class SignedProductJoin:
    ordinal: int
    operator: SignedProductJoinOperator
    original_expression: expr.Expression = field(compare=False)
    left_identity: str
    right_term_identity: str
    left_type: HardwareType
    right_type: HardwareType
    result_type: HardwareType
    width: int
    fractional_scale: int
    signedness: str
    semantic_identity: str
    source_origin: SourceOrigin | None = field(default=None, compare=False)


@dataclass(frozen=True)
class SignedProductReduction:
    terms: tuple[SignedProductTerm, ...]
    joins: tuple[SignedProductJoin, ...]
    original_expression: expr.Expression = field(compare=False)
    result_type: HardwareType
    semantic_identity: str
    source_origin: SourceOrigin | None = field(default=None, compare=False)

    @property
    def has_subtraction(self) -> bool:
        return any(term.sign is ProductTermSign.SUBTRACT for term in self.terms)


def expression_semantic_identity(value: expr.Expression) -> str:
    """Stable identity for typed value semantics, excluding source provenance."""

    return sha256(_semantic_payload(value).encode()).hexdigest()


def selection_expression_semantic_identity(value: expr.Expression) -> str:
    """Identity used to join equivalent pipeline catalog expressions.

    Delay/pipeline allocation IDs are physical bookkeeping, not value
    semantics.  The semantic site key and candidate identity remain separate;
    this helper only makes the exact selected pipeline expression comparable to
    a planner catalog regenerated with a fresh allocator.
    """

    if isinstance(value, expr.Pipeline):
        value = replace(
            value,
            instance=0,
            expression=selection_expression(value.expression),
        )
    return expression_semantic_identity(value)


def selection_expression(value: expr.Expression) -> expr.Expression:
    """Normalize allocation-only pipeline IDs recursively."""

    if isinstance(value, expr.Pipeline):
        return replace(
            value,
            instance=0,
            expression=selection_expression(value.expression),
        )
    return value


def recognize_signed_product_reduction(
    value: expr.Expression,
) -> SignedProductReduction | None:
    """Recognize the frozen ordered ``product (+|-) product`` left spine.

    The original nodes and all typed intermediate boundaries remain authoritative.
    No conversion node is peeled and no expression is reconstructed.
    """

    root = _project_constructed_field(value)
    flattened = _recognize_spine(root)
    if flattened is None:
        return None
    products, join_nodes = flattened
    if len(products) < 2 or len(join_nodes) != len(products) - 1:
        return None

    terms: list[SignedProductTerm] = []
    for ordinal, (sign, product) in enumerate(products):
        product_identity = expression_semantic_identity(product)
        signedness = (
            "signed"
            if isinstance(product.type, (FixedType, SIntType))
            else "unsigned"
        )
        semantic_identity = sha256(repr((
            SIGNED_PRODUCT_REDUCTION_SCHEMA, "term", ordinal, sign.value,
            product_identity, product.type,
        )).encode()).hexdigest()
        terms.append(SignedProductTerm(
            ordinal, sign, product, product_identity, product.type,
            product.type.width, _fraction(product.type), signedness,
            semantic_identity, product.origin,
        ))

    joins: list[SignedProductJoin] = []
    left_identity = terms[0].semantic_identity
    for ordinal, node in enumerate(join_nodes):
        operator = (
            SignedProductJoinOperator.ADD
            if isinstance(node, expr.Add)
            else SignedProductJoinOperator.SUBTRACT
        )
        right_term = terms[ordinal + 1]
        semantic_identity = expression_semantic_identity(node)
        signedness = (
            "signed"
            if isinstance(node.type, (FixedType, SIntType))
            else "unsigned"
        )
        joins.append(SignedProductJoin(
            ordinal, operator, node, left_identity, right_term.semantic_identity,
            node.left.type, node.right.type, node.type, node.type.width,
            _fraction(node.type), signedness, semantic_identity, node.origin,
        ))
        left_identity = semantic_identity

    return SignedProductReduction(
        tuple(terms), tuple(joins), root, root.type,
        sha256(repr((
            SIGNED_PRODUCT_REDUCTION_SCHEMA,
            expression_semantic_identity(root),
            tuple(term.semantic_identity for term in terms),
            tuple(join.semantic_identity for join in joins),
            root.type,
        )).encode()).hexdigest(),
        root.origin,
    )


def _project_constructed_field(value: expr.Expression) -> expr.Expression:
    """Resolve only an exact field projection introduced by operator inlining."""

    if isinstance(value, expr.FieldAccess) and isinstance(value.expression, expr.StructConstruct):
        matches = tuple(
            field_value for name, field_value in value.expression.fields
            if name == value.field
        )
        if len(matches) == 1 and matches[0].type == value.type:
            return matches[0]
    return value


def _recognize_spine(value):
    if _is_full_precision_fixed_product(value):
        return [(ProductTermSign.ADD, value)], []

    operator = None
    if isinstance(value, expr.Add):
        operator = ProductTermSign.ADD
    elif isinstance(value, expr.Binary) and value.operator is expr.BinaryOperator.SUBTRACT:
        operator = ProductTermSign.SUBTRACT
    if operator is None or not _is_full_precision_fixed_product(value.right):
        return None

    left = _recognize_spine(value.left)
    if left is None:
        return None
    products, joins = left
    if not _join_is_exact(value):
        return None
    return [*products, (operator, value.right)], [*joins, value]


def _is_full_precision_fixed_product(value: expr.Expression) -> bool:
    if not (
        isinstance(value, expr.Binary)
        and value.operator is expr.BinaryOperator.MULTIPLY
        and isinstance(value.type, (FixedType, UFixedType, SIntType, UIntType))
        and type(value.left.type) is type(value.type)
        and type(value.right.type) is type(value.type)
    ):
        return False
    try:
        rule = multiplication_rule(value.left.type, value.right.type)
    except NumericTypeError:
        return False
    return value.operand_type == rule.operand_type and value.type == rule.result_type


def _join_is_exact(value: expr.Expression) -> bool:
    if not isinstance(value.type, (FixedType, UFixedType, SIntType, UIntType)):
        return False
    try:
        rule = (
            addition_rule(value.left.type, value.right.type)
            if isinstance(value, expr.Add)
            else subtraction_rule(value.left.type, value.right.type)
        )
    except NumericTypeError:
        return False
    return value.type == rule.result_type and (
        isinstance(value, expr.Add) or value.operand_type == rule.operand_type
    )


def _fraction(type_: HardwareType) -> int:
    return type_.fraction if isinstance(type_, (FixedType, UFixedType)) else 0


def _semantic_payload(value) -> str:
    if isinstance(value, tuple):
        return "(" + ",".join(_semantic_payload(item) for item in value) + ")"
    if isinstance(value, Enum):
        return f"{type(value).__module__}.{type(value).__name__}.{value.value}"
    if is_dataclass(value):
        body = ",".join(
            f"{item.name}={_semantic_payload(getattr(value, item.name))}"
            for item in fields(value)
            if item.name not in {
                "origin",
                "source_origin",
                "formal_records",
                "formal_eligible",
            }
        )
        return f"{type(value).__module__}.{type(value).__name__}({body})"
    return repr(value)


__all__ = [
    "ProductTermSign", "SIGNED_PRODUCT_REDUCTION_SCHEMA",
    "SignedProductJoin", "SignedProductJoinOperator", "SignedProductReduction",
    "SignedProductTerm", "expression_semantic_identity",
    "selection_expression", "selection_expression_semantic_identity",
    "recognize_signed_product_reduction",
]
