"""Deterministic materialization planning for typed backend expressions.

The semantic IR deliberately does not prescribe whether a pure value becomes
an RTL temporary. The backend needs one common decision for
large or shared expressions so an exact typed graph is not copied at every use
site.  This module only plans names and rewrites value references; it does not
render SystemVerilog and therefore cannot change semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from collections.abc import Iterator
from typing import Callable, Iterable, Mapping

from zlang.ir import expressions as expr
from zlang.ir.expression_graph import ExpressionDagIndex
from zlang.ir.functional import materialize_exact_reduction
from zlang.ir.module import Module
from zlang.ir.signed_reductions import expression_merkle_identity
from zlang.ir.traversal import (
    ExpressionTraversalPolicy,
    expression_children as typed_expression_children,
)


FUNCTIONAL_REGION_EMISSION_SCHEMA = "zlang-direct-sv-functional-region-v3"
DIRECT_SV_DAG_SCHEMA = "zlang-direct-sv-dag-v1"


@dataclass(frozen=True)
class MaterializedExpression:
    """One exact typed expression and its deterministic backend-local name."""

    expression: expr.Expression
    name: str


@dataclass(frozen=True)
class DirectSvDagNode:
    """One pure node in the backend-owned exact expression DAG."""

    expression: expr.Expression
    identity: str
    fanout: int
    size: int
    scope: str
    temporary: str | None = None


@dataclass(frozen=True)
class DirectSvDagPlan:
    """Deterministic dependency-first sharing plan for one procedural scope."""

    nodes: tuple[DirectSvDagNode, ...]
    materialized: tuple[MaterializedExpression, ...]
    schema: str = DIRECT_SV_DAG_SCHEMA


class ExpressionAliasMap:
    """Identity-keyed aliases that never hash recursive expression dataclasses.

    Frozen expression records inherit structural ``__hash__`` and ``__eq__``.
    Using a deeply shared DAG node as an ordinary ``dict`` key can therefore
    revisit its complete logical expansion.  Backend alias lookup is semantic,
    so store the stable expression identity instead and cache it by object ID.
    """

    def __init__(
        self, items: Iterable[tuple[expr.Expression, str]] = ()
    ) -> None:
        self._names: dict[str, str] = {}
        self._identity_cache: dict[int, tuple[expr.Expression, str]] = {}
        self._payload_cache: dict[int, str] = {}
        self.update(items)

    def identity(self, value: expr.Expression) -> str:
        return _cached_expression_identity(
            value, self._identity_cache, self._payload_cache
        )

    def __contains__(self, value: object) -> bool:
        return isinstance(value, expr.Expression) and self.identity(value) in self._names

    def __getitem__(self, value: expr.Expression) -> str:
        return self._names[self.identity(value)]

    def __setitem__(self, value: expr.Expression, name: str) -> None:
        identity = self.identity(value)
        previous = self._names.get(identity)
        if previous is not None and previous != name:
            raise ValueError(
                "one semantic expression cannot have conflicting backend aliases"
            )
        self._names[identity] = name

    def update(self, items: Iterable[tuple[expr.Expression, str]]) -> None:
        for value, name in items:
            self[value] = name

    def values(self) -> Iterator[str]:
        return iter(self._names.values())


def _cached_expression_identity(
    value: expr.Expression,
    identity_cache: dict[int, tuple[expr.Expression, str]],
    payload_cache: dict[int, str],
) -> str:
    cached = identity_cache.get(id(value))
    if cached is not None and cached[0] is value:
        return cached[1]
    identity = expression_merkle_identity(value, payload_cache)
    identity_cache[id(value)] = (value, identity)
    return identity


def expression_children(value: expr.Expression) -> tuple[expr.Expression, ...]:
    """Return executable child expressions without double-counting plans."""

    if isinstance(value, expr.FunctionalRegion):
        # A FunctionalRegion is an executable *template*, not an eagerly
        # replicated expression tree.  Backend materialization may share its
        # runtime captures, but selecting a node from one virtual iteration as
        # a module temporary would both escape the binder and force the very
        # textual expansion that the compact IR is meant to prevent.
        return tuple(captured for _, captured in value.captures)

    if isinstance(value, expr.Reduce) and (
        value.expanded is not None or value.plan is not None
    ):
        expanded = (
            value.expanded
            if value.expanded is not None
            else materialize_exact_reduction(value)
        )
        assert expanded is not None
        return (expanded,)

    if isinstance(value, expr.ImplementationChoice):
        return (value.selected_alternative.expression,)

    return typed_expression_children(
        value,
        policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
    )


def walk_expression(value: expr.Expression) -> Iterable[expr.Expression]:
    """Walk one executable DAG in deterministic preorder exactly once."""

    yield from ExpressionDagIndex(
        (value,), children=expression_children
    ).preorder()


def expression_size(value: expr.Expression) -> int:
    # Preserve the historical logical-occurrence metric while computing it
    # once per unique DAG node.  Shared subtrees therefore no longer cause
    # recursive host work proportional to their expanded tree.
    return ExpressionDagIndex(
        (value,), children=expression_children
    ).logical_occurrences()


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
    # direct-SV procedural reset path uses the runtime renderer.
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
                memory.initial_value,
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
    preferred_names: (
        Iterable[tuple[expr.Expression, str]]
        | Mapping[expr.Expression, str]
        | None
    ) = None,
    reserved_names: Iterable[str] = (),
    generated_prefix: str = "zlang_expr_",
    minimum_shared_size: int = 4,
    scope: str = "module",
) -> tuple[MaterializedExpression, ...]:
    """Choose shared/expensive exact expressions in deterministic DFS order."""

    return build_direct_sv_dag_plan(
        roots,
        preferred_names=preferred_names,
        reserved_names=reserved_names,
        generated_prefix=generated_prefix,
        minimum_shared_size=minimum_shared_size,
        scope=scope,
    ).materialized


def build_direct_sv_dag_plan(
    roots: Iterable[expr.Expression],
    *,
    preferred_names: (
        Iterable[tuple[expr.Expression, str]]
        | Mapping[expr.Expression, str]
        | None
    ) = None,
    reserved_names: Iterable[str] = (),
    generated_prefix: str = "zlang_expr_",
    minimum_shared_size: int = 4,
    scope: str = "module",
) -> DirectSvDagPlan:
    """Build a bounded DAG plan without expanding logical expression paths.

    Fanout counts incoming DAG edges and root uses.  Recursion visits every
    object once, so a heavily shared semantic graph remains linear in its
    number of unique nodes.  Module and FunctionalRegion callers build
    separate plans; this is the backend's explicit scope boundary.
    """

    if minimum_shared_size < 1:
        raise ValueError("minimum shared expression size must be positive")

    if preferred_names is None:
        preferred_items: tuple[tuple[expr.Expression, str], ...] = ()
    elif isinstance(preferred_names, Mapping):
        preferred_items = tuple(preferred_names.items())
    else:
        preferred_items = tuple(preferred_names)
    preferred_by_identity = {
        expression_merkle_identity(expression): name
        for expression, name in preferred_items
    }
    identities: dict[int, tuple[expr.Expression, str]] = {}
    representatives: dict[str, expr.Expression] = {}
    fanout: dict[str, int] = {}
    dependency_order: list[expr.Expression] = []
    aggregate_field_selections: set[str] = set()
    dynamic_index_prefixes: set[str] = set()
    sizes: dict[str, int] = {}
    payload_cache: dict[int, str] = {}

    def identity_of(value: expr.Expression) -> str:
        return _cached_expression_identity(value, identities, payload_cache)

    def size_of(value: expr.Expression) -> int:
        identity = identity_of(value)
        cached = sizes.get(identity)
        if cached is not None:
            return cached
        result = 1 + sum(size_of(child) for child in expression_children(value))
        sizes[identity] = result
        return result

    def visit(value: expr.Expression) -> None:
        identity = identity_of(value)
        fanout[identity] = fanout.get(identity, 0) + 1
        if identity in representatives:
            return
        representatives[identity] = value
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
            dynamic_index_prefixes.add(identity_of(value.expression))
        if (
            isinstance(value, expr.FieldAccess)
            and isinstance(value.expression, expr.RuntimeIndex)
        ):
            aggregate_field_selections.add(identity_of(value.expression))
        if (
            isinstance(value, (expr.FieldAccess, expr.VectorIndex, expr.RuntimeIndex))
            and isinstance(value.expression, (expr.Unpack, expr.Bitcast, expr.Reshape))
        ):
            aggregate_field_selections.add(identity_of(value.expression))
        for child in expression_children(value):
            visit(child)
        dependency_order.append(value)

    for root in roots:
        visit(root)

    expensive_conversion_inputs = {
        identity_of(value.expression)
        for value in dependency_order
        if isinstance(value, expr.FixedConvert)
        and size_of(value.expression) >= 8
    }

    selected: list[expr.Expression] = []
    for value in dependency_order:
        size = size_of(value)
        used_repeatedly = (
            fanout[identity_of(value)] > 1 and size >= minimum_shared_size
        )
        identity = identity_of(value)
        feeds_expensive_conversion = identity in expensive_conversion_inputs
        if (
            identity in preferred_by_identity
            or identity in aggregate_field_selections
            or identity in dynamic_index_prefixes
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
        identity = identity_of(value)
        name = preferred_by_identity.get(identity)
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
    names_by_identity = {
        identity_of(item.expression): item.name for item in materialized
    }
    return DirectSvDagPlan(
        nodes=tuple(
            DirectSvDagNode(
                expression=value,
                identity=identity_of(value),
                fanout=fanout[identity_of(value)],
                size=size_of(value),
                scope=scope,
                temporary=names_by_identity.get(identity_of(value)),
            )
            for value in dependency_order
        ),
        materialized=tuple(materialized),
    )


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
    identity_cache: dict[int, tuple[expr.Expression, str]] = {}
    payload_cache: dict[int, str] = {}

    def identity_of(value: expr.Expression) -> str:
        return _cached_expression_identity(value, identity_cache, payload_cache)

    by_identity = {identity_of(item.expression): item for item in planned}
    state: dict[str, int] = {}
    ordered: list[MaterializedExpression] = []

    def dependencies(value: expr.Expression) -> tuple[str, ...]:
        found: list[str] = []
        seen: set[str] = set()

        def visit(current: expr.Expression) -> None:
            for child in expression_children(current):
                child_identity = identity_of(child)
                if child_identity in seen:
                    continue
                seen.add(child_identity)
                if child_identity in by_identity:
                    found.append(child_identity)
                else:
                    visit(child)

        visit(value)
        return tuple(found)

    def visit(identity: str) -> None:
        status = state.get(identity, 0)
        if status == 2:
            return
        if status == 1:
            raise ValueError(
                "materialized expression dependency graph contains a cycle"
            )
        state[identity] = 1
        item = by_identity[identity]
        for dependency in dependencies(item.expression):
            visit(dependency)
        state[identity] = 2
        ordered.append(item)

    for item in planned:
        visit(identity_of(item.expression))
    return tuple(ordered)


def replace_materialized(
    value: expr.Expression,
    aliases: ExpressionAliasMap | Mapping[expr.Expression, str],
    *,
    keep: expr.Expression | None = None,
    rewrite_region_owned: bool = False,
) -> expr.Expression:
    """Replace planned subgraphs with exact-width typed local references."""

    alias_index = (
        aliases
        if isinstance(aliases, ExpressionAliasMap)
        else ExpressionAliasMap(aliases.items())
    )
    keep_identity = alias_index.identity(keep) if keep is not None else None
    if alias_index.identity(value) != keep_identity and value in alias_index:
        return expr.InputRef(alias_index[value], value.type, origin=value.origin)

    # Preserve a compact region and rewrite only its runtime captures.  The
    # region-owned template/table graph is rendered under its loop binder by
    # statement-based backends and cannot legally refer to a module-scoped
    # materialization alias.
    if isinstance(value, expr.FunctionalRegion) and not rewrite_region_owned:
        return replace(
            value,
            captures=tuple(
                (
                    reference,
                    replace_materialized(
                        captured,
                        alias_index,
                        keep=keep,
                        rewrite_region_owned=rewrite_region_owned,
                    ),
                )
                for reference, captured in value.captures
            ),
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
        return replace_materialized(
            expanded,
            alias_index,
            keep=keep,
            rewrite_region_owned=rewrite_region_owned,
        )
    if isinstance(value, expr.ImplementationChoice):
        # Rendering and executable traversal observe only the selected
        # alternative.  Rewriting unused alternatives can demand aliases for
        # state that the backend correctly never allocated.
        return replace_materialized(
            value.selected_alternative.expression,
            alias_index,
            keep=keep,
            rewrite_region_owned=rewrite_region_owned,
        )

    def rewrite(item: object) -> object:
        if isinstance(item, expr.Expression):
            return replace_materialized(
                item,
                alias_index,
                keep=keep,
                rewrite_region_owned=rewrite_region_owned,
            )
        if isinstance(item, tuple):
            return tuple(rewrite(child) for child in item)
        if is_dataclass(item) and not isinstance(item, type):
            updates: dict[str, object] = {}
            for field in fields(item):
                if field.name in {"origin", "source_origin"} or not field.init:
                    continue
                current = getattr(item, field.name)
                replacement = rewrite(current)
                if replacement is not current:
                    updates[field.name] = replacement
            return replace(item, **updates) if updates else item
        return item

    updates: dict[str, object] = {}
    for field in fields(value):
        if field.name in {"origin", "source_origin"} or not field.init:
            continue
        current = getattr(value, field.name)
        replacement = rewrite(current)
        if replacement is not current:
            updates[field.name] = replacement
    return replace(value, **updates) if updates else value
