"""Proof-only admission of exact typed egglog value alternatives.

This checker never enumerates architecture/value candidates or runs egglog. It
normalizes only through the active, frozen local equalities and replays the
local transformations independently of the e-graph extraction result.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from zlang.ir.expressions import BinaryOperator
from zlang.ir.module import EquivalenceRule
from zlang.ir.types import BitType, FixedType, SIntType, UFixedType, UIntType
from zlang.opt.ir import ExpressionOp, NodeCategory
from zlang.opt.rewrite_guards import PatternValue, guard_holds
from zlang.opt.rewrite_model import (
    CheckedValueCertificate, Term, render_term, term_constant_value,
    term_to_expression,
)
from zlang.opt.rewrite_spec import RewriteRule, TypedRewriteSpec


CHECKER_VERSION = "exact-local-value-v1"


class ValueCertificateError(ValueError):
    """An extracted typed candidate has no replayable exact-value proof."""


def _identity(term: Term) -> str:
    return sha256(render_term(term).encode()).hexdigest()


def _source_guard(
    spec: TypedRewriteSpec,
    rule: EquivalenceRule,
    value: Term,
    condition: Term | None,
) -> bool:
    if not spec.identity.startswith(f"source.{rule.name}."):
        return False
    bindings = dict(rule.bindings)
    values = {bindings["value"]: PatternValue(value.type, term_constant_value(value))}
    if "condition" in bindings:
        if condition is None:
            return False
        values[bindings["condition"]] = PatternValue(
            condition.type, term_constant_value(condition),
        )
    return all(guard_holds(guard, values) for guard in rule.guards)


def _allowed(
    spec: TypedRewriteSpec,
    value: Term,
    condition: Term | None,
    source_rules: tuple[EquivalenceRule, ...],
) -> bool:
    if not spec.identity.startswith("source."):
        return True
    return any(
        _source_guard(spec, rule, value, condition) for rule in source_rules
    )


def _once(
    term: Term,
    spec: TypedRewriteSpec,
    source_rules: tuple[EquivalenceRule, ...],
) -> Term | None:
    operands = term.operands

    def zero(item: Term) -> bool:
        return term_constant_value(item) == 0

    if term.op is ExpressionOp.ADD and len(operands) == 2:
        left, right = operands
        if spec.rule is RewriteRule.ADD_ZERO:
            if zero(right) and left.type == term.type:
                return left
            if zero(left) and right.type == term.type:
                return right
        if spec.rule is RewriteRule.ADD_COMMUTE and left.type == right.type and render_term(left) > render_term(right):
            return replace(term, operands=(right, left))
    if term.op is ExpressionOp.BINARY and len(operands) == 2:
        left, right = operands
        operator = term.attribute("operator")
        if operator is BinaryOperator.SUBTRACT and spec.rule is RewriteRule.SUBTRACT_ZERO:
            if zero(right) and left.type == term.type:
                return left
        if operator is BinaryOperator.MULTIPLY:
            if spec.rule is RewriteRule.MULTIPLY_ZERO and (zero(left) or zero(right)):
                return Term(NodeCategory.VALUE, ExpressionOp.CONSTANT, term.type, attributes=(("value", 0),))
            if spec.rule is RewriteRule.MULTIPLY_ONE and not isinstance(term.type, (FixedType, UFixedType)):
                if term_constant_value(right) == 1 and left.type == term.type:
                    return left
                if term_constant_value(left) == 1 and right.type == term.type:
                    return right
            if spec.rule is RewriteRule.MULTIPLY_POWER_OF_TWO and isinstance(term.type, (UIntType, SIntType)):
                for value, constant in ((left, right), (right, left)):
                    factor = term_constant_value(constant)
                    if factor is None or factor <= 1 or factor & (factor - 1):
                        continue
                    if type(value.type) is not type(term.type) or value.type.width > term.type.width:
                        continue
                    shift = factor.bit_length() - 1
                    widened = value if value.type == term.type else Term(
                        NodeCategory.VALUE, ExpressionOp.EXTEND, term.type, (value,),
                    )
                    amount = Term(
                        NodeCategory.VALUE, ExpressionOp.CONSTANT,
                        UIntType(max(1, shift.bit_length())),
                        attributes=(("value", shift),),
                    )
                    return Term(
                        NodeCategory.VALUE, ExpressionOp.BINARY, term.type,
                        (widened, amount),
                        (("operator", BinaryOperator.SHIFT_LEFT), ("operand_type", term.type)),
                    )
            if spec.rule is RewriteRule.MULTIPLY_COMMUTE and left.type == right.type and render_term(left) > render_term(right):
                return replace(term, operands=(right, left))
        if operator in {BinaryOperator.BIT_OR, BinaryOperator.BIT_XOR} and zero(right):
            desired = RewriteRule.BIT_OR_ZERO if operator is BinaryOperator.BIT_OR else RewriteRule.BIT_XOR_ZERO
            if spec.rule is desired and left.type == term.type and _allowed(spec, left, None, source_rules):
                return left
        if operator in {BinaryOperator.SHIFT_LEFT, BinaryOperator.SHIFT_RIGHT} and zero(right):
            if spec.rule is RewriteRule.SHIFT_ZERO and left.type == term.type and _allowed(spec, left, None, source_rules):
                return left
    if term.op is ExpressionOp.MUX and len(operands) == 3:
        condition, yes, no = operands
        if spec.rule is RewriteRule.MUX_IDENTITY and yes == no and _allowed(spec, yes, condition, source_rules):
            return yes
        if spec.rule is RewriteRule.MUX_CONSTANT:
            constant = term_constant_value(condition)
            if isinstance(condition.type, BitType) and constant in {0, 1}:
                return yes if constant else no
    if term.op in {ExpressionOp.EXTEND, ExpressionOp.TRUNCATE} and len(operands) == 1:
        if spec.rule is RewriteRule.RESIZE_IDENTITY and operands[0].type == term.type:
            return operands[0]
    return None


def _normal_form(
    root: Term,
    specs: tuple[TypedRewriteSpec, ...],
    source_rules: tuple[EquivalenceRule, ...],
) -> tuple[Term, tuple[tuple[str, str, str], ...]]:
    steps: list[tuple[str, str, str]] = []

    def visit(term: Term) -> Term:
        current = replace(term, operands=tuple(visit(child) for child in term.operands))
        for _ in range(8):
            for spec in specs:
                changed = _once(current, spec, source_rules)
                if changed is None or changed == current:
                    continue
                # Every local step must remain in the closed typed value set.
                term_to_expression(changed)
                steps.append((spec.identity, render_term(current), render_term(changed)))
                current = changed
                break
            else:
                return current
        raise ValueCertificateError("local value proof exceeded eight steps at one node")

    return visit(root), tuple(steps)


def checked_value_certificate(
    source: Term,
    candidate: Term,
    active_specs: tuple[TypedRewriteSpec, ...],
    source_rules: tuple[EquivalenceRule, ...],
) -> CheckedValueCertificate:
    """Build and immediately independently replay a candidate certificate."""
    term_to_expression(source)
    term_to_expression(candidate)
    specs = tuple(sorted(active_specs, key=lambda item: item.identity))
    source_form, source_steps = _normal_form(source, specs, source_rules)
    selected_form, selected_steps = _normal_form(candidate, specs, source_rules)
    if render_term(source_form) != render_term(selected_form):
        raise ValueCertificateError(
            "typed candidate has no exact local-rewrite proof of source equality"
        )
    certificate = CheckedValueCertificate(
        CHECKER_VERSION, _identity(source), _identity(candidate),
        _identity(source_form), tuple(spec.identity for spec in specs),
        source_steps, selected_steps,
    )
    verify_checked_value_certificate(
        certificate, source, candidate, specs, source_rules,
    )
    return certificate


def verify_checked_value_certificate(
    certificate: CheckedValueCertificate,
    source: Term,
    candidate: Term,
    active_specs: tuple[TypedRewriteSpec, ...],
    source_rules: tuple[EquivalenceRule, ...],
) -> None:
    """Replay the proof without consuming egglog's reported e-class relation."""
    specs = tuple(sorted(active_specs, key=lambda item: item.identity))
    if certificate.checker_version != CHECKER_VERSION or (
        certificate.source_identity, certificate.selected_identity,
        certificate.active_rule_identities,
    ) != (_identity(source), _identity(candidate), tuple(item.identity for item in specs)):
        raise ValueCertificateError("value certificate identity or checker version mismatch")
    source_form, source_steps = _normal_form(source, specs, source_rules)
    selected_form, selected_steps = _normal_form(candidate, specs, source_rules)
    if (
        render_term(source_form) != render_term(selected_form)
        or certificate.normal_form_identity != _identity(source_form)
        or certificate.source_steps != source_steps
        or certificate.selected_steps != selected_steps
    ):
        raise ValueCertificateError("value certificate local replay failed")


__all__ = [
    "CHECKER_VERSION", "ValueCertificateError", "checked_value_certificate",
    "verify_checked_value_certificate",
]
