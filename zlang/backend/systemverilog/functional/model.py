# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Immutable direct-SystemVerilog products for compact functional regions."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.backend import expression_materialization as materialization
from zlang.ir import expressions as expr


@dataclass(frozen=True)
class FunctionalRegionEmissionPlan:
    """Stable statement-level lowering plan for one compact region."""

    identity: str
    result_name: str
    loop_variable: str
    element_width: int
    table_temporaries: tuple[tuple[expr.FunctionalTableLookup, str], ...]
    expression_temporaries: tuple[materialization.MaterializedExpression, ...]
    reduction_temporaries: tuple[FunctionalReductionEmissionPlan, ...] = ()
    scatter: FunctionalScatterEmissionPlan | None = None
    schema: str = materialization.FUNCTIONAL_REGION_EMISSION_SCHEMA


@dataclass(frozen=True)
class FunctionalRegionCompositionNode:
    """One region and its exact lexical dependencies."""

    region: expr.FunctionalRegion
    identity: str
    dependencies: tuple[str, ...]
    free_binders: tuple[str, ...]


@dataclass(frozen=True)
class FunctionalRegionCompositionPlan:
    """Stable dependency-first plan for nested compact regions."""

    nodes: tuple[FunctionalRegionCompositionNode, ...]
    schema: str = materialization.FUNCTIONAL_REGION_EMISSION_SCHEMA


@dataclass(frozen=True)
class FunctionalReductionEmissionPlan:
    """One exact associative bitwise reduction over a compact child region."""

    reduction: expr.Reduce
    accumulator: str
    loop_variable: str
    table_temporaries: tuple[tuple[expr.FunctionalTableLookup, str], ...]
    children: tuple[FunctionalReductionEmissionPlan, ...]


@dataclass(frozen=True)
class FunctionalScatterDimension:
    region: expr.FunctionalRegion
    loop_variable: str
    table_temporaries: tuple[tuple[expr.FunctionalTableLookup, str], ...]


@dataclass(frozen=True)
class FunctionalScatterEntry:
    enable: expr.Expression
    address: expr.Expression
    value: expr.Expression


@dataclass(frozen=True)
class FunctionalScatterEmissionPlan:
    """Invert an exact destination decode into candidate-addressed writes."""

    dimensions: tuple[FunctionalScatterDimension, ...]
    entries: tuple[FunctionalScatterEntry, ...]
    expression_temporaries: tuple[materialization.MaterializedExpression, ...]
    enable_temporary: str
    address_temporary: str
    value_temporary: str


@dataclass(frozen=True)
class FunctionalExpressionContext:
    """Lexical names available while rendering one functional expression."""

    binders: tuple[tuple[str, str], ...]
    captures: tuple[tuple[str, expr.Expression], ...]
    table_temporaries: tuple[tuple[expr.FunctionalTableLookup, str], ...]

    def binder(self, identity: str) -> str | None:
        return next((name for key, name in self.binders if key == identity), None)

    def capture(self, identity: str) -> expr.Expression | None:
        return next((value for key, value in self.captures if key == identity), None)

    def table_temporary(self, lookup: expr.FunctionalTableLookup) -> str | None:
        return next(
            (name for candidate, name in self.table_temporaries if candidate == lookup),
            None,
        )


__all__ = [
    "FunctionalExpressionContext",
    "FunctionalReductionEmissionPlan",
    "FunctionalRegionCompositionNode",
    "FunctionalRegionCompositionPlan",
    "FunctionalRegionEmissionPlan",
    "FunctionalScatterDimension",
    "FunctionalScatterEmissionPlan",
    "FunctionalScatterEntry",
]
