"""Stable model, materialization, and reports for exact-value rewrites.

This module deliberately contains no egglog execution or rewrite definitions.
It is the small compiler-owned boundary shared by saturation, candidate
selection, reports, and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from zlang.ir.expressions import Expression
from zlang.ir.types import HardwareType
from zlang.opt.ir import (
    CanonicalExpression,
    EquivalenceMode,
    ExpressionOp,
    NodeCategory,
    NodeId,
    Observation,
    pure_metadata,
)
from zlang.opt.lowering import restore_expression
from zlang.opt.rewrite_spec import RewriteRegistration, RewriteRule
from zlang.source import SourceOrigin


@dataclass(frozen=True)
class Term:
    """A tree-shaped member of one typed equality class."""

    category: NodeCategory
    op: ExpressionOp
    type: HardwareType
    operands: tuple[Term, ...] = ()
    attributes: tuple[tuple[str, object], ...] = ()
    origins: tuple[SourceOrigin, ...] = field(default=(), compare=False, hash=False)

    def attribute(self, name: str) -> object:
        for key, value in self.attributes:
            if key == name:
                return value
        raise KeyError(name)


def term_constant_value(term: Term) -> int | None:
    """Return the exact integer payload of a canonical constant term."""

    if term.op is not ExpressionOp.CONSTANT:
        return None
    value = term.attribute("value")
    return value if isinstance(value, int) else None


@dataclass(frozen=True)
class EquivalenceClass:
    id: int
    mode: EquivalenceMode
    type: HardwareType
    terms: tuple[Term, ...]

    def __post_init__(self) -> None:
        if any(term.type != self.type for term in self.terms):
            raise ValueError("every equality-class term must have the same exact type")


@dataclass(frozen=True)
class CheckedValueCertificate:
    """Compiler-checked local exact-value equality, independent of egglog IDs."""

    checker_version: str
    source_identity: str
    selected_identity: str
    normal_form_identity: str
    active_rule_identities: tuple[str, ...]
    source_steps: tuple[tuple[str, str, str], ...]
    selected_steps: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class SaturationResult:
    root: NodeId
    equivalence_class: EquivalenceClass
    observations: tuple[Observation, ...]
    rules: tuple[RewriteRule, ...]
    iterations: int
    max_iterations: int
    max_terms: int
    saturated: bool
    truncated: bool
    registrations: tuple[RewriteRegistration, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    eclass_count: int = 1
    certificates: tuple[CheckedValueCertificate, ...] = ()

    @property
    def original(self) -> Term:
        return self.equivalence_class.terms[0]

    @property
    def alternatives(self) -> tuple[Term, ...]:
        return self.equivalence_class.terms[1:]


def term_to_expression(term: Term) -> Expression:
    """Materialize one equality-class member as typed semantic expression IR."""

    nodes: list[CanonicalExpression] = []
    interned: dict[Term, NodeId] = {}

    def lower(item: Term) -> NodeId:
        if item in interned:
            return interned[item]
        operands = tuple(lower(operand) for operand in item.operands)
        node_id = len(nodes)
        nodes.append(
            CanonicalExpression(
                id=node_id,
                category=item.category,
                op=item.op,
                type=item.type,
                operands=operands,
                attributes=item.attributes,
                metadata=pure_metadata(item.type),
            )
        )
        interned[item] = node_id
        return node_id

    root = lower(term)
    from zlang.opt.egraph import validate_scalar_pure_nodes

    validate_scalar_pure_nodes(tuple(nodes), root)
    return restore_expression(tuple(nodes), root)


def render_saturation(result: SaturationResult) -> str:
    """Render a stable, inspectable equality-saturation report."""

    observations = ",".join(item.value for item in result.observations)
    rules = ",".join(rule.value for rule in result.rules) or "none"
    terms = result.equivalence_class.terms
    lines = [
        f"saturation root=%{result.root}",
        f"equivalence {result.equivalence_class.mode.value} "
        f"observations={observations}",
        f"bounds max_iterations={result.max_iterations} max_terms={result.max_terms}",
        f"result saturated={str(result.saturated).lower()} "
        f"truncated={str(result.truncated).lower()} "
        f"iterations={result.iterations} eclasses={result.eclass_count} "
        f"candidates={len(terms)}",
        f"rules {rules}",
    ]
    lines.extend(
        "rewrite "
        f"{registration.identity} enabled={str(registration.enabled).lower()} "
        f"fired={str(registration.fired).lower()} "
        f"direction={registration.direction} "
        f"provenance={','.join(registration.provenance)} "
        f"guards={','.join(registration.guards) or 'none'}"
        + (f" reason={registration.reason}" if registration.reason else "")
        for registration in result.registrations
    )
    lines.append(
        "rejections "
        + ("; ".join(result.rejection_reasons) if result.rejection_reasons else "none")
    )
    lines.extend(
        f"certificate {index} status=verified checker={item.checker_version} "
        f"source={item.source_identity} selected={item.selected_identity} "
        f"normal_form={item.normal_form_identity} steps="
        f"{len(item.source_steps) + len(item.selected_steps)}"
        for index, item in enumerate(result.certificates)
    )
    lines.extend(
        [
            f"eclass {result.equivalence_class.id} type={result.equivalence_class.type}",
            f"  original {render_term(terms[0])}",
        ]
    )
    lines.extend(
        f"  alternative {index} {render_term(term)}"
        for index, term in enumerate(terms[1:], 1)
    )
    return "\n".join(lines) + "\n"


def render_term(term: Term) -> str:
    attributes = " ".join(
        f"{name}={_render_value(value)}" for name, value in term.attributes
    )
    operands = " ".join(render_term(operand) for operand in term.operands)
    contents = " ".join(item for item in (attributes, operands) if item)
    head = f"{term.op.value}:{term.type}"
    return f"({head}{(' ' + contents) if contents else ''})"


def _render_value(value: object) -> str:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return "[" + ",".join(_render_value(item) for item in value) + "]"
    return str(value)


__all__ = [
    "CheckedValueCertificate",
    "EquivalenceClass",
    "SaturationResult",
    "Term",
    "render_saturation",
    "render_term",
    "term_constant_value",
    "term_to_expression",
]
