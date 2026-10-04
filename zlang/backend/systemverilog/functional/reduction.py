# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative functional-region reduction SystemVerilog emission."""

from __future__ import annotations


from zlang.ir import expressions as expr
from zlang.ir import types as ir_types
from zlang.backend import expression_materialization as materialization
from zlang.backend import identifiers as identifiers
from zlang.backend.systemverilog import context as emission_context
from zlang.backend.systemverilog import functional as functional
from zlang.ir import signed_reductions as signed_reductions
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering

_FUNCTIONAL_REGION_PLANNER = functional.FunctionalRegionPlanner(error=SystemVerilogEmissionError)

def _functional_reduction_plan(
    reduction: expr.Reduce,
    *,
    owner_identity: str,
    ordinal: int,
    used: set[str],
) -> functional.FunctionalReductionEmissionPlan:
    region = reduction.collection
    assert isinstance(region, expr.FunctionalRegion)
    identity = signed_reductions.expression_semantic_identity(reduction)
    accumulator = identifiers.allocate_private_rtl_identifier(
        f"region_reduce_{identity[:10]}",
        semantic_identity=f"{owner_identity}:reduction:{ordinal}:{identity}:value",
        used=used,
    )
    loop_variable = identifiers.allocate_private_rtl_identifier(
        f"region_reduce_i_{identity[:10]}",
        semantic_identity=f"{owner_identity}:reduction:{ordinal}:{identity}:binder",
        used=used,
    )
    table_temporaries: list[tuple[expr.FunctionalTableLookup, str]] = []
    for table_ordinal, lookup in enumerate(
        _FUNCTIONAL_REGION_PLANNER.table_lookups(region.template)
    ):
        name = identifiers.allocate_private_rtl_identifier(
            f"region_reduce_table_{identity[:10]}_{table_ordinal}",
            semantic_identity=(
                f"{owner_identity}:reduction:{ordinal}:{identity}:"
                f"table:{lookup.table_name}"
            ),
            used=used,
        )
        table_temporaries.append((lookup, name))
    children = tuple(
        _functional_reduction_plan(
            child,
            owner_identity=f"{owner_identity}:reduction:{ordinal}:{identity}",
            ordinal=child_ordinal,
            used=used,
        )
        for child_ordinal, child in enumerate(
            _FUNCTIONAL_REGION_PLANNER.compact_reductions(region.template)
        )
    )
    return functional.FunctionalReductionEmissionPlan(
        reduction,
        accumulator,
        loop_variable,
        tuple(table_temporaries),
        children,
    )

def _functional_reduction_declarations(
    plan: functional.FunctionalReductionEmissionPlan,
    indent: str,
) -> list[str]:
    signed = (
        " signed"
        if isinstance(plan.reduction.type, (ir_types.SIntType, ir_types.FixedType))
        else ""
    )
    declarations = [
        f"{indent}logic{signed} {sv_rendering._range(sv_rendering._width(plan.reduction.type))}"
        f"{plan.accumulator};",
        f"{indent}integer {plan.loop_variable};",
    ]
    for lookup, temporary in plan.table_temporaries:
        table_signed = (
            " signed" if isinstance(lookup.type, (ir_types.SIntType, ir_types.FixedType)) else ""
        )
        declarations.append(
            f"{indent}logic{table_signed} {sv_rendering._range(sv_rendering._width(lookup.type))}{temporary};"
        )
    for child in plan.children:
        declarations.extend(_functional_reduction_declarations(child, indent))
    return declarations

def _functional_reduction_statements(
    plan: functional.FunctionalReductionEmissionPlan,
    indent: str,
) -> list[str]:
    reduction = plan.reduction
    region = reduction.collection
    assert isinstance(region, expr.FunctionalRegion)
    parent = emission_context.current_functional_expression()
    if parent is None:
        raise SystemVerilogEmissionError(
            "functional reduction requires an enclosing region context"
        )
    table_by_name = {table.name: table for table in region.tables}
    context = functional.FunctionalExpressionContext(
        binders=(*parent.binders, (region.binder.identity, plan.loop_variable)),
        captures=(
            *((reference.identity, value) for reference, value in region.captures),
            *parent.captures,
        ),
        table_temporaries=(*plan.table_temporaries, *parent.table_temporaries),
    )
    identity = (
        "'1"
        if reduction.operator is expr.ReductionOperator.BIT_AND
        else "'0"
    )
    lines = [
        f"{indent}{plan.accumulator} = {identity};",
        f"{indent}for ({plan.loop_variable} = {region.binder.start}; "
        f"{plan.loop_variable} < {region.binder.stop}; "
        f"{plan.loop_variable} = {plan.loop_variable} + 1) begin",
    ]
    inner = indent + "  "
    with emission_context.functional_expression_scope(context):
        for lookup, temporary in plan.table_temporaries:
            table = table_by_name.get(lookup.table_name)
            if table is None:
                raise SystemVerilogEmissionError(
                    f"functional table '{lookup.table_name}' is not declared"
                )
            lines.append(f"{inner}case ({sv_expression._compile_time_expression(lookup.index)})")
            for offset, value in enumerate(table.values):
                lines.append(
                    f"{inner}  {table.start + offset}: {temporary} = "
                    f"{sv_expression._expression(value)};"
                )
            lines.extend(
                (f"{inner}  default: {temporary} = '0;", f"{inner}endcase")
            )
        for child in plan.children:
            lines.extend(_functional_reduction_statements(child, inner))
        aliases = {
            child.reduction: child.accumulator for child in plan.children
        }
        template = materialization.replace_materialized(region.template, aliases)
        if reduction.operator is expr.ReductionOperator.ADD:
            combined = expr.Add(
                expr.InputRef(plan.accumulator, reduction.type),
                template,
                reduction.type,
            )
        else:
            operator = {
                expr.ReductionOperator.BIT_AND: expr.BinaryOperator.BIT_AND,
                expr.ReductionOperator.BIT_OR: expr.BinaryOperator.BIT_OR,
                expr.ReductionOperator.BIT_XOR: expr.BinaryOperator.BIT_XOR,
            }[reduction.operator]
            combined = expr.Binary(
                operator,
                expr.InputRef(plan.accumulator, reduction.type),
                template,
                reduction.type,
                reduction.type,
            )
        lines.append(f"{inner}{plan.accumulator} = {sv_expression._expression(combined)};")
    lines.append(f"{indent}end")
    return lines
