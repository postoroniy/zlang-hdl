# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative functional-region scatter SystemVerilog emission."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from itertools import product

from zlang.ir import expressions as expr
from zlang.ir import types as ir_types
from zlang.backend import expression_materialization as materialization
from zlang.backend import identifiers as identifiers
from zlang.backend.systemverilog import context as emission_context
from zlang.backend.systemverilog import functional as functional
from zlang.backend.systemverilog import functional_scatter as scatter_lowering
from zlang.ir import functional_regions as functional_regions
from zlang.ir import signed_reductions as signed_reductions
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering

_FUNCTIONAL_REGION_PLANNER = functional.FunctionalRegionPlanner(error=SystemVerilogEmissionError)

def _resolve_functional_scatter_captures(
    value: object,
    captures: dict[str, expr.Expression],
    *,
    resolving: frozenset[str] = frozenset(),
) -> object:
    """Inline a closed region capture graph for structural recognition."""

    if isinstance(value, expr.FunctionalCaptureRef):
        if value.identity in resolving:
            raise SystemVerilogEmissionError(
                f"functional capture '{value.display_name}' is recursive"
            )
        captured = captures.get(value.identity)
        if captured is None or captured.type != value.type:
            raise SystemVerilogEmissionError(
                f"functional capture '{value.display_name}' is not bound exactly"
            )
        return _resolve_functional_scatter_captures(
            captured,
            captures,
            resolving=resolving | {value.identity},
        )
    if isinstance(value, tuple):
        return tuple(
            _resolve_functional_scatter_captures(
                item, captures, resolving=resolving
            )
            for item in value
        )
    if is_dataclass(value) and not isinstance(value, type):
        updates = {
            descriptor.name: _resolve_functional_scatter_captures(
                getattr(value, descriptor.name),
                captures,
                resolving=resolving,
            )
            for descriptor in fields(value)
            if descriptor.init
            and descriptor.name not in {"type", "origin", "source_origin"}
        }
        return replace(value, **updates) if updates else value
    return value

def _functional_scatter_shape(
    region: expr.FunctionalRegion,
) -> tuple[
    tuple[expr.FunctionalRegion, ...],
    tuple[functional.FunctionalScatterEntry, ...],
] | None:
    """Recognize an exact OR-of-addressed-candidates destination decode."""

    dimensions: list[expr.FunctionalRegion] = []
    current: expr.Expression = region.template
    leaves: tuple[expr.Expression, ...] | None = None
    while isinstance(current, expr.Reduce) and (
        current.operator is expr.ReductionOperator.BIT_OR
    ):
        if isinstance(current.collection, expr.FunctionalRegion):
            child = current.collection
            dimensions.append(child)
            current = child.template
            continue
        if isinstance(current.collection, (expr.Generate, expr.Map)):
            leaves = current.collection.elements
        break
    if leaves is None:
        leaves = (current,)
    if not leaves or any(
        not isinstance(leaf, expr.Mux) and not isinstance(leaf.type, ir_types.BitType)
        for leaf in leaves
    ):
        return None

    captures = {
        reference.identity: value
        for owner in (region, *dimensions)
        for reference, value in owner.captures
    }
    if not isinstance(region.type.element_type, (ir_types.BitType, ir_types.BitsType, ir_types.UIntType)):
        return None

    def match(leaf: expr.Expression) -> functional.FunctionalScatterEntry | None:
        # A destination-hit bitmap is the one-bit form of the same scatter as
        # a destination-value mux: OR-reducing ``enable && address == dst`` is
        # exactly OR-writing one to each enabled candidate address. Recognize
        # that compiler-owned shape directly so synthesis never expands a
        # destination-count x candidate-count procedural reduction.
        if isinstance(leaf, expr.Mux):
            source_items = (leaf.condition, leaf.when_true, leaf.when_false)
        else:
            source_items = (
                leaf,
                expr.Constant(1, ir_types.BitType(), origin=leaf.origin),
                expr.Constant(0, ir_types.BitType(), origin=leaf.origin),
            )
        try:
            resolved = tuple(
                _resolve_functional_scatter_captures(item, captures)
                for item in source_items
            )
        except (TypeError, ValueError, SystemVerilogEmissionError):
            return None
        if not all(isinstance(item, expr.Expression) for item in resolved):
            return None
        condition, when_true, when_false = resolved
        assert isinstance(condition, expr.Expression)
        assert isinstance(when_true, expr.Expression)
        assert isinstance(when_false, expr.Expression)
        if not isinstance(when_false, expr.Constant) or when_false.value != 0:
            return None
        if when_true.type != region.type.element_type:
            return None

        factors: list[expr.Expression] = []

        def flatten_and(value: expr.Expression) -> None:
            if (
                isinstance(value, expr.Binary)
                and value.operator is expr.BinaryOperator.BIT_AND
                and isinstance(value.type, ir_types.BitType)
            ):
                flatten_and(value.left)
                flatten_and(value.right)
                return
            factors.append(value)

        flatten_and(condition)
        destination: expr.FunctionalValue | None = None
        address: expr.Expression | None = None
        equality_index: int | None = None
        for index, factor in enumerate(factors):
            if not (
                isinstance(factor, expr.Binary)
                and factor.operator is expr.BinaryOperator.EQUAL
            ):
                continue
            for candidate_destination, candidate_address in (
                (factor.left, factor.right),
                (factor.right, factor.left),
            ):
                if not isinstance(candidate_destination, expr.FunctionalValue):
                    continue
                compile_time = candidate_destination.expression
                if not (
                    isinstance(compile_time, functional_regions.CompileTimeExpr)
                    and compile_time.operator is functional_regions.CompileTimeOperator.BINDER
                    and isinstance(compile_time.operands[0], functional_regions.CompileTimeBinderRef)
                    and compile_time.operands[0].identity == region.binder.identity
                ):
                    continue
                destination = candidate_destination
                address = candidate_address
                equality_index = index
                break
            if destination is not None:
                break
        if destination is None or address is None or equality_index is None:
            return None
        if region.binder.start != 0 or destination.type != address.type:
            return None
        if not isinstance(address.type, (ir_types.BitsType, ir_types.UIntType)):
            return None
        if any(
            _FUNCTIONAL_REGION_PLANNER.binder_dependent(
                item, region.binder.identity
            )
            for item in (address, when_true)
        ):
            return None

        enabled_factors = tuple(
            factor
            for index, factor in enumerate(factors)
            if index != equality_index
        )
        if not enabled_factors:
            enabled: expr.Expression = expr.Constant(1, ir_types.BitType())
        else:
            enabled = enabled_factors[0]
            for factor in enabled_factors[1:]:
                if not isinstance(factor.type, ir_types.BitType):
                    return None
                enabled = expr.Binary(
                    expr.BinaryOperator.BIT_AND,
                    enabled,
                    factor,
                    ir_types.BitType(),
                    ir_types.BitType(),
                )
        if _FUNCTIONAL_REGION_PLANNER.binder_dependent(
            enabled, region.binder.identity
        ):
            return None
        return functional.FunctionalScatterEntry(enabled, address, when_true)

    entries = tuple(match(leaf) for leaf in leaves)
    if any(entry is None for entry in entries):
        return None
    return tuple(dimensions), tuple(
        entry for entry in entries if entry is not None
    )

def _functional_scatter_plan(
    region: expr.FunctionalRegion,
    *,
    owner_identity: str,
    used: set[str],
) -> functional.FunctionalScatterEmissionPlan | None:
    shape = _functional_scatter_shape(region)
    if shape is None:
        return None
    regions, entries = shape
    # Capture resolution can make individual dimensions irrelevant. An
    # OR-scatter is idempotent, so replaying an identical candidate for every
    # value of a dead binder cannot change the result. Retain each dimension
    # only while an emitted candidate depends on its binder identity.
    regions = tuple(
        owner
        for owner in regions
        if any(
            _FUNCTIONAL_REGION_PLANNER.binder_dependent(
                value, owner.binder.identity
            )
            for entry in entries
            for value in (entry.enable, entry.address, entry.value)
        )
    )
    prefix = owner_identity[:10]
    dimensions = [
        functional.FunctionalScatterDimension(
            child,
            identifiers.allocate_private_rtl_identifier(
                f"region_scatter_i_{prefix}_{ordinal}",
                semantic_identity=(
                    f"{owner_identity}:scatter:{ordinal}:{child.binder.identity}"
                ),
                used=used,
            ),
            (),
        )
        for ordinal, child in enumerate(regions)
    ]
    tables = {
        table.name: table for child in (region, *regions) for table in child.tables
    }
    table_temporaries: list[tuple[expr.FunctionalTableLookup, str]] = []
    lookup_candidates = (
        lookup
        for entry in entries
        for root in (entry.enable, entry.address, entry.value)
        for lookup in _FUNCTIONAL_REGION_PLANNER.table_lookups(root)
    )
    lookup_identities: set[str] = set()
    identity_memo: dict[int, str] = {}
    unique_lookups: list[expr.FunctionalTableLookup] = []
    for lookup in lookup_candidates:
        identity = signed_reductions.expression_merkle_identity(lookup, identity_memo)
        if identity in lookup_identities:
            continue
        lookup_identities.add(identity)
        unique_lookups.append(lookup)
    for ordinal, lookup in enumerate(unique_lookups):
        if lookup.table_name not in tables:
            return None
        temporary = identifiers.allocate_private_rtl_identifier(
            f"region_scatter_table_{prefix}_{ordinal}",
            semantic_identity=(
                f"{owner_identity}:scatter:table:{ordinal}:{lookup.table_name}"
            ),
            used=used,
        )
        table_temporaries.append((lookup, temporary))
    if table_temporaries and not dimensions:
        return None
    if dimensions:
        dimensions[-1] = replace(
            dimensions[-1], table_temporaries=tuple(table_temporaries)
        )
    expression_temporaries = materialization.plan_materialization(
        (
            value
            for entry in entries
            for value in (entry.enable, entry.address, entry.value)
        ),
        reserved_names=tuple(used),
        generated_prefix=f"zlang_scatter_expr_{prefix}_",
        scope=f"functional_scatter:{owner_identity}",
    )
    used.update(item.name for item in expression_temporaries)
    return functional.FunctionalScatterEmissionPlan(
        tuple(dimensions),
        entries,
        expression_temporaries,
        identifiers.allocate_private_rtl_identifier(
            f"region_scatter_enable_{prefix}",
            semantic_identity=f"{owner_identity}:scatter:enable",
            used=used,
        ),
        identifiers.allocate_private_rtl_identifier(
            f"region_scatter_address_{prefix}",
            semantic_identity=f"{owner_identity}:scatter:address",
            used=used,
        ),
        identifiers.allocate_private_rtl_identifier(
            f"region_scatter_value_{prefix}",
            semantic_identity=f"{owner_identity}:scatter:value",
            used=used,
        ),
    )

def _functional_scatter_lowering_plan(
    region: expr.FunctionalRegion,
    plan: functional.FunctionalRegionEmissionPlan,
) -> scatter_lowering.FunctionalScatterLoweringPlan:
    """Adapt typed functional-region facts to the scatter policy owner."""

    scatter = plan.scatter
    assert scatter is not None
    try:
        return scatter_lowering.plan_scatter_lowering(
            entry_count=len(scatter.entries),
            result_width=region.type.length * plan.element_width,
            dimensions=tuple(
                scatter_lowering.FunctionalScatterDimensionShape(
                    dimension.region.binder.identity,
                    dimension.region.binder.start,
                    dimension.region.binder.stop,
                )
                for dimension in scatter.dimensions
            ),
            prefix=plan.identity[:10],
        )
    except ValueError as error:
        raise SystemVerilogEmissionError(str(error)) from error

def _functional_scatter_generate_rendering(
    region: expr.FunctionalRegion,
    plan: functional.FunctionalRegionEmissionPlan,
    *,
    declaration_indent: str,
    statement_indent: str,
    outer_materialized: tuple[materialization.MaterializedExpression, ...] = (),
) -> tuple[list[str], list[str]] | None:
    """Render an addressed OR-scatter through one bounded lowering policy."""

    lowering = _functional_scatter_lowering_plan(region, plan)
    if lowering.strategy is scatter_lowering.FunctionalScatterLoweringStrategy.CHUNKED_PROCEDURAL:
        return _functional_scatter_chunked_rendering(
            region,
            plan,
            lowering,
            declaration_indent=declaration_indent,
            statement_indent=statement_indent,
            outer_materialized=outer_materialized,
        )
    rendered = _functional_scatter_chunked_rendering(
        region,
        plan,
        lowering,
        declaration_indent=declaration_indent,
        statement_indent=statement_indent,
        outer_materialized=outer_materialized,
        procedural_write=False,
    )
    if rendered is not None:
        return rendered
    return _functional_scatter_bounded_process_rendering(
        region,
        plan,
        lowering,
        declaration_indent=declaration_indent,
        statement_indent=statement_indent,
        outer_materialized=outer_materialized,
    )

def _validated_scatter(
    plan: functional.FunctionalRegionEmissionPlan,
) -> tuple[
    functional.FunctionalScatterEmissionPlan,
    functional.FunctionalScatterEntry,
]:
    scatter = plan.scatter
    assert scatter is not None
    first_entry = scatter.entries[0]
    if any(
        entry.address.type != first_entry.address.type
        or entry.value.type != first_entry.value.type
        for entry in scatter.entries[1:]
    ):
        raise SystemVerilogEmissionError(
            "functional scatter entries do not share exact address/value types"
        )
    return scatter, first_entry


def _scatter_temporaries(
    scatter: functional.FunctionalScatterEmissionPlan,
    outer_materialized: tuple[materialization.MaterializedExpression, ...],
) -> tuple[
    materialization.ExpressionAliasMap,
    tuple[materialization.MaterializedExpression, ...],
    tuple[materialization.MaterializedExpression, ...],
    tuple[materialization.MaterializedExpression, ...],
]:
    outer_aliases = materialization.ExpressionAliasMap(
        (item.expression, item.name) for item in outer_materialized
    )
    local = tuple(
        item
        for item in scatter.expression_temporaries
        if item.expression not in outer_aliases
    )
    ordered = materialization.dependency_ordered_materialization(local)
    dependent = tuple(
        item
        for item in ordered
        if any(
            _FUNCTIONAL_REGION_PLANNER.binder_dependent(
                item.expression, dimension.region.binder.identity
            )
            for dimension in scatter.dimensions
        )
    )
    dependent_ids = {id(item) for item in dependent}
    invariant = tuple(item for item in ordered if id(item) not in dependent_ids)
    return outer_aliases, local, dependent, invariant


def _scatter_captures(
    scatter: functional.FunctionalScatterEmissionPlan,
) -> tuple[tuple[str, expr.Expression], ...]:
    captures: tuple[tuple[str, expr.Expression], ...] = ()
    for dimension in scatter.dimensions:
        captures = (
            *((reference.identity, value) for reference, value in dimension.region.captures),
            *captures,
        )
    return captures


def _temporary_declaration(
    item: materialization.MaterializedExpression,
    name: str,
    indent: str,
) -> str:
    signed = (
        " signed"
        if isinstance(item.expression.type, (ir_types.SIntType, ir_types.FixedType))
        else ""
    )
    return (
        f"{indent}logic{signed} "
        f"{sv_rendering._range(sv_rendering._width(item.expression.type))}{name};"
    )


def _functional_scatter_chunked_rendering(
    region: expr.FunctionalRegion,
    plan: functional.FunctionalRegionEmissionPlan,
    lowering: scatter_lowering.FunctionalScatterLoweringPlan,
    *,
    declaration_indent: str,
    statement_indent: str,
    outer_materialized: tuple[materialization.MaterializedExpression, ...] = (),
    procedural_write: bool = True,
) -> tuple[list[str], list[str]] | None:
    """Render invariant buses and one shared helper per physical chunk shape."""

    scatter, first_entry = _validated_scatter(plan)
    if any(dimension.table_temporaries for dimension in scatter.dimensions):
        if not procedural_write:
            return None
        return _functional_scatter_bounded_process_rendering(
            region,
            plan,
            lowering,
            declaration_indent=declaration_indent,
            statement_indent=statement_indent,
            outer_materialized=outer_materialized,
        )
    emission = emission_context.current_emission_context()
    if emission is None:
        raise SystemVerilogEmissionError(
            "functional scatter rendering requires an active helper scope"
        )
    helpers = emission.functional.scatter_helpers

    outer_aliases, _local, _dependent, invariant_temporaries = (
        _scatter_temporaries(scatter, outer_materialized)
    )
    aliases = materialization.ExpressionAliasMap(
        (
            *((item.expression, item.name) for item in outer_materialized),
            *((item.expression, item.name) for item in invariant_temporaries),
        )
    )
    declarations: list[str] = []
    statements: list[str] = []
    declarations.extend(
        _temporary_declaration(item, item.name, declaration_indent)
        for item in invariant_temporaries
    )
    with emission_context.functional_expression_scope(functional.FunctionalExpressionContext((), (), ())):
        for item in invariant_temporaries:
            rewritten = materialization.replace_materialized(
                item.expression, aliases, keep=item.expression
            )
            statements.append(
                f"{statement_indent}assign {item.name} = {sv_expression._expression(rewritten)};"
            )

    all_captures = _scatter_captures(scatter)
    address_width = sv_rendering._width(first_entry.address.type)
    for chunk in lowering.chunks:
        enable_bus = f"{chunk.accumulator}_candidate_enable"
        address_bus = f"{chunk.accumulator}_candidate_address"
        value_bus = f"{chunk.accumulator}_candidate_value"
        declarations.extend(
            (
                f"{declaration_indent}logic [{chunk.write_count - 1}:0] "
                f"{enable_bus};",
                f"{declaration_indent}logic "
                f"[{chunk.write_count * address_width - 1}:0] {address_bus};",
                f"{declaration_indent}logic "
                f"[{chunk.write_count * plan.element_width - 1}:0] {value_bus};",
                f"{declaration_indent}logic "
                f"[{lowering.result_width - 1}:0] {chunk.accumulator};",
            )
        )
        remaining_ranges = tuple(
            range(
                scatter.dimensions[ordinal].region.binder.start,
                scatter.dimensions[ordinal].region.binder.stop,
            )
            for ordinal in chunk.remaining_dimension_ordinals
        )
        remaining_values = (
            tuple(product(*remaining_ranges)) if remaining_ranges else ((),)
        )
        candidate_ordinal = 0
        for suffix_values in remaining_values:
            binders = tuple(
                (identity, str(value)) for identity, value in chunk.fixed_binders
            ) + tuple(
                (
                    scatter.dimensions[dimension_ordinal].region.binder.identity,
                    str(value),
                )
                for dimension_ordinal, value in zip(
                    chunk.remaining_dimension_ordinals, suffix_values
                )
            )
            context = functional.FunctionalExpressionContext(binders, all_captures, ())
            with emission_context.functional_expression_scope(context):
                for entry in scatter.entries[
                    chunk.entry_start:chunk.entry_stop
                ]:
                    enabled = sv_expression._expression(
                        materialization.replace_materialized(entry.enable, aliases)
                    )
                    address = sv_expression._expression(
                        materialization.replace_materialized(entry.address, aliases)
                    )
                    value = sv_expression._expression(
                        materialization.replace_materialized(entry.value, aliases)
                    )
                    statements.extend(
                        (
                            f"{statement_indent}assign {enable_bus}"
                            f"[{candidate_ordinal}] = {enabled};",
                            f"{statement_indent}assign {address_bus}["
                            f"{candidate_ordinal * address_width} +: "
                            f"{address_width}] = {address};",
                            f"{statement_indent}assign {value_bus}["
                            f"{candidate_ordinal * plan.element_width} +: "
                            f"{plan.element_width}] = {value};",
                        )
                    )
                    candidate_ordinal += 1
        if candidate_ordinal != chunk.write_count:
            raise SystemVerilogEmissionError(
                "functional scatter chunk candidate enumeration changed shape"
            )
        if procedural_write:
            helper_name, helper_text = scatter_lowering.render_scatter_helper(
                destination_count=region.type.length,
                result_width=lowering.result_width,
                element_width=plan.element_width,
                address_width=address_width,
                candidate_count=chunk.write_count,
                procedural_write=True,
            )
        else:
            helper_name, helper_text = scatter_lowering.render_scatter_helper(
                destination_count=region.type.length,
                result_width=lowering.result_width,
                element_width=plan.element_width,
                address_width=address_width,
                candidate_count=chunk.write_count,
                procedural_write=False,
            )
        existing = helpers.get(helper_name)
        if existing is not None and existing != helper_text:
            raise SystemVerilogEmissionError(
                "functional scatter helper identity collision"
            )
        helpers[helper_name] = helper_text
        statements.extend(
            (
                f"{statement_indent}{helper_name} "
                f"{chunk.accumulator}_instance (",
                f"{statement_indent}  .candidate_enable({enable_bus}),",
                f"{statement_indent}  .candidate_address({address_bus}),",
                f"{statement_indent}  .candidate_value({value_bus}),",
                f"{statement_indent}  .result({chunk.accumulator})",
                f"{statement_indent});",
            )
        )

    _append_scatter_reduction(
        lowering,
        plan.result_name,
        declarations,
        statements,
        declaration_indent=declaration_indent,
        statement_indent=statement_indent,
    )
    return declarations, statements

def _append_scatter_reduction(
    lowering: scatter_lowering.FunctionalScatterLoweringPlan,
    result_name: str,
    declarations: list[str],
    statements: list[str],
    *,
    declaration_indent: str,
    statement_indent: str,
) -> None:
    sources = tuple(chunk.accumulator for chunk in lowering.chunks)
    for level in lowering.reduction_names:
        declarations.extend(
            f"{declaration_indent}logic [{lowering.result_width - 1}:0] {name};"
            for name in level
        )
        next_sources: list[str] = []
        for ordinal, target in enumerate(level):
            left = sources[ordinal * 2]
            right = sources[ordinal * 2 + 1] if ordinal * 2 + 1 < len(sources) else None
            expression = left if right is None else f"{left} | {right}"
            statements.append(f"{statement_indent}assign {target} = {expression};")
            next_sources.append(target)
        sources = tuple(next_sources)
    statements.append(f"{statement_indent}assign {result_name} = {sources[0]};")


def _functional_scatter_bounded_process_rendering(
    region: expr.FunctionalRegion,
    plan: functional.FunctionalRegionEmissionPlan,
    lowering: scatter_lowering.FunctionalScatterLoweringPlan,
    *,
    declaration_indent: str,
    statement_indent: str,
    outer_materialized: tuple[materialization.MaterializedExpression, ...] = (),
) -> tuple[list[str], list[str]]:
    """Render independent bounded accumulators and a balanced OR tree."""

    scatter, first_entry = _validated_scatter(plan)

    table_by_name = {
        table.name: table
        for owner in (region, *(dimension.region for dimension in scatter.dimensions))
        for table in owner.tables
    }
    outer_aliases, _local, dependent_temporaries, invariant_temporaries = (
        _scatter_temporaries(scatter, outer_materialized)
    )

    declarations = [
        _temporary_declaration(item, item.name, declaration_indent)
        for item in invariant_temporaries
    ]
    statements: list[str] = []
    invariant_aliases = materialization.ExpressionAliasMap(
        (
            *((item.expression, item.name) for item in outer_materialized),
            *((item.expression, item.name) for item in invariant_temporaries),
        )
    )
    with emission_context.functional_expression_scope(functional.FunctionalExpressionContext((), (), ())):
        for item in invariant_temporaries:
            rewritten = materialization.replace_materialized(
                item.expression,
                invariant_aliases,
                keep=item.expression,
            )
            statements.append(
                f"{statement_indent}assign {item.name} = {sv_expression._expression(rewritten)};"
            )

    all_captures = _scatter_captures(scatter)

    for chunk in lowering.chunks:
        declarations.append(
            f"{declaration_indent}logic "
            f"[{lowering.result_width - 1}:0] {chunk.accumulator};"
        )
        for loop_variable in chunk.loop_variables:
            declarations.append(f"{declaration_indent}integer {loop_variable};")
        declarations.extend(
            (
                f"{declaration_indent}logic {chunk.enable_temporary};",
                f"{declaration_indent}logic "
                f"{sv_rendering._range(sv_rendering._width(first_entry.address.type))}{chunk.address_temporary};",
                f"{declaration_indent}logic "
                f"{sv_rendering._range(sv_rendering._width(first_entry.value.type))}{chunk.value_temporary};",
            )
        )
        dependent_names = tuple(
            (item, f"{chunk.accumulator}_expr_{ordinal}")
            for ordinal, item in enumerate(dependent_temporaries)
        )
        declarations.extend(
            _temporary_declaration(item, name, declaration_indent)
            for item, name in dependent_names
        )
        table_names: list[tuple[expr.FunctionalTableLookup, str]] = []
        table_names_by_dimension: dict[
            int, tuple[tuple[expr.FunctionalTableLookup, str], ...]
        ] = {}
        for dimension_ordinal, dimension in enumerate(scatter.dimensions):
            owned_names = tuple(
                (
                    lookup,
                    f"{chunk.accumulator}_table_{dimension_ordinal}_{ordinal}",
                )
                for ordinal, (lookup, _temporary) in enumerate(
                    dimension.table_temporaries
                )
            )
            table_names_by_dimension[dimension_ordinal] = owned_names
            table_names.extend(owned_names)
            for lookup, name in owned_names:
                signed = (
                    " signed"
                    if isinstance(lookup.type, (ir_types.SIntType, ir_types.FixedType))
                    else ""
                )
                declarations.append(
                    f"{declaration_indent}logic{signed} "
                    f"{sv_rendering._range(sv_rendering._width(lookup.type))}{name};"
                )

        aliases = materialization.ExpressionAliasMap(
            (
                *((item.expression, item.name) for item in outer_materialized),
                *((item.expression, item.name) for item in invariant_temporaries),
                *((item.expression, name) for item, name in dependent_names),
            )
        )
        fixed_binders = tuple(
            (identity, str(value)) for identity, value in chunk.fixed_binders
        )
        remaining_binders = tuple(
            (
                scatter.dimensions[dimension_ordinal].region.binder.identity,
                loop_variable,
            )
            for dimension_ordinal, loop_variable in zip(
                chunk.remaining_dimension_ordinals,
                chunk.loop_variables,
            )
        )
        context = functional.FunctionalExpressionContext(
            (*fixed_binders, *remaining_binders),
            all_captures,
            tuple(table_names),
        )
        statements.extend(
            (
                f"{statement_indent}always_comb begin",
                f"{statement_indent}  {chunk.accumulator} = '0;",
                f"{statement_indent}  {chunk.enable_temporary} = '0;",
                f"{statement_indent}  {chunk.address_temporary} = '0;",
                f"{statement_indent}  {chunk.value_temporary} = '0;",
            )
        )
        for _item, name in dependent_names:
            statements.append(f"{statement_indent}  {name} = '0;")
        for _lookup, name in table_names:
            statements.append(f"{statement_indent}  {name} = '0;")

        def render_tables(dimension_ordinal: int, indent: str) -> None:
            for lookup, temporary in table_names_by_dimension[dimension_ordinal]:
                table = table_by_name.get(lookup.table_name)
                if table is None:
                    raise SystemVerilogEmissionError(
                        f"functional table '{lookup.table_name}' is not declared"
                    )
                statements.append(
                    f"{indent}case ({sv_expression._compile_time_expression(lookup.index)})"
                )
                for offset, value in enumerate(table.values):
                    statements.append(
                        f"{indent}  {table.start + offset}: {temporary} = "
                        f"{sv_expression._expression(value)};"
                    )
                statements.append(f"{indent}endcase")

        def render_entries(indent: str) -> None:
            for item, name in dependent_names:
                rewritten = materialization.replace_materialized(
                    item.expression,
                    aliases,
                    keep=item.expression,
                )
                statements.append(f"{indent}{name} = {sv_expression._expression(rewritten)};")
            for entry in scatter.entries[chunk.entry_start:chunk.entry_stop]:
                statements.extend(
                    (
                        f"{indent}{chunk.enable_temporary} = "
                        f"{sv_expression._expression(materialization.replace_materialized(entry.enable, aliases))};",
                        f"{indent}{chunk.address_temporary} = "
                        f"{sv_expression._expression(materialization.replace_materialized(entry.address, aliases))};",
                        f"{indent}{chunk.value_temporary} = "
                        f"{sv_expression._expression(materialization.replace_materialized(entry.value, aliases))};",
                    )
                )
                address_width = sv_rendering._width(entry.address.type)
                guard = (
                    "1'b1"
                    if region.type.length >= 1 << address_width
                    else (
                        f"($unsigned({chunk.address_temporary}) < "
                        f"{address_width}'d{region.type.length})"
                    )
                )
                base = (
                    f"($unsigned({chunk.address_temporary}) * "
                    f"{plan.element_width})"
                )
                destination = (
                    f"{chunk.accumulator}[{base} +: {plan.element_width}]"
                )
                statements.extend(
                    (
                        f"{indent}if (({chunk.enable_temporary}) && ({guard})) begin",
                        f"{indent}  {destination} = "
                        f"{plan.element_width}'($unsigned({destination})) | "
                        f"{plan.element_width}'($unsigned({chunk.value_temporary}));",
                        f"{indent}end",
                    )
                )

        def render_dimensions(position: int, indent: str) -> None:
            if position == len(chunk.remaining_dimension_ordinals):
                render_entries(indent)
                return
            dimension_ordinal = chunk.remaining_dimension_ordinals[position]
            dimension = scatter.dimensions[dimension_ordinal]
            loop_variable = chunk.loop_variables[position]
            owned = dimension.region
            statements.append(
                f"{indent}for ({loop_variable} = {owned.binder.start}; "
                f"{loop_variable} < {owned.binder.stop}; "
                f"{loop_variable} = {loop_variable} + 1) begin"
            )
            inner = indent + "  "
            render_tables(dimension_ordinal, inner)
            render_dimensions(position + 1, inner)
            statements.append(f"{indent}end")

        with emission_context.functional_expression_scope(context):
            fixed_dimension_count = len(chunk.fixed_binders)
            for dimension_ordinal in range(fixed_dimension_count):
                render_tables(dimension_ordinal, statement_indent + "  ")
            render_dimensions(0, statement_indent + "  ")
        statements.append(f"{statement_indent}end")

    _append_scatter_reduction(
        lowering,
        plan.result_name,
        declarations,
        statements,
        declaration_indent=declaration_indent,
        statement_indent=statement_indent,
    )
    return declarations, statements

def _functional_scatter_rendering(
    region: expr.FunctionalRegion,
    plan: functional.FunctionalRegionEmissionPlan,
    *,
    declaration_indent: str,
    statement_indent: str,
    outer_materialized: tuple[materialization.MaterializedExpression, ...] = (),
) -> tuple[list[str], list[str]]:
    scatter, first_entry = _validated_scatter(plan)
    declarations: list[str] = []
    table_by_name = {
        table.name: table
        for owner in (region, *(dimension.region for dimension in scatter.dimensions))
        for table in owner.tables
    }
    for dimension in scatter.dimensions:
        declarations.append(
            f"{declaration_indent}integer {dimension.loop_variable};"
        )
        for lookup, temporary in dimension.table_temporaries:
            signed = (
                " signed"
                if isinstance(lookup.type, (ir_types.SIntType, ir_types.FixedType))
                else ""
            )
            declarations.append(
                f"{declaration_indent}logic{signed} "
                f"{sv_rendering._range(sv_rendering._width(lookup.type))}{temporary};"
            )
    declarations.extend(
        (
            f"{declaration_indent}logic {scatter.enable_temporary};",
            f"{declaration_indent}logic "
            f"{sv_rendering._range(sv_rendering._width(first_entry.address.type))}{scatter.address_temporary};",
            f"{declaration_indent}logic "
            f"{sv_rendering._range(sv_rendering._width(first_entry.value.type))}{scatter.value_temporary};",
        )
    )
    outer_aliases, local_temporaries, dependent_temporaries, invariant_temporaries = (
        _scatter_temporaries(scatter, outer_materialized)
    )
    declarations.extend(
        _temporary_declaration(item, item.name, declaration_indent)
        for item in local_temporaries
    )

    aliases = materialization.ExpressionAliasMap(
        (
            *((item.expression, item.name) for item in outer_materialized),
            *((item.expression, item.name) for item in local_temporaries),
        )
    )

    statements = [f"{statement_indent}{plan.result_name} = '0;"]
    with emission_context.functional_expression_scope(functional.FunctionalExpressionContext((), (), ())):
        for item in invariant_temporaries:
            rewritten = materialization.replace_materialized(
                item.expression,
                aliases,
                keep=item.expression,
            )
            statements.append(
                f"{statement_indent}{item.name} = {sv_expression._expression(rewritten)};"
            )

    def render_entry(entry: functional.FunctionalScatterEntry, indent: str) -> None:
        statements.extend(
            (
                f"{indent}{scatter.enable_temporary} = "
                f"{sv_expression._expression(materialization.replace_materialized(entry.enable, aliases))};",
                f"{indent}{scatter.address_temporary} = "
                f"{sv_expression._expression(materialization.replace_materialized(entry.address, aliases))};",
                f"{indent}{scatter.value_temporary} = "
                f"{sv_expression._expression(materialization.replace_materialized(entry.value, aliases))};",
            )
        )
        address = scatter.address_temporary
        enabled = scatter.enable_temporary
        value = scatter.value_temporary
        address_width = sv_rendering._width(entry.address.type)
        guard = (
            "1'b1"
            if region.type.length >= 1 << address_width
            else f"($unsigned({address}) < {address_width}'d{region.type.length})"
        )
        base = sv_rendering._scaled_packed_index(
            f"$unsigned({address})", plan.element_width
        )
        destination = f"{plan.result_name}[{base} +: {plan.element_width}]"
        statements.extend(
            (
                f"{indent}if (({enabled}) && ({guard})) begin",
                f"{indent}  {destination} = "
                f"{plan.element_width}'($unsigned({destination})) | "
                f"{plan.element_width}'($unsigned({value}));",
                f"{indent}end",
            )
        )

    def render_dimension(
        ordinal: int,
        indent: str,
        parent: functional.FunctionalExpressionContext,
    ) -> None:
        dimension = scatter.dimensions[ordinal]
        owned = dimension.region
        context = functional.FunctionalExpressionContext(
            binders=(
                *parent.binders,
                (owned.binder.identity, dimension.loop_variable),
            ),
            captures=(
                *((reference.identity, value) for reference, value in owned.captures),
                *parent.captures,
            ),
            table_temporaries=(
                *dimension.table_temporaries,
                *parent.table_temporaries,
            ),
        )
        statements.extend(
            (
                f"{indent}for ({dimension.loop_variable} = {owned.binder.start}; "
                f"{dimension.loop_variable} < {owned.binder.stop}; "
                f"{dimension.loop_variable} = "
                f"{dimension.loop_variable} + 1) begin",
            )
        )
        inner = indent + "  "
        with emission_context.functional_expression_scope(context):
            for lookup, temporary in dimension.table_temporaries:
                table = table_by_name.get(lookup.table_name)
                if table is None:
                    raise SystemVerilogEmissionError(
                        f"functional table '{lookup.table_name}' is not declared"
                    )
                statements.append(
                    f"{inner}case ({sv_expression._compile_time_expression(lookup.index)})"
                )
                for offset, value in enumerate(table.values):
                    statements.append(
                        f"{inner}  {table.start + offset}: {temporary} = "
                        f"{sv_expression._expression(value)};"
                    )
                statements.extend(
                    (f"{inner}  default: {temporary} = '0;", f"{inner}endcase")
                )
            if ordinal + 1 < len(scatter.dimensions):
                render_dimension(ordinal + 1, inner, context)
            else:
                for item in dependent_temporaries:
                    rewritten = materialization.replace_materialized(
                        item.expression,
                        aliases,
                        keep=item.expression,
                    )
                    statements.append(
                        f"{inner}{item.name} = {sv_expression._expression(rewritten)};"
                    )
                for entry in scatter.entries:
                    render_entry(entry, inner)
        statements.append(f"{indent}end")

    root_context = functional.FunctionalExpressionContext((), (), ())
    if scatter.dimensions:
        render_dimension(0, statement_indent, root_context)
    else:
        with emission_context.functional_expression_scope(root_context):
            for item in dependent_temporaries:
                rewritten = materialization.replace_materialized(
                    item.expression,
                    aliases,
                    keep=item.expression,
                )
                statements.append(
                    f"{statement_indent}{item.name} = {sv_expression._expression(rewritten)};"
                )
            for entry in scatter.entries:
                render_entry(entry, statement_indent)
    return declarations, statements
