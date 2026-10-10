# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Typed state-transition assembly and conflict validation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any

from zlang.ast import nodes as ast
from zlang.common.graph import ReachabilityIndex
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import state as ir_state
from zlang.ir import storage as ir_storage
from zlang.ir import types as ir_types
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.source import SourceOrigin

from .errors import SemanticError
from . import callables as semantic_callables
from . import expression_ranges
from . import expression_domains
from . import expression_origins
from . import expression_operators
from . import expression_support
from . import observations as semantic_observations
from . import actions as semantic_actions
from . import context as semantic_context
from . import module_pipeline
from .storage_symbols import FifoSymbol, MemorySymbol


ResourceAction = tuple[
    ir_state.StateActionKind, str, tuple[ir_expr.Expression, ...],
    SourceOrigin | None, ir_expr.Expression | None,
]


@dataclass(frozen=True)
class TransitionAnalysisProduct:
    priorities: tuple[ir_module.RulePriority, ...]
    rule_output_targets: frozenset[str]
    transition: ir_state.ResolvedTransition


def _priority_orders(
    first: str, second: str, graph: ReachabilityIndex[str],
) -> bool:
    return graph.reaches(first, second) or graph.reaches(second, first)


def _guards_are_provably_disjoint(
    first: ir_expr.Expression,
    second: ir_expr.Expression,
) -> bool:
    """Prove only opposite exact register-equality constraints."""

    def constraints(expression: ir_expr.Expression) -> dict[str, set[tuple[str, object]]]:
        if not isinstance(expression, ir_expr.Binary):
            return {}
        if expression.operator is ir_expr.BinaryOperator.BIT_AND:
            merged = constraints(expression.left)
            for name, values in constraints(expression.right).items():
                merged.setdefault(name, set()).update(values)
            return merged
        if expression.operator is not ir_expr.BinaryOperator.EQUAL:
            return {}
        left, right = expression.left, expression.right
        if isinstance(left, ir_expr.RegisterRef) and isinstance(right, ir_expr.Constant):
            return {left.name: {(str(right.type), right.value)}}
        if isinstance(right, ir_expr.RegisterRef) and isinstance(left, ir_expr.Constant):
            return {right.name: {(str(left.type), left.value)}}
        return {}

    left = constraints(first)
    right = constraints(second)
    return any(
        left[name].isdisjoint(right[name]) for name in left.keys() & right.keys()
    )


def _state_resources(
    transition_prefix: str,
    registers: tuple[ir_module.Register, ...],
    fifos: tuple[ir_storage.Fifo, ...],
    memories: tuple[ir_storage.Memory, ...],
    ports: tuple[ir_module.Port, ...],
    output_targets: frozenset[str],
    clock: str | None,
) -> tuple[tuple[ir_state.StateResource, ...], dict[tuple[ir_state.StateResourceKind, str], str]]:
    resources = tuple((
        *(ir_state.StateResource(
            f"state:{transition_prefix}:register:{item.name}", item.name,
            ir_state.StateResourceKind.REGISTER, item.type, item.domain or clock,
            item.initial.origin if item.initial is not None else None,
        ) for item in registers),
        *(ir_state.StateResource(
            f"state:{transition_prefix}:fifo:{item.name}", item.name,
            ir_state.StateResourceKind.FIFO, item.element_type, item.domain,
            item.source_origin, item.depth,
        ) for item in fifos),
        *(ir_state.StateResource(
            item.semantic_id, item.name, ir_state.StateResourceKind.MEMORY,
            item.element_type, item.domain, item.source_origin, item.depth,
        ) for item in memories if item.scheduled),
        *(ir_state.StateResource(
            f"state:{transition_prefix}:output:{item.name}", item.name,
            ir_state.StateResourceKind.OUTPUT, item.type, item.domain or clock,
        ) for item in ports if item.name in output_targets),
    ))
    return resources, {
        (item.kind, item.name): item.semantic_id for item in resources
    }


def _action_groups(
    transition_prefix: str,
    rules: tuple[ir_module.Rule, ...],
    resource_actions: dict[str, list[ResourceAction]],
    resource_ids: dict[tuple[ir_state.StateResourceKind, str], str],
) -> tuple[ir_state.ActionGroup, ...]:
    groups: list[ir_state.ActionGroup] = []
    for rule in rules:
        group_id = f"action-group:{transition_prefix}:{rule.name}"
        actions: list[ir_state.StateAction] = []
        for ordinal, action in enumerate(rule.actions):
            register = isinstance(action.target, ir_module.Register)
            resource_kind = (
                ir_state.StateResourceKind.REGISTER
                if register else ir_state.StateResourceKind.OUTPUT
            )
            action_kind = (
                ir_state.StateActionKind.REGISTER_WRITE
                if register else ir_state.StateActionKind.OUTPUT_WRITE
            )
            actions.append(ir_state.StateAction(
                f"{group_id}:{action_kind.value}:{action.target.name}:{ordinal}",
                resource_ids[(resource_kind, action.target.name)], action_kind,
                (action.expression,), group_id, action.expression.origin,
                activation=action.activation,
            ))
        for ordinal, item in enumerate(resource_actions[rule.name]):
            kind, name, operands, origin, activation = item
            resource_kind = (
                ir_state.StateResourceKind.FIFO
                if kind in {ir_state.StateActionKind.FIFO_PUSH, ir_state.StateActionKind.FIFO_POP}
                else ir_state.StateResourceKind.MEMORY
            )
            actions.append(ir_state.StateAction(
                f"{group_id}:{kind.value}:{name}:{ordinal}",
                resource_ids[(resource_kind, name)], kind, operands, group_id,
                origin, activation=activation,
            ))
        groups.append(ir_state.ActionGroup(
            group_id, rule.name, rule.guard, tuple(actions), rule.guard.origin,
            rule.domain,
        ))
    return tuple(groups)


def _validate_conflicts(
    groups: tuple[ir_state.ActionGroup, ...],
    rules: tuple[ir_module.Rule, ...],
    priorities: ReachabilityIndex[str],
) -> None:
    for index, first in enumerate(groups):
        for second in groups[index + 1:]:
            if (
                first.domain == second.domain
                and ir_state.groups_may_conflict(first, second)
                and not _priority_orders(first.rule_name, second.rule_name, priorities)
                and not _guards_are_provably_disjoint(first.guard, second.guard)
            ):
                raise SemanticError(
                    f"rules '{first.rule_name}' and '{second.rule_name}' have "
                    "conflicting state actions; add explicit priority"
                )
    writes: dict[str, dict[str, ir_module.Rule]] = {}
    for rule in rules:
        for action in rule.actions:
            writes.setdefault(action.target.name, {})[rule.name] = rule
    for target_name, writers_by_name in writes.items():
        writers = tuple(writers_by_name.values())
        for index, first in enumerate(writers):
            for second in writers[index + 1:]:
                if (
                    first.domain == second.domain
                    and not _priority_orders(first.name, second.name, priorities)
                    and not _guards_are_provably_disjoint(first.guard, second.guard)
                ):
                    raise SemanticError(
                        f"rules '{first.name}' and '{second.name}' both write "
                        f"target '{target_name}'; add explicit priority"
                    )


def _transition_identity(
    clock: str | None,
    reset: str | None,
    resources: tuple[ir_state.StateResource, ...],
    groups: tuple[ir_state.ActionGroup, ...],
    priorities: set[tuple[str, str]],
) -> str:
    payload = (
        clock, reset,
        tuple(
            (item.semantic_id, item.kind.value, repr(item.type), item.domain, item.depth)
            for item in resources
        ),
        tuple(
            (
                group.semantic_id, group.domain,
                expression_semantic_identity(group.guard),
                tuple(
                    (
                        action.semantic_id, action.resource_id, action.kind.value,
                        tuple(expression_semantic_identity(value) for value in action.operands),
                        *((expression_semantic_identity(action.activation),)
                          if action.activation is not None else ()),
                    )
                    for action in group.actions
                ),
            )
            for group in groups
        ),
        tuple(sorted(priorities)),
    )
    return hashlib.sha256(repr(payload).encode()).hexdigest()


@dataclass(frozen=True)
class RuleAnalysisProduct:
    rules: tuple[ir_module.Rule, ...]
    resource_actions: dict[str, list[ResourceAction]]


class RuleAnalyzer:
    """Own rule guards, conditional activations, and typed state actions."""

    def analyze(
        self,
        preparation: module_pipeline.DeclarationPreparationProduct,
        hardware: module_pipeline.HardwareInterfaceProduct,
        state_storage: module_pipeline.StateStoragePreparationProduct,
        storage: Any,
        assigned_registers: frozenset[str],
    ) -> RuleAnalysisProduct:
        value_symbols = state_storage.value_symbols
        symbols = state_storage.symbols
        storage_declarations = state_storage.storage_declarations
        resource_symbols = storage_declarations.resource_symbols
        register_symbols = state_storage.register_symbols
        outputs = state_storage.outputs
        module_context = state_storage.expression_context
        effective_source_unit = preparation.source_unit
        effective_source_digest = preparation.source_digest
        _FifoSymbol = FifoSymbol
        rules: list[ir_module.Rule] = []
        rule_resource_actions: dict[
            str,
            list[
                tuple[
                    ir_state.StateActionKind,
                    str,
                    tuple[ir_expr.Expression, ...],
                    SourceOrigin | None,
                    ir_expr.Expression | None,
                ]
            ],
        ] = {}
        rule_names: set[str] = set()
        for declaration in preparation.module.rules:
            if declaration.name in rule_names:
                raise SemanticError(f"duplicate rule '{declaration.name}'")
            rule_names.add(declaration.name)
            target_domains: set[str | None] = set()
            for leaf in semantic_actions.conditional_action_leaves(
                declaration.actions
            ):
                source_action = leaf.action
                if isinstance(source_action, ast.ResourceAction):
                    resource = resource_symbols.get(source_action.resource)
                    if resource is not None:
                        target_domains.add(resource.domain)
                    continue
                target_name = (
                    source_action.target.register
                    if isinstance(source_action, ast.NextAssignment)
                    and isinstance(source_action.target, ast.IndexedAssignmentTarget)
                    else source_action.target
                )
                target = (
                    outputs.get(target_name)
                    if isinstance(source_action, ast.OutputDrive)
                    else register_symbols.get(target_name)
                )
                if target is not None:
                    target_domains.add(target.domain)
            rule_domain = hardware.state_domains.resolve(
                "rule", declaration.name, declaration.domain, target_domains
            )
            guard = module_context.expressions.check(
                declaration.guard, value_symbols, ir_types.BitType(), module_context
            )
            if guard.type != ir_types.BitType():
                raise SemanticError(f"guard for rule '{declaration.name}' must be bit")
            guard_domains = {
                item
                for item in expression_domains.expression_domains(
                    expression_support._expand_immutable_locals(
                        guard, value_symbols, work_budget=module_context.services
                    ),
                    {**symbols, **resource_symbols},
                    register_symbols,
                )
                if item is not None
            }
            if guard_domains - {rule_domain}:
                foreign = sorted(guard_domains - {rule_domain})[0]
                raise SemanticError(
                    f"clock-domain mismatch in rule '{declaration.name}': rule "
                    f"domain is '{rule_domain}', guard reads '{foreign}'",
                    code="ZL-DOMAIN-CROSSING",
                    primary=guard.origin,
                    fixes=("insert an explicit supported clock-domain crossing",),
                )
            guard_analysis = semantic_callables._expand_analysis_calls(
                expression_support._expand_immutable_locals(
                    guard, value_symbols, work_budget=module_context.services
                ),
                module_context,
                purpose=f"rule '{declaration.name}' guard refinement",
            )
            rule_context = module_context.with_scope(
                range_refinements=expression_ranges.guard_range_refinements(
                    guard_analysis
                ),
            )
            actions: list[ir_module.NextAssignment] = []
            resource_actions: list[
                tuple[
                    ir_state.StateActionKind,
                    str,
                    tuple[ir_expr.Expression, ...],
                    SourceOrigin | None,
                    ir_expr.Expression | None,
                ]
            ] = []
            action_targets: dict[
                str,
                list[tuple[tuple[tuple[tuple[int, ...], bool], ...], SourceOrigin | None]],
            ] = {}
            activation_cache: dict[
                tuple[tuple[int, bool], ...],
                tuple[ir_expr.Expression, semantic_context.ExpressionContext],
            ] = {}
            # One source conditional is shared by its true and false branch paths.
            # Type it once so a stateful guard expression (for example ``delay``)
            # denotes one physical expression instance rather than being allocated
            # independently for each effect below the branch.
            typed_condition_cache: dict[int, ir_expr.Expression] = {}

            def effect_origin(
                action: ast.NextAssignment | ast.OutputDrive | ast.ResourceAction,
            ) -> SourceOrigin | None:
                if isinstance(action, ast.ResourceAction) and action.origin is not None:
                    return SourceOrigin(
                        action.origin,
                        f"state action {action.resource}.{action.operation}",
                        effective_source_unit,
                        effective_source_digest,
                    )
                syntax_origin = (
                    action.target.origin
                    if (
                        isinstance(action, ast.NextAssignment)
                        and isinstance(action.target, ast.IndexedAssignmentTarget)
                    )
                    else action.expression.origin
                    if isinstance(action, (ast.NextAssignment, ast.OutputDrive))
                    else None
                )
                return (
                    SourceOrigin(
                        syntax_origin,
                        "conditional rule action",
                        effective_source_unit,
                        effective_source_digest,
                    )
                    if syntax_origin is not None else None
                )

            def claim_effect_target(
                key: str,
                leaf: semantic_actions.ConditionalActionLeaf,
                description: str,
            ) -> None:
                origin = effect_origin(leaf.action)
                for previous_path, previous_origin in action_targets.get(key, ()):
                    if semantic_actions.action_paths_are_exclusive(
                        previous_path, leaf.branch_path
                    ):
                        continue
                    notes = (
                        (
                            "the previous potentially-overlapping effect is at "
                            f"{previous_origin.span.render()}"
                        ),
                    ) if previous_origin is not None else ()
                    raise SemanticError(
                        f"rule '{declaration.name}' {description}; the effects are "
                        "on overlapping conditional paths",
                        code="ZL-SEMANTIC-CONDITIONAL-ACTION-CONFLICT",
                        primary=origin,
                        notes=notes,
                        fixes=(
                            "place the effects in opposite arms of one when/else, "
                            "or use separate explicitly prioritized rules",
                        ),
                    )
                action_targets.setdefault(key, []).append((leaf.branch_path, origin))

            def typed_activation_and_context(
                conditions: tuple[tuple[ast.Expression, bool], ...],
            ) -> tuple[ir_expr.Expression | None, semantic_context.ExpressionContext]:
                if not conditions:
                    return None, rule_context
                cache_key = tuple((id(condition), truth) for condition, truth in conditions)
                cached = activation_cache.get(cache_key)
                if cached is not None:
                    return cached
                typed_terms: list[ir_expr.Expression] = []
                effect_context = rule_context
                combined_analysis = guard_analysis
                for condition, truth in conditions:
                    typed_condition = typed_condition_cache.get(id(condition))
                    if typed_condition is None:
                        typed_condition = effect_context.expressions.check(
                            condition, value_symbols, ir_types.BitType(), effect_context
                        )
                        typed_condition_cache[id(condition)] = typed_condition
                        # ``replace(context, ...)`` copies the numerical allocator.
                        # Propagate allocations made while checking this unique
                        # condition so later source expressions cannot reuse them.
                        rule_context.advance_delay_allocator(
                            effect_context.scope.next_delay_instance
                        )
                        module_context.advance_delay_allocator(
                            effect_context.scope.next_delay_instance
                        )
                    if typed_condition.type != ir_types.BitType():
                        raise SemanticError(
                            f"nested when guard in rule '{declaration.name}' must be "
                            f"bit, got {typed_condition.type}",
                            code="ZL-SEMANTIC-CONDITIONAL-GUARD",
                            primary=expression_origins.semantic_origin(condition, module_context),
                        )
                    term = (
                        typed_condition
                        if truth else replace(
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
                            term, value_symbols, work_budget=module_context.services
                        ),
                        module_context,
                        purpose=f"rule '{declaration.name}' conditional refinement",
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
                activation_cache[cache_key] = result
                return result

            def validate_conditional_guards(
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
                    typed_activation_and_context(true_conditions)
                    validate_conditional_guards(
                        source_action.when_true,
                        true_conditions,
                    )
                    if source_action.when_false is None:
                        continue
                    false_conditions = (*conditions, (source_action.guard, False))
                    typed_activation_and_context(false_conditions)
                    validate_conditional_guards(
                        source_action.when_false,
                        false_conditions,
                    )

            validate_conditional_guards(declaration.actions)
            leaves = semantic_actions.conditional_action_leaves(
                declaration.actions
            )
            if not leaves:
                raise SemanticError(
                    f"rule '{declaration.name}' has no state or output effects"
                )
            for leaf in leaves:
                action = leaf.action
                activation, action_context = typed_activation_and_context(leaf.conditions)
                if isinstance(action, ast.ResourceAction):
                    resource = resource_symbols.get(action.resource)
                    if resource is None:
                        raise SemanticError(
                            f"rule '{declaration.name}' references unknown state resource '{action.resource}'"
                        )
                    if resource.domain != rule_domain:
                        raise SemanticError(
                            f"clock-domain mismatch in rule '{declaration.name}': "
                            f"rule domain is '{rule_domain}', resource "
                            f"'{resource.name}' belongs to '{resource.domain}'",
                            code="ZL-DOMAIN-CROSSING",
                            primary=effect_origin(action),
                        )
                    resource_kind = "FIFO" if isinstance(resource, _FifoSymbol) else "memory"
                    origin = (
                        SourceOrigin(
                            action.origin,
                            f"{resource_kind} {resource.name}.{action.operation}",
                            effective_source_unit,
                            effective_source_digest,
                        )
                        if action.origin is not None else None
                    )
                    if isinstance(resource, _FifoSymbol):
                        if resource.name not in storage.scheduled_fifo_names:
                            raise SemanticError(
                                f"FIFO '{resource.name}' is not owned by scheduled rule actions"
                            )
                        if action.operation == "push":
                            if len(action.operands) != 1:
                                raise SemanticError("FIFO push requires exactly one payload")
                            operand = action_context.expressions.check_typed_boundary(
                                action.operands[0], value_symbols, resource.element_type,
                                action_context,
                            )
                            if operand.type != resource.element_type:
                                raise SemanticError(
                                    f"FIFO '{resource.name}' push has type {operand.type}, expected {resource.element_type}"
                                )
                            kind = ir_state.StateActionKind.FIFO_PUSH
                            operands = (operand,)
                        elif action.operation == "pop":
                            if action.operands:
                                raise SemanticError("FIFO pop does not accept operands")
                            kind = ir_state.StateActionKind.FIFO_POP
                            operands = ()
                        else:
                            raise SemanticError(
                                f"FIFO '{resource.name}' has no rule action '{action.operation}'"
                            )
                    elif isinstance(resource, MemorySymbol):
                        if (
                            resource.name
                            not in storage_declarations.scheduled_memory_names
                        ):
                            raise SemanticError(
                                f"memory '{resource.name}' is not owned by scheduled rule actions"
                            )
                        address_type = ir_types.UIntType(resource.address_width)
                        if action.operation == "read":
                            if len(action.operands) != 1:
                                raise SemanticError(
                                    "memory read requires exactly one address operand"
                                )
                            address = action_context.expressions.check(
                                action.operands[0], value_symbols, address_type,
                                action_context,
                            )
                            if address.type != address_type:
                                raise SemanticError(
                                    f"memory '{resource.name}' read address has type {address.type}, expected {address_type}"
                                )
                            kind = ir_state.StateActionKind.MEMORY_READ_REQUEST
                            operands = (address,)
                        elif action.operation == "write":
                            if len(action.operands) not in {2, 3}:
                                raise SemanticError(
                                    "memory write requires exactly address and data operands, "
                                    "with an optional byte mask"
                                )
                            address = action_context.expressions.check(
                                action.operands[0], value_symbols, address_type,
                                action_context,
                            )
                            data = action_context.expressions.check_typed_boundary(
                                action.operands[1], value_symbols, resource.element_type,
                                action_context,
                            )
                            if address.type != address_type:
                                raise SemanticError(
                                    f"memory '{resource.name}' write address has type {address.type}, expected {address_type}"
                                )
                            if data.type != resource.element_type:
                                raise SemanticError(
                                    f"memory '{resource.name}' write data has type {data.type}, expected {resource.element_type}"
                                )
                            memory_masked = (
                                resource.name
                                in storage_declarations.scheduled_masked_memory_names
                            )
                            if memory_masked:
                                mask_type = ir_types.BitsType(
                                    ir_storage.memory_byte_mask_width(
                                        resource.element_type.width
                                    )
                                )
                                if len(action.operands) == 3:
                                    mask = action_context.expressions.check(
                                        action.operands[2], value_symbols, mask_type,
                                        action_context,
                                    )
                                    if mask.type != mask_type:
                                        raise SemanticError(
                                            f"memory '{resource.name}' write mask has type "
                                            f"{mask.type}, expected {mask_type}"
                                        )
                                else:
                                    mask = ir_expr.Constant((1 << mask_type.width) - 1, mask_type)
                            kind = ir_state.StateActionKind.MEMORY_WRITE
                            operands = (
                                (address, data, mask) if memory_masked else (address, data)
                            )
                        else:
                            raise SemanticError(
                                f"memory '{resource.name}' has no rule action '{action.operation}'"
                            )
                    else:
                        raise SemanticError(
                            f"state resource '{resource.name}' does not support rule actions"
                        )
                    key = f"{resource.name}:{kind.value}"
                    claim_effect_target(
                        key,
                        leaf,
                        f"performs state action '{resource.name}.{action.operation}' twice",
                    )
                    resource_actions.append(
                        (kind, resource.name, operands, origin, activation)
                    )
                    continue
                if (
                    isinstance(action, ast.NextAssignment)
                    and isinstance(action.target, ast.IndexedAssignmentTarget)
                ):
                    indexed = action.target
                    target = register_symbols.get(indexed.register)
                    if target is None:
                        raise SemanticError(
                            f"rule '{declaration.name}' indexed target "
                            f"'{indexed.register}' is not a register"
                        )
                    semantic_observations.record_definition(
                        action_context,
                        semantic_observations.declaration_origin(
                            indexed.name_origin,
                            f"register {target.name}",
                            action_context,
                        ),
                        action_context.services.tooling.definition_targets.get(id(target)),
                        name=target.name,
                        kind="register",
                    )
                    if target.domain != rule_domain:
                        raise SemanticError(
                            f"clock-domain mismatch in rule '{declaration.name}': "
                            f"rule domain is '{rule_domain}', register "
                            f"'{target.name}' belongs to '{target.domain}'",
                            code="ZL-DOMAIN-CROSSING",
                            primary=effect_origin(action),
                        )
                    if not isinstance(target.type, ir_types.VecType):
                        raise SemanticError(
                            f"rule '{declaration.name}' indexed target "
                            f"'{indexed.register}' must be a one-dimensional vector "
                            f"register, got {target.type}"
                        )
                    if target.name in assigned_registers:
                        raise SemanticError(
                            f"register '{target.name}' has both a rule action and "
                            "next-state assignment"
                        )
                    index = action_context.expressions.check(
                        indexed.index, value_symbols, None, action_context
                    )
                    index = expression_support._expand_immutable_locals(
                        index, value_symbols, work_budget=action_context.services
                    )
                    index = semantic_callables._expand_analysis_calls(
                        index, action_context, purpose="vector-register update index"
                    )
                    if not isinstance(index.type, (ir_types.UIntType, ir_types.BitsType)):
                        raise SemanticError(
                            "vector-register update index must be an unsigned "
                            f"integral expression; got {index.type}"
                        )
                    value_range = expression_ranges.static_value_range(
                        index, action_context.scope.range_refinements
                    )
                    if value_range is None:
                        raise SemanticError(
                            "vector-register update index has no statically provable "
                            f"unsigned range; required 0..{target.type.length - 1}"
                        )
                    if (
                        value_range.minimum < 0
                        or value_range.maximum >= target.type.length
                    ):
                        raise SemanticError(
                            f"vector-register update index range "
                            f"{value_range.minimum}..{value_range.maximum} is not "
                            f"provably within vector length {target.type.length} "
                            f"(required 0..{target.type.length - 1}, type {index.type})"
                        )
                    value = action_context.expressions.check_typed_boundary(
                        action.expression,
                        value_symbols,
                        target.type.element_type,
                        action_context,
                    )
                    if value.type != target.type.element_type:
                        raise SemanticError(
                            f"rule '{declaration.name}' writes {value.type} to "
                            f"element type {target.type.element_type} of register "
                            f"'{target.name}'"
                        )
                    update_origin = (
                        SourceOrigin(
                            indexed.origin,
                            f"vector update {target.name}",
                            effective_source_unit,
                            effective_source_digest,
                        )
                        if indexed.origin is not None
                        else action.expression.origin
                    )
                    vector = ir_expr.RegisterRef(
                        target.name, target.type, origin=update_origin
                    )
                    update = ir_expr.VectorUpdate(
                        vector,
                        index,
                        value,
                        target.type.length,
                        value_range,
                        target.type,
                        origin=update_origin,
                    )
                    claim_effect_target(
                        target.name,
                        leaf,
                        f"writes register '{target.name}' twice",
                    )
                    actions.append(ir_module.NextAssignment(
                        target, update, activation
                    ))
                    continue
                if isinstance(action, ast.OutputDrive):
                    target = outputs.get(action.target)
                    if target is None:
                        if action.target in register_symbols:
                            raise SemanticError(
                                f"drive target '{action.target}' is stored state; "
                                "use '<-' to update a register or registered output",
                                code="ZL-SEMANTIC-DRIVE-TARGET",
                                primary=semantic_observations.declaration_origin(
                                    action.target_origin,
                                    f"drive target {action.target}",
                                    action_context,
                                ),
                            )
                        raise SemanticError(
                            f"rule '{declaration.name}' drive target "
                            f"'{action.target}' is not an output wire",
                            code="ZL-SEMANTIC-DRIVE-TARGET",
                            primary=semantic_observations.declaration_origin(
                                action.target_origin,
                                f"drive target {action.target}",
                                action_context,
                            ),
                        )
                    if target.registered:
                        raise SemanticError(
                            f"registered output '{action.target}' is stored state; "
                            "use '<-' to update it",
                            code="ZL-SEMANTIC-DRIVE-TARGET",
                            primary=semantic_observations.declaration_origin(
                                action.target_origin,
                                f"drive target {action.target}",
                                action_context,
                            ),
                        )
                else:
                    target = register_symbols.get(action.target)
                    if target is None:
                        output = outputs.get(action.target)
                        if output is not None:
                            raise SemanticError(
                                f"output wire '{action.target}' is not stored and "
                                "cannot be updated with '<-'; declare 'out reg "
                                f"{action.target} : {output.type}' for held state, "
                                f"or use 'drive {action.target} = ...' for a "
                                "transient rule output",
                                code="ZL-SEMANTIC-WIRE-NEXT-ASSIGNMENT",
                                primary=semantic_observations.declaration_origin(
                                    action.target_origin,
                                    f"output target {action.target}",
                                    action_context,
                                ),
                            )
                        raise SemanticError(
                            f"rule '{declaration.name}' target '{action.target}' "
                            "is not a register or output wire"
                        )
                if target.domain != rule_domain:
                    raise SemanticError(
                        f"clock-domain mismatch in rule '{declaration.name}': "
                        f"rule domain is '{rule_domain}', target '{target.name}' "
                        f"belongs to '{target.domain}'",
                        code="ZL-DOMAIN-CROSSING",
                        primary=effect_origin(action),
                    )
                if isinstance(target, ir_module.Port) and not isinstance(
                    target.type, (ir_types.BitType, ir_types.UIntType, ir_types.SIntType, ir_types.BitsType)
                ):
                    raise SemanticError("rule output actions require a scalar wire")
                if isinstance(target, ir_module.Register) and target.name in assigned_registers:
                    raise SemanticError(
                        f"register '{target.name}' has both a rule action and next-state assignment"
                    )
                expression = action_context.expressions.check_typed_boundary(
                    action.expression, value_symbols, target.type, action_context
                )
                if expression.type != target.type:
                    raise SemanticError(
                        f"rule '{declaration.name}' writes {expression.type} to "
                        f"{target.type} target '{target.name}'"
                    )
                target_kind = "output" if isinstance(target, ir_module.Port) else "register"
                semantic_observations.record_definition(
                    action_context,
                    semantic_observations.declaration_origin(
                        action.target_origin,
                        f"{target_kind} {target.name}",
                        action_context,
                    ),
                    action_context.services.tooling.definition_targets.get(id(target)),
                    name=target.name,
                    kind=target_kind,
                )
                claim_effect_target(
                    target.name,
                    leaf,
                    f"writes {target_kind} '{target.name}' twice",
                )
                actions.append(ir_module.NextAssignment(
                    target, expression, activation
                ))
            for effect in actions:
                effect_domains = {
                    item
                    for item in expression_domains.expression_domains(
                        expression_support._expand_immutable_locals(
                            effect.expression, value_symbols,
                            work_budget=module_context.services,
                        ),
                        {**symbols, **resource_symbols},
                        register_symbols,
                    )
                    if item is not None
                }
                if effect_domains - {rule_domain}:
                    foreign = sorted(effect_domains - {rule_domain})[0]
                    raise SemanticError(
                        f"clock-domain mismatch in rule '{declaration.name}': "
                        f"action in '{rule_domain}' reads '{foreign}'",
                        code="ZL-DOMAIN-CROSSING",
                        primary=effect.expression.origin,
                        fixes=("insert an explicit supported clock-domain crossing",),
                    )
            for _, _, operands, origin, activation in resource_actions:
                for operand in (*operands, *((activation,) if activation is not None else ())):
                    operand_domains = {
                        item
                        for item in expression_domains.expression_domains(
                            expression_support._expand_immutable_locals(
                                operand, value_symbols,
                                work_budget=module_context.services,
                            ),
                            {**symbols, **resource_symbols},
                            register_symbols,
                        )
                        if item is not None
                    }
                    if operand_domains - {rule_domain}:
                        foreign = sorted(operand_domains - {rule_domain})[0]
                        raise SemanticError(
                            f"clock-domain mismatch in rule '{declaration.name}': "
                            f"resource action in '{rule_domain}' reads '{foreign}'",
                            code="ZL-DOMAIN-CROSSING",
                            primary=origin,
                        )
            rules.append(
                ir_module.Rule(
                    declaration.name,
                    guard,
                    tuple(actions),
                    rule_domain,
                    declaration.physical_name_hint,
                )
            )
            rule_resource_actions[declaration.name] = resource_actions

        return RuleAnalysisProduct(tuple(rules), rule_resource_actions)


class StateTransitionAnalyzer:
    """Own rule priority, resource identity, and conflict semantics."""

    def analyze(
        self, *, priorities: tuple[ast.RulePriority, ...],
        rules: tuple[ir_module.Rule, ...], registers: tuple[ir_module.Register, ...],
        fifos: tuple[ir_storage.Fifo, ...], memories: tuple[ir_storage.Memory, ...],
        ports: tuple[ir_module.Port, ...],
        resource_actions: dict[str, list[ResourceAction]],
        transition_prefix: str, clock: str | None, reset: str | None,
    ) -> TransitionAnalysisProduct:
        rule_names = {rule.name for rule in rules}
        domains = {rule.name: rule.domain for rule in rules}
        records: list[ir_module.RulePriority] = []
        edges: set[tuple[str, str]] = set()
        for declaration in priorities:
            edge = (declaration.higher, declaration.lower)
            if not set(edge) <= rule_names:
                raise SemanticError("rule priority references an unknown rule")
            if declaration.higher == declaration.lower:
                raise SemanticError("a rule cannot have priority over itself")
            if domains[declaration.higher] != domains[declaration.lower]:
                raise SemanticError(
                    "rule priority cannot order different clock domains: "
                    f"'{declaration.higher}' is in '{domains[declaration.higher]}' "
                    f"and '{declaration.lower}' is in "
                    f"'{domains[declaration.lower]}'",
                    code="ZL-DOMAIN-CROSSING",
                )
            if edge in edges:
                raise SemanticError("duplicate rule priority")
            edges.add(edge)
            records.append(ir_module.RulePriority(*edge))
        graph = ReachabilityIndex(edges)
        if graph.has_cycle:
            raise SemanticError("rule priority graph contains a cycle")
        outputs = frozenset(
            action.target.name for rule in rules for action in rule.actions
            if isinstance(action.target, ir_module.Port)
        )
        resources, resource_ids = _state_resources(
            transition_prefix, registers, fifos, memories, ports, outputs, clock
        )
        groups = _action_groups(
            transition_prefix, rules, resource_actions, resource_ids
        )
        _validate_conflicts(groups, rules, graph)
        transition = ir_state.ResolvedTransition(
            f"transition:{_transition_identity(clock, reset, resources, groups, edges)}",
            clock, reset, resources, groups, tuple(sorted(edges)),
        )
        return TransitionAnalysisProduct(tuple(records), outputs, transition)
