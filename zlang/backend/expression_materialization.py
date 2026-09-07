"""Deterministic materialization planning for typed backend expressions.

The semantic IR deliberately does not prescribe whether a pure value becomes
an RTL/Haskell temporary.  Backends nevertheless need one common decision for
large or shared expressions so an exact typed graph is not copied at every use
site.  This module only plans names and rewrites value references; it does not
render either SystemVerilog or Clash and therefore cannot change semantics.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Callable, Iterable, Mapping

from zlang.ir import expressions as expr
from zlang.ir.functional import (
    materialize_exact_reduction,
    materialize_functional_region,
)
from zlang.ir.module import Module
from zlang.ir.traversal import (
    ExpressionTraversalPolicy,
    expression_children as typed_expression_children,
)


@dataclass(frozen=True)
class MaterializedExpression:
    """One exact typed expression and its deterministic backend-local name."""

    expression: expr.Expression
    name: str


def expression_children(value: expr.Expression) -> tuple[expr.Expression, ...]:
    """Return executable child expressions without double-counting plans."""

    return typed_expression_children(
        value,
        policy=ExpressionTraversalPolicy.EXECUTABLE,
    )


def walk_expression(value: expr.Expression) -> Iterable[expr.Expression]:
    yield value
    for child in expression_children(value):
        yield from walk_expression(child)


def expression_size(value: expr.Expression) -> int:
    return 1 + sum(expression_size(child) for child in expression_children(value))


def module_expression_roots(
    module: Module,
    *,
    normalize: Callable[[expr.Expression], expr.Expression] | None = None,
    include_register_initials: bool = True,
) -> tuple[expr.Expression, ...]:
    """Collect every executable pure expression owned by one module.

    Unified state resource operands are authoritative roots in addition to the
    compatibility ``Rule.actions`` view.  Optional scheduled-memory controls
    are intentionally skipped rather than fabricated.
    """

    roots: list[expr.Expression] = []
    roots.extend(local.expression for local in module.locals)
    roots.extend(assignment.expression for assignment in module.assignments)
    roots.extend(assignment.expression for assignment in module.next_assignments)
    roots.extend(
        assignment.activation
        for assignment in module.next_assignments
        if assignment.activation is not None
    )
    # A backend may render register reset values in a static/value context
    # instead of through its runtime expression renderer.  Such initializers
    # must not make an otherwise dead Signal temporary appear reusable.  The
    # direct-SV procedural reset path does use the runtime renderer, while the
    # Clash ``register initial next`` form deliberately does not.
    if include_register_initials:
        roots.extend(register.initial for register in module.registers)
    roots.extend(binding.expression for binding in module.instance_bindings)
    for fifo in module.fifos:
        roots.extend(
            value for value in (fifo.data, fifo.push, fifo.pop) if value is not None
        )
    for memory in module.memories:
        roots.extend(
            value
            for value in (
                memory.read_address,
                memory.write_enable,
                memory.write_address,
                memory.write_data,
                memory.write_mask,
            )
            if value is not None
        )
    roots.extend(rom.read_address for rom in module.roms)
    for region in module.elastic_pipeline_regions:
        roots.append(region.source_expression)
        roots.extend(candidate.expression for candidate in region.candidates)
    if module.resolved_transition is None:
        # Compatibility-only modules may still carry the original Rule view.
        # Once a transition is resolved, its action groups are authoritative;
        # walking both views double-counts identical expressions and can create
        # spurious "shared" temporaries.
        for rule in module.rules:
            roots.append(rule.guard)
            for action in rule.actions:
                roots.append(action.expression)
                if action.activation is not None:
                    roots.append(action.activation)
    else:
        for group in module.resolved_transition.action_groups:
            roots.append(group.guard)
            roots.extend(
                operand
                for action in group.actions
                for operand in action.operands
            )
            roots.extend(
                action.activation
                for action in group.actions
                if action.activation is not None
            )
    if normalize is not None:
        return tuple(normalize(root) for root in roots)
    return tuple(roots)


def plan_materialization(
    roots: Iterable[expr.Expression],
    *,
    preferred_names: Mapping[expr.Expression, str] | None = None,
    reserved_names: Iterable[str] = (),
    generated_prefix: str = "zlang_expr_",
) -> tuple[MaterializedExpression, ...]:
    """Choose shared/expensive exact expressions in deterministic DFS order."""

    preferred = dict(preferred_names or {})
    occurrences: Counter[expr.Expression] = Counter()
    order: list[expr.Expression] = []
    aggregate_field_selections: set[expr.Expression] = set()
    dynamic_index_prefixes: set[expr.Expression] = set()
    sizes: dict[expr.Expression, int] = {}

    def size_of(value: expr.Expression) -> int:
        cached = sizes.get(value)
        if cached is not None:
            return cached
        result = 1 + sum(size_of(child) for child in expression_children(value))
        sizes[value] = result
        return result

    def visit(value: expr.Expression) -> None:
        if isinstance(value, expr.RuntimeIndex) and not isinstance(
            value.expression,
            (
                expr.InputRef,
                expr.ParameterRef,
                expr.RegisterRef,
                expr.InstanceOutputRef,
                expr.ReadyValidRef,
                expr.CreditRef,
                expr.RequestResponseRef,
                expr.FifoRef,
                expr.MemoryRef,
                expr.RomRef,
            ),
        ):
            # IEEE SystemVerilog permits an indexed part-select on an
            # expression, but Vivado rejects a range/cast/compound expression
            # as that select's prefix (Synth 8-2599).  Retain the exact typed
            # vector as one backend-local value so every emitter can render a
            # simple reference without changing index order or element width.
            dynamic_index_prefixes.add(value.expression)
        if (
            isinstance(value, expr.FieldAccess)
            and isinstance(value.expression, expr.RuntimeIndex)
        ):
            aggregate_field_selections.add(value.expression)
        if (
            isinstance(value, (expr.FieldAccess, expr.VectorIndex, expr.RuntimeIndex))
            and isinstance(value.expression, (expr.Unpack, expr.Bitcast, expr.Reshape))
        ):
            aggregate_field_selections.add(value.expression)
        occurrences[value] += 1
        if occurrences[value] == 1:
            order.append(value)
        for child in expression_children(value):
            visit(child)

    for root in roots:
        visit(root)

    expensive_conversion_inputs = {
        value.expression
        for value in order
        if isinstance(value, expr.FixedConvert)
        and size_of(value.expression) >= 8
    }

    selected: list[expr.Expression] = []
    for value in order:
        size = size_of(value)
        used_repeatedly = occurrences[value] > 1 and size >= 4
        feeds_expensive_conversion = value in expensive_conversion_inputs
        if (
            value in preferred
            or value in aggregate_field_selections
            or value in dynamic_index_prefixes
            or used_repeatedly
            or feeds_expensive_conversion
        ):
            if not isinstance(
                value,
                (
                    expr.Constant,
                    expr.InputRef,
                    expr.ParameterRef,
                    expr.RegisterRef,
                    expr.InstanceOutputRef,
                    expr.ReadyValidRef,
                    expr.CreditRef,
                    expr.RequestResponseRef,
                ),
            ):
                selected.append(value)

    used_names = set(reserved_names)
    materialized: list[MaterializedExpression] = []
    generated_index = 0
    for value in selected:
        name = preferred.get(value)
        if name is None:
            while True:
                name = f"{generated_prefix}{generated_index}"
                generated_index += 1
                if name not in used_names:
                    break
        if name in {item.name for item in materialized}:
            continue
        used_names.add(name)
        materialized.append(MaterializedExpression(value, name))
    return tuple(materialized)


def dependency_ordered_materialization(
    materialized: Iterable[MaterializedExpression],
) -> tuple[MaterializedExpression, ...]:
    """Order exact temporaries so every selected dependency is defined first.

    :func:`plan_materialization` deliberately preserves deterministic global
    first-discovery order.  That order is suitable for continuous assignments,
    but it is not necessarily topological when one selected subexpression was
    first seen through a different root.  Procedural backends (notably local
    variables inside a SystemVerilog function) must therefore order the
    selected dependency graph explicitly instead of merely reversing DFS
    discovery order.
    """

    planned = tuple(materialized)
    by_expression = {item.expression: item for item in planned}
    state: dict[expr.Expression, int] = {}
    ordered: list[MaterializedExpression] = []

    def dependencies(value: expr.Expression) -> tuple[expr.Expression, ...]:
        found: list[expr.Expression] = []
        seen: set[expr.Expression] = set()

        def visit(current: expr.Expression) -> None:
            for child in expression_children(current):
                if child in seen:
                    continue
                seen.add(child)
                if child in by_expression:
                    found.append(child)
                else:
                    visit(child)

        visit(value)
        return tuple(found)

    def visit(value: expr.Expression) -> None:
        status = state.get(value, 0)
        if status == 2:
            return
        if status == 1:
            raise ValueError(
                "materialized expression dependency graph contains a cycle"
            )
        state[value] = 1
        for dependency in dependencies(value):
            visit(dependency)
        state[value] = 2
        ordered.append(by_expression[value])

    for item in planned:
        visit(item.expression)
    return tuple(ordered)


def replace_materialized(
    value: expr.Expression,
    aliases: Mapping[expr.Expression, str],
    *,
    keep: expr.Expression | None = None,
) -> expr.Expression:
    """Replace planned subgraphs with exact-width typed local references."""

    if value != keep and value in aliases:
        return expr.InputRef(aliases[value], value.type, origin=value.origin)

    # Compact functional/reduction regions intentionally keep their expanded
    # executable graph outside dataclass fields.  Materialization planning
    # walks that graph via :func:`expression_children`; rewriting must expose
    # the identical graph as well or an alias selected from a region body would
    # never reach the backend renderer.  These replacements are backend-local
    # views only: the semantic/canonical IR remains compact and unchanged.
    if isinstance(value, expr.FunctionalRegion):
        elements = tuple(
            replace_materialized(element, aliases, keep=keep)
            for element in materialize_functional_region(value)
        )
        return expr.Generate(
            index=value.binder.display_name,
            start=value.binder.start,
            stop=value.binder.stop,
            elements=elements,
            type=value.type,
            origin=value.origin,
        )
    if isinstance(value, expr.Reduce) and (
        value.expanded is not None or value.plan is not None
    ):
        expanded = (
            value.expanded
            if value.expanded is not None
            else materialize_exact_reduction(value)
        )
        assert expanded is not None
        return replace_materialized(expanded, aliases, keep=keep)
    if isinstance(value, expr.ImplementationChoice):
        # Rendering and executable traversal observe only the selected
        # alternative.  Rewriting unused alternatives can demand aliases for
        # state that the backend correctly never allocated.
        return replace_materialized(
            value.selected_alternative.expression,
            aliases,
            keep=keep,
        )

    def rewrite(item: object) -> object:
        if isinstance(item, expr.Expression):
            return replace_materialized(item, aliases, keep=keep)
        if isinstance(item, tuple):
            return tuple(rewrite(child) for child in item)
        if is_dataclass(item) and not isinstance(item, type):
            updates: dict[str, object] = {}
            for field in fields(item):
                if field.name in {"origin", "source_origin"} or not field.init:
                    continue
                current = getattr(item, field.name)
                replacement = rewrite(current)
                if replacement != current:
                    updates[field.name] = replacement
            return replace(item, **updates) if updates else item
        return item

    updates: dict[str, object] = {}
    for field in fields(value):
        if field.name in {"origin", "source_origin"} or not field.init:
            continue
        current = getattr(value, field.name)
        replacement = rewrite(current)
        if replacement != current:
            updates[field.name] = replacement
    return replace(value, **updates) if updates else value
