# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Type and refine conditional actions with one pair of caches per rule."""

from __future__ import annotations

from dataclasses import replace
from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr, module as ir_module, types as ir_types

from . import callables as semantic_callables
from . import context as semantic_context
from . import (
    expression_domains,
    expression_operators,
    expression_origins,
    expression_ranges,
    expression_support,
)
from .errors import SemanticError


class ConditionalRuleActivations:
    """A source guard shared by branch paths allocates its physical delays once."""

    def __init__(
        self,
        *,
        rule_name: str,
        rule_domain: str | None,
        rule_context: semantic_context.ExpressionContext,
        module_context: semantic_context.ExpressionContext,
        value_symbols: dict[str, object],
        symbols: dict[str, ir_module.Port],
        resource_symbols: dict[str, object],
        register_symbols: dict[str, ir_module.Register],
        guard_analysis: ir_expr.Expression,
    ) -> None:
        self._rule_name = rule_name
        self._rule_domain = rule_domain
        self._rule_context = rule_context
        self._module_context = module_context
        self._value_symbols = value_symbols
        self._symbols = symbols
        self._resource_symbols = resource_symbols
        self._register_symbols = register_symbols
        self._guard_analysis = guard_analysis
        self._activation_cache: dict[
            tuple[tuple[int, bool], ...],
            tuple[ir_expr.Expression, semantic_context.ExpressionContext],
        ] = {}
        # One source conditional is shared by its true and false branch paths.
        # Type it once so a stateful guard expression (for example ``delay``)
        # denotes one physical expression instance rather than being allocated
        # independently for each effect below the branch.
        self._typed_condition_cache: dict[int, ir_expr.Expression] = {}

    def typed_activation_and_context(
        self,
        conditions: tuple[tuple[ast.Expression, bool], ...],
    ) -> tuple[ir_expr.Expression | None, semantic_context.ExpressionContext]:
        if not conditions:
            return None, self._rule_context
        cache_key = tuple((id(condition), truth) for condition, truth in conditions)
        cached = self._activation_cache.get(cache_key)
        if cached is not None:
            return cached
        typed_terms: list[ir_expr.Expression] = []
        effect_context = self._rule_context
        combined_analysis = self._guard_analysis
        for condition, truth in conditions:
            typed_condition = self._typed_condition_cache.get(id(condition))
            if typed_condition is None:
                typed_condition = effect_context.expressions.check(
                    condition, self._value_symbols, ir_types.BitType(), effect_context
                )
                if typed_condition.type == ir_types.BitType():
                    expanded = expression_support._expand_immutable_locals(
                        typed_condition,
                        self._value_symbols,
                        work_budget=self._module_context.services,
                    )
                    foreign = expression_domains.expression_domains(
                        expanded,
                        {**self._symbols, **self._resource_symbols},
                        self._register_symbols,
                    ) - {None, self._rule_domain}
                    if foreign:
                        raise SemanticError(
                            f"clock-domain mismatch in rule '{self._rule_name}': rule "
                            f"domain is '{self._rule_domain}', nested guard reads '{min(foreign)}'",
                            code="ZL-DOMAIN-CROSSING",
                            primary=expression_origins.semantic_origin(
                                condition, self._module_context
                            ),
                            fixes=(
                                "insert an explicit supported clock-domain crossing",
                            ),
                        )
                self._typed_condition_cache[id(condition)] = typed_condition
                # ``replace(context, ...)`` copies the numerical allocator.
                # Propagate allocations made while checking this unique
                # condition so later source expressions cannot reuse them.
                self._rule_context.advance_delay_allocator(
                    effect_context.scope.next_delay_instance
                )
                self._module_context.advance_delay_allocator(
                    effect_context.scope.next_delay_instance
                )
            if typed_condition.type != ir_types.BitType():
                raise SemanticError(
                    f"nested when guard in rule '{self._rule_name}' must be "
                    f"bit, got {typed_condition.type}",
                    code="ZL-SEMANTIC-CONDITIONAL-GUARD",
                    primary=expression_origins.semantic_origin(
                        condition, self._module_context
                    ),
                )
            term = (
                typed_condition
                if truth
                else replace(
                    expression_operators.build_binary(
                        ir_expr.BinaryOperator.EQUAL,
                        typed_condition,
                        ir_expr.Constant(0, ir_types.BitType()),
                    ),
                    origin=typed_condition.origin,
                )
            )
            typed_terms.append(term)
            term_analysis = semantic_callables._expand_analysis_calls(
                expression_support._expand_immutable_locals(
                    term, self._value_symbols, work_budget=self._module_context.services
                ),
                self._module_context,
                purpose=f"rule '{self._rule_name}' conditional refinement",
                refinement_only=True,
            )
            combined_analysis = expression_operators.build_binary(
                ir_expr.BinaryOperator.BIT_AND,
                combined_analysis,
                term_analysis,
            )
            effect_context = effect_context.with_scope(
                range_refinements=expression_ranges.guard_range_refinements(
                    combined_analysis
                ),
            )
        activation = typed_terms[0]
        for term in typed_terms[1:]:
            activation = replace(
                expression_operators.build_binary(
                    ir_expr.BinaryOperator.BIT_AND, activation, term
                ),
                origin=term.origin or activation.origin,
            )
        result = (activation, effect_context)
        self._activation_cache[cache_key] = result
        return result

    def validate_conditional_guards(
        self,
        source_actions: tuple[
            ast.NextAssignment
            | ast.OutputDrive
            | ast.ResourceAction
            | ast.ConditionalAction,
            ...,
        ],
        conditions: tuple[tuple[ast.Expression, bool], ...] = (),
    ) -> None:
        """Type every source guard, including branches with no effects.

        Effect flattening intentionally omits empty branches.  Guard
        validation therefore has to walk the source action tree itself;
        otherwise an unknown or non-bit guard can disappear merely because
        its branch currently performs no state action.
        """

        for source_action in source_actions:
            if not isinstance(source_action, ast.ConditionalAction):
                continue
            true_conditions = (*conditions, (source_action.guard, True))
            self.typed_activation_and_context(true_conditions)
            self.validate_conditional_guards(
                source_action.when_true,
                true_conditions,
            )
            if source_action.when_false is None:
                continue
            false_conditions = (*conditions, (source_action.guard, False))
            self.typed_activation_and_context(false_conditions)
            self.validate_conditional_guards(
                source_action.when_false,
                false_conditions,
            )
