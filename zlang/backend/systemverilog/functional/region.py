# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative functional-region region SystemVerilog emission."""

from __future__ import annotations


from zlang.common import stable_digest
from zlang.ir import expressions as expr
from zlang.ir import types as ir_types
from zlang.backend import expression_materialization as materialization
from zlang.backend import identifiers as identifiers
from zlang.backend.systemverilog import context as emission_context
from zlang.backend.systemverilog import functional as functional
from zlang.backend.systemverilog.functional import reduction as functional_reduction
from zlang.backend.systemverilog.functional import scatter as functional_scatter
from zlang.ir import signed_reductions as signed_reductions
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering

_FUNCTIONAL_REGION_PLANNER = functional.FunctionalRegionPlanner(error=SystemVerilogEmissionError)

def functional_region_plan(
    region: expr.FunctionalRegion,
    result_name: str,
    *,
    reserved_names: tuple[str, ...] = (),
) -> functional.FunctionalRegionEmissionPlan:
    identity = stable_digest(
        {
            "schema": materialization.FUNCTIONAL_REGION_EMISSION_SCHEMA,
            "expression": signed_reductions.expression_semantic_identity(region),
            "result": result_name,
        }
    )
    prefix = identity[:10]
    used = set(reserved_names) | {result_name}
    scatter = functional_scatter._functional_scatter_plan(
        region,
        owner_identity=identity,
        used=used,
    )
    if scatter is not None:
        return functional.FunctionalRegionEmissionPlan(
            identity=identity,
            result_name=result_name,
            loop_variable="",
            element_width=sv_rendering._width(region.type.element_type),
            table_temporaries=(),
            expression_temporaries=(),
            scatter=scatter,
        )
    loop_variable = identifiers.allocate_private_rtl_identifier(
        f"region_i_{prefix}",
        semantic_identity=f"{identity}:binder:{region.binder.identity}",
        used=used,
    )
    reduction_temporaries = tuple(
        functional_reduction._functional_reduction_plan(
            reduction,
            owner_identity=identity,
            ordinal=ordinal,
            used=used,
        )
        for ordinal, reduction in enumerate(
            _FUNCTIONAL_REGION_PLANNER.compact_reductions(region.template)
        )
    )
    materialized = [
        item
        for item in materialization.plan_materialization(
            (region.template,),
            reserved_names=tuple(used),
            generated_prefix=f"zlang_region_expr_{prefix}_",
            scope=f"functional_region:{identity}",
        )
        if not _FUNCTIONAL_REGION_PLANNER.contains_compact_reduction(
            item.expression
        )
    ]
    used.update(item.name for item in materialized)
    selected = materialization.ExpressionAliasMap(
        (item.expression, item.name) for item in materialized
    )
    scalar_leaves = (
        expr.Constant,
        expr.InputRef,
        expr.ParameterRef,
        expr.RegisterRef,
        expr.InstanceOutputRef,
        expr.FunctionalCaptureRef,
        expr.FunctionalValue,
        expr.FunctionalTableLookup,
    )

    def partition(value: expr.Expression, *, root: bool = False) -> int:
        if value in selected:
            return 1
        effective_size = 1 + sum(
            partition(child) for child in materialization.expression_children(value)
        )
        # Bound every procedural RHS independently of expression sharing.  A
        # small structural threshold is deliberate: exact-width casts and
        # signed comparisons can render far wider than their IR node count.
        if (
            not root
            and effective_size > 16
            and not isinstance(value, scalar_leaves)
            and not _FUNCTIONAL_REGION_PLANNER.contains_compact_reduction(value)
        ):
            temporary = identifiers.allocate_private_rtl_identifier(
                f"region_expr_{prefix}",
                semantic_identity=(
                    f"{identity}:expression:{signed_reductions.expression_semantic_identity(value)}"
                ),
                used=used,
            )
            materialized.append(materialization.MaterializedExpression(value, temporary))
            selected[value] = temporary
            return 1
        return effective_size

    partition(region.template, root=True)
    table_temporaries: list[tuple[expr.FunctionalTableLookup, str]] = []
    for index, lookup in enumerate(
        _FUNCTIONAL_REGION_PLANNER.table_lookups(region.template)
    ):
        temporary = identifiers.allocate_private_rtl_identifier(
            f"region_table_{prefix}_{index}",
            semantic_identity=(
                f"{identity}:table:{lookup.table_name}:"
                f"{signed_reductions.expression_semantic_identity(lookup)}"
            ),
            used=used,
        )
        table_temporaries.append((lookup, temporary))
    return functional.FunctionalRegionEmissionPlan(
        identity=identity,
        result_name=result_name,
        loop_variable=loop_variable,
        element_width=sv_rendering._width(region.type.element_type),
        table_temporaries=tuple(table_temporaries),
        expression_temporaries=tuple(materialized),
        reduction_temporaries=reduction_temporaries,
    )

def _functional_region_rendering(
    region: expr.FunctionalRegion,
    plan: functional.FunctionalRegionEmissionPlan,
    *,
    declaration_indent: str,
    statement_indent: str,
    outer_materialized: tuple[materialization.MaterializedExpression, ...] = (),
) -> tuple[list[str], list[str]]:
    """Render declarations and procedural statements for one region plan."""

    if plan.scatter is not None:
        return functional_scatter._functional_scatter_rendering(
            region,
            plan,
            declaration_indent=declaration_indent,
            statement_indent=statement_indent,
            outer_materialized=outer_materialized,
        )

    table_by_name = {table.name: table for table in region.tables}
    context = functional.FunctionalExpressionContext(
        binders=((region.binder.identity, plan.loop_variable),),
        captures=tuple(
            (reference.identity, value) for reference, value in region.captures
        ),
        table_temporaries=plan.table_temporaries,
    )
    declarations = [f"{declaration_indent}integer {plan.loop_variable};"]
    for lookup, temporary in plan.table_temporaries:
        signed = " signed" if isinstance(lookup.type, (ir_types.SIntType, ir_types.FixedType)) else ""
        declarations.append(
            f"{declaration_indent}logic{signed} {sv_rendering._range(sv_rendering._width(lookup.type))}{temporary};"
        )
    for item in plan.expression_temporaries:
        signed = (
            " signed" if isinstance(item.expression.type, (ir_types.SIntType, ir_types.FixedType)) else ""
        )
        declarations.append(
            f"{declaration_indent}logic{signed} "
            f"{sv_rendering._range(sv_rendering._width(item.expression.type))}{item.name};"
        )
    for reduction in plan.reduction_temporaries:
        declarations.extend(
            functional_reduction._functional_reduction_declarations(reduction, declaration_indent)
        )

    aliases = materialization.ExpressionAliasMap(
        (item.expression, item.name) for item in plan.expression_temporaries
    )
    aliases.update(
        (item.reduction, item.accumulator)
        for item in plan.reduction_temporaries
    )
    invariant: list[str] = []
    dependent: list[str] = []
    with emission_context.functional_expression_scope(context):
        for item in materialization.dependency_ordered_materialization(
            plan.expression_temporaries
        ):
            rewritten = materialization.replace_materialized(
                item.expression,
                aliases,
                keep=item.expression,
            )
            assignment = f"{item.name} = {sv_expression._expression(rewritten)};"
            target = (
                dependent
                if _FUNCTIONAL_REGION_PLANNER.binder_dependent(
                    item.expression, region.binder.identity
                )
                else invariant
            )
            target.append(assignment)

        statements = [
            *(f"{statement_indent}{line}" for line in invariant),
            f"{statement_indent}{plan.result_name} = '0;",
            f"{statement_indent}for ({plan.loop_variable} = {region.binder.start}; "
            f"{plan.loop_variable} < {region.binder.stop}; "
            f"{plan.loop_variable} = {plan.loop_variable} + 1) begin",
        ]
        inner = statement_indent + "  "
        for lookup, temporary in plan.table_temporaries:
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
        for reduction in plan.reduction_temporaries:
            statements.extend(
                functional_reduction._functional_reduction_statements(reduction, inner)
            )
        statements.extend(f"{inner}{line}" for line in dependent)
        rewritten_template = materialization.replace_materialized(region.template, aliases)
        base = (
            f"(({plan.loop_variable} - {region.binder.start}) * "
            f"{plan.element_width})"
        )
        statements.append(
            f"{inner}{plan.result_name}[{base} +: {plan.element_width}] = "
            f"{sv_expression._expression(rewritten_template)};"
        )
        statements.append(f"{statement_indent}end")
    return declarations, statements

def _functional_region_generate_rendering(
    region: expr.FunctionalRegion,
    plan: functional.FunctionalRegionEmissionPlan,
    *,
    declaration_indent: str,
    statement_indent: str,
    outer_materialized: tuple[materialization.MaterializedExpression, ...] = (),
) -> tuple[list[str], list[str]] | None:
    """Render a pure element map as structural generate assignments.

    Each iteration owns one disjoint packed result slice.  Expressing that
    fact structurally avoids the priority/update network that synthesis tools
    must conservatively construct for a procedural loop writing a variable
    indexed part-select.  Tables, reductions and scatters retain their
    specialized statement-owned lowering.
    """

    if plan.scatter is not None:
        return functional_scatter._functional_scatter_generate_rendering(
            region,
            plan,
            declaration_indent=declaration_indent,
            statement_indent=statement_indent,
            outer_materialized=outer_materialized,
        )
    if plan.table_temporaries or plan.reduction_temporaries:
        return None

    aliases = materialization.ExpressionAliasMap(
        (item.expression, item.name) for item in plan.expression_temporaries
    )
    ordered = materialization.dependency_ordered_materialization(plan.expression_temporaries)
    dependent = tuple(
        item
        for item in ordered
        if _FUNCTIONAL_REGION_PLANNER.binder_dependent(
            item.expression, region.binder.identity
        )
    )
    dependent_ids = {id(item) for item in dependent}
    invariant = tuple(item for item in ordered if id(item) not in dependent_ids)
    declarations: list[str] = []
    statements: list[str] = []
    context = functional.FunctionalExpressionContext(
        binders=((region.binder.identity, plan.loop_variable),),
        captures=tuple(
            (reference.identity, value) for reference, value in region.captures
        ),
        table_temporaries=(),
    )

    def declaration(item: materialization.MaterializedExpression, indent: str) -> str:
        signed = (
            " signed"
            if isinstance(item.expression.type, (ir_types.SIntType, ir_types.FixedType))
            else ""
        )
        return (
            f"{indent}logic{signed} "
            f"{sv_rendering._range(sv_rendering._width(item.expression.type))}{item.name};"
        )

    with emission_context.functional_expression_scope(context):
        for item in invariant:
            declarations.append(declaration(item, declaration_indent))
            rewritten = materialization.replace_materialized(
                item.expression, aliases, keep=item.expression
            )
            statements.append(
                f"{statement_indent}assign {item.name} = "
                f"{sv_expression._expression(rewritten)};"
            )
        statements.extend(
            (
                f"{statement_indent}generate",
                f"{statement_indent}  for (genvar {plan.loop_variable} = "
                f"{region.binder.start}; {plan.loop_variable} < "
                f"{region.binder.stop}; {plan.loop_variable} = "
                f"{plan.loop_variable} + 1) begin : "
                f"zlang_region_generate_{plan.identity[:10]}",
            )
        )
        for item in dependent:
            statements.append(declaration(item, statement_indent + "    "))
            rewritten = materialization.replace_materialized(
                item.expression, aliases, keep=item.expression
            )
            statements.append(
                f"{statement_indent}    assign {item.name} = "
                f"{sv_expression._expression(rewritten)};"
            )
        rewritten_template = materialization.replace_materialized(region.template, aliases)
        base = (
            f"(({plan.loop_variable} - {region.binder.start}) * "
            f"{plan.element_width})"
        )
        statements.extend(
            (
                f"{statement_indent}    assign {plan.result_name}[{base} +: "
                f"{plan.element_width}] = {sv_expression._expression(rewritten_template)};",
                f"{statement_indent}  end",
                f"{statement_indent}endgenerate",
            )
        )
    return declarations, statements

def _functional_region_replication(
    region: expr.FunctionalRegion,
) -> str | None:
    """Render a binder-independent region as one packed replication."""

    if _FUNCTIONAL_REGION_PLANNER.binder_dependent(
        region.template, region.binder.identity
    ):
        return None
    if _FUNCTIONAL_REGION_PLANNER.table_lookups(region.template):
        return None
    context = functional.FunctionalExpressionContext(
        binders=(),
        captures=tuple(
            (reference.identity, value) for reference, value in region.captures
        ),
        table_temporaries=(),
    )
    with emission_context.functional_expression_scope(context):
        element = sv_expression._expression(region.template)
    length = region.binder.stop - region.binder.start
    return "{" + f"{length}{{{sv_rendering._width(region.type.element_type)}'({element})}}" + "}"
