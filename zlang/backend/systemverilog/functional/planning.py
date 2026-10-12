# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Dependency and lexical-scope planning for compact functional regions."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Callable

from zlang.backend import expression_materialization as materialization
from zlang.backend.systemverilog.functional import model as functional_model
from zlang.ir import expressions as expr
from zlang.ir import functional_regions as functional_regions
from zlang.ir import signed_reductions as signed_reductions


class FunctionalRegionPlanner:
    """Own exact functional dependency and binder analysis for direct SV."""

    def __init__(
        self,
        *,
        error: Callable[[str], Exception] = ValueError,
    ) -> None:
        self._error = error

    def regions(self, value: object) -> tuple[expr.FunctionalRegion, ...]:
        """Return compact regions in deterministic dependency-first order."""

        return tuple(node.region for node in self.composition_plan(value).nodes)

    def composition_plan(self, value: object) -> functional_model.FunctionalRegionCompositionPlan:
        """Build the lexical region DAG without expanding virtual elements."""

        ordered: list[functional_model.FunctionalRegionCompositionNode] = []
        state: dict[int, tuple[expr.FunctionalRegion, int]] = {}
        visited_expressions: dict[tuple[int, bool], expr.Expression] = {}

        def free_binders(item: object, bound: frozenset[str]) -> frozenset[str]:
            if isinstance(item, functional_regions.CompileTimeBinderRef):
                return (
                    frozenset()
                    if item.identity in bound
                    else frozenset((item.identity,))
                )
            if isinstance(item, expr.FunctionalRegion):
                owned = bound | frozenset((item.binder.identity,))
                template = free_binders(item.template, owned)
                tables = frozenset().union(
                    *(free_binders(table.values, owned) for table in item.tables),
                    frozenset(),
                )
                captures = frozenset().union(
                    *(free_binders(captured, bound) for _, captured in item.captures),
                    frozenset(),
                )
                return template | tables | captures
            if isinstance(item, tuple):
                return frozenset().union(
                    *(free_binders(child, bound) for child in item),
                    frozenset(),
                )
            if is_dataclass(item) and not isinstance(item, type):
                return frozenset().union(
                    *(
                        free_binders(getattr(item, descriptor.name), bound)
                        for descriptor in fields(item)
                        if descriptor.name not in {"type", "origin", "source_origin"}
                    ),
                    frozenset(),
                )
            return frozenset()

        def dependencies(
            item: object,
            *,
            owner: expr.FunctionalRegion,
            region_template: bool = False,
        ) -> tuple[expr.FunctionalRegion, ...]:
            result: list[expr.FunctionalRegion] = []
            seen: set[int] = set()
            seen_expressions: dict[tuple[int, bool], expr.Expression] = {}

            def collect(current: object, *, template: bool) -> None:
                if isinstance(current, expr.FunctionalRegion):
                    if current is owner or id(current) in seen:
                        return
                    seen.add(id(current))
                    result.append(current)
                    return
                if isinstance(current, expr.Expression):
                    key = (id(current), template)
                    previous = seen_expressions.get(key)
                    if previous is current:
                        return
                    seen_expressions[key] = current
                    if (
                        template
                        and isinstance(current, expr.Reduce)
                        and isinstance(current.collection, expr.FunctionalRegion)
                        and current.operator
                        in {
                            expr.ReductionOperator.ADD,
                            expr.ReductionOperator.BIT_AND,
                            expr.ReductionOperator.BIT_OR,
                            expr.ReductionOperator.BIT_XOR,
                        }
                    ):
                        return
                    for child in materialization.expression_children(current):
                        collect(child, template=template)
                    return
                if isinstance(current, tuple):
                    for child in current:
                        collect(child, template=template)

            collect(item, template=region_template)
            return tuple(result)

        def visit(item: object, *, region_template: bool = False) -> None:
            if isinstance(item, expr.FunctionalRegion):
                status = state.get(id(item))
                if status is not None and status[0] is item:
                    if status[1] == 1:
                        raise self._error(
                            "functional region dependency graph contains a cycle"
                        )
                    return
                state[id(item)] = (item, 1)
                children = dependencies(
                    item.template,
                    owner=item,
                    region_template=True,
                )
                children += tuple(
                    child
                    for table in item.tables
                    for child in dependencies(table.values, owner=item)
                )
                children += tuple(
                    child
                    for _, captured in item.captures
                    for child in dependencies(captured, owner=item)
                )
                unique_children: list[expr.FunctionalRegion] = []
                child_ids: set[int] = set()
                for child in children:
                    if id(child) not in child_ids:
                        child_ids.add(id(child))
                        unique_children.append(child)
                        visit(child)
                identity = signed_reductions.expression_semantic_identity(item)
                ordered.append(
                    functional_model.FunctionalRegionCompositionNode(
                        region=item,
                        identity=identity,
                        dependencies=tuple(
                            signed_reductions.expression_semantic_identity(child)
                            for child in unique_children
                        ),
                        free_binders=tuple(sorted(free_binders(item, frozenset()))),
                    )
                )
                state[id(item)] = (item, 2)
                return
            if isinstance(item, expr.Expression):
                expression_key = (id(item), region_template)
                previous = visited_expressions.get(expression_key)
                if previous is item:
                    return
                visited_expressions[expression_key] = item
                if (
                    region_template
                    and isinstance(item, expr.Reduce)
                    and isinstance(item.collection, expr.FunctionalRegion)
                    and item.operator
                    in {
                        expr.ReductionOperator.ADD,
                        expr.ReductionOperator.BIT_AND,
                        expr.ReductionOperator.BIT_OR,
                        expr.ReductionOperator.BIT_XOR,
                    }
                ):
                    return
                for child in materialization.expression_children(item):
                    visit(child, region_template=region_template)
                return
            if isinstance(item, tuple):
                for child in item:
                    visit(child)

        visit(value)
        return functional_model.FunctionalRegionCompositionPlan(tuple(ordered))

    def table_lookups(
        self, value: object
    ) -> tuple[expr.FunctionalTableLookup, ...]:
        found: list[expr.FunctionalTableLookup] = []
        seen: set[str] = set()
        identity_memo: dict[int, str] = {}

        def visit(item: object) -> None:
            if isinstance(item, expr.FunctionalTableLookup):
                identity = signed_reductions.expression_merkle_identity(item, identity_memo)
                if identity not in seen:
                    seen.add(identity)
                    found.append(item)
                return
            if isinstance(item, expr.FunctionalRegion):
                return
            if isinstance(item, expr.Expression):
                for child in materialization.expression_children(item):
                    visit(child)
                if isinstance(item, expr.VectorIndex) and isinstance(
                    item.index, functional_regions.CompileTimeExpr
                ):
                    visit(item.index)
                return
            if isinstance(item, tuple):
                for child in item:
                    visit(child)
                return
            if is_dataclass(item) and not isinstance(item, type):
                for descriptor in fields(item):
                    if descriptor.name not in {"type", "origin", "source_origin"}:
                        visit(getattr(item, descriptor.name))

        visit(value)
        return tuple(found)

    def binder_dependent(self, value: object, identity: str) -> bool:
        if isinstance(value, functional_regions.CompileTimeBinderRef):
            return value.identity == identity
        if isinstance(value, expr.FunctionalRegion):
            return self.binder_dependent(value.template, identity) or any(
                self.binder_dependent(item, identity)
                for table in value.tables
                for item in table.values
            ) or any(
                self.binder_dependent(captured, identity)
                for _, captured in value.captures
            )
        if isinstance(value, tuple):
            return any(self.binder_dependent(item, identity) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            return any(
                self.binder_dependent(getattr(value, descriptor.name), identity)
                for descriptor in fields(value)
                if descriptor.name not in {"type", "origin", "source_origin"}
            )
        return False

    def compact_reductions(self, value: object) -> tuple[expr.Reduce, ...]:
        """Find outermost compact reductions with an exact loop lowering."""

        found: list[expr.Reduce] = []
        seen: set[str] = set()
        identity_memo: dict[int, str] = {}

        def visit(item: object) -> None:
            if (
                isinstance(item, expr.Reduce)
                and isinstance(item.collection, expr.FunctionalRegion)
                and item.operator
                in {
                    expr.ReductionOperator.ADD,
                    expr.ReductionOperator.BIT_AND,
                    expr.ReductionOperator.BIT_OR,
                    expr.ReductionOperator.BIT_XOR,
                }
            ):
                identity = signed_reductions.expression_merkle_identity(item, identity_memo)
                if identity not in seen:
                    seen.add(identity)
                    found.append(item)
                return
            if isinstance(item, expr.FunctionalRegion):
                return
            if isinstance(item, tuple):
                for child in item:
                    visit(child)
                return
            if is_dataclass(item) and not isinstance(item, type):
                for descriptor in fields(item):
                    if descriptor.name not in {"type", "origin", "source_origin"}:
                        visit(getattr(item, descriptor.name))

        visit(value)
        return tuple(found)

    def contains_compact_reduction(self, value: object) -> bool:
        return bool(self.compact_reductions(value))


__all__ = ["FunctionalRegionPlanner"]
