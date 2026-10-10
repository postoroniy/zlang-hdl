"""Materialized expression and functional-region SystemVerilog emission."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from typing import Callable

from zlang.ir import expressions as expr
from zlang.ir.traversal import walk_expression
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.backend import expression_materialization as materialization
from zlang.backend import identifiers as identifiers
from zlang.backend import naming as naming
from zlang.backend.systemverilog import context as emission_context
from zlang.backend.systemverilog import functional as functional
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.ir import functional_regions as functional_regions
from zlang.ir import signed_reductions as signed_reductions
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering
from zlang.backend.systemverilog.functional import region as functional_region


_FUNCTIONAL_REGION_PLANNER = functional.FunctionalRegionPlanner(
    error=SystemVerilogEmissionError
)


class ModuleMaterializationOwner:
    """Own physical expression preparation for one emitted module.

    Namespace validation, staging discovery, and the final renderer all inspect
    the same immutable expression DAG.  This owner retains the physicalized
    nodes, roots, and materialization plan so those consumers cannot rebuild
    subtly different copies or repeat graph work.
    """

    def __init__(
        self,
        module: ir_module.Module,
        names: naming.ModuleRtlNames,
    ) -> None:
        self.module = module
        self.names = names
        self._instance_names = frozenset(
            item.instance.name for item in module.elaborated_instances
        )
        self._physicalized: dict[int, tuple[object, object]] = {}
        self._roots: tuple[expr.Expression, ...] | None = None
        self._plan: tuple[materialization.MaterializedExpression, ...] | None = None

    def physicalize(self, expression: expr.Expression) -> expr.Expression:
        """Replace hierarchical references once while preserving DAG sharing."""

        if not self._instance_names:
            return expression
        result = self._rewrite_value(expression)
        if not isinstance(result, expr.Expression):
            raise SystemVerilogEmissionError(
                "module expression physicalization lost typed expression IR"
            )
        return result

    def _rewrite_value(self, value: object) -> object:
        cacheable = isinstance(value, (expr.Expression, tuple)) or (
            is_dataclass(value) and not isinstance(value, type)
        )
        if cacheable:
            cached = self._physicalized.get(id(value))
            if cached is not None and cached[0] is value:
                return cached[1]

        if isinstance(value, expr.InstanceOutputRef):
            protocol_field = ir_interfaces.parse_ready_valid_field_name(value.port)
            result: object = expr.InputRef(
                (
                    self.names.child_signal(
                        value.instance,
                        protocol_field[0],
                        protocol_field[1].value,
                    )
                    if protocol_field is not None
                    else self.names.child_signal(value.instance, value.port)
                ),
                value.type,
                origin=value.origin,
            )
        elif (
            isinstance(value, expr.FieldAccess)
            and isinstance(value.expression, expr.InputRef)
            and value.expression.name in self._instance_names
        ):
            result = expr.InputRef(
                self.names.child_signal(value.expression.name, value.field),
                value.type,
                origin=value.origin,
            )
        elif isinstance(value, tuple):
            rewritten = tuple(self._rewrite_value(item) for item in value)
            result = (
                value
                if all(
                    before is after
                    for before, after in zip(value, rewritten, strict=True)
                )
                else rewritten
            )
        elif is_dataclass(value) and not isinstance(value, type):
            updates: dict[str, object] = {}
            for descriptor in fields(value):
                if descriptor.name == "origin" or not descriptor.init:
                    continue
                current = getattr(value, descriptor.name)
                rewritten = self._rewrite_value(current)
                if rewritten is not current:
                    updates[descriptor.name] = rewritten
            if updates:
                try:
                    result = replace(value, **updates)
                except (TypeError, ValueError):
                    result = value
            else:
                result = value
        else:
            result = value

        if cacheable:
            self._physicalized[id(value)] = (value, result)
        return result

    @property
    def roots(self) -> tuple[expr.Expression, ...]:
        if self._roots is None:
            self._roots = self._build_roots()
        return self._roots

    def _build_roots(self) -> tuple[expr.Expression, ...]:
        return materialization.module_expression_roots(
            self.module,
            normalize=self.physicalize,
        )

    @property
    def materialization_plan(
        self,
    ) -> tuple[materialization.MaterializedExpression, ...]:
        if self._plan is None:
            self._plan = self._build_materialization_plan()
        return self._plan

    def _build_materialization_plan(
        self,
    ) -> tuple[materialization.MaterializedExpression, ...]:
        preferred = tuple(
            (
                self.physicalize(local.expression),
                sv_rendering._identifier(local.name),
            )
            for local in self.module.locals
            if not local.compile_time
        )
        reserved_names = {
            sv_rendering._identifier(item.name)
            for item in (
                *self.module.ports,
                *self.module.registers,
                *self.module.locals,
            )
        }
        reserved_names.update(item.physical_name for item in self.names.entries)
        planned = list(
            materialization.plan_materialization(
                self.roots,
                preferred_names=preferred,
                reserved_names=reserved_names,
            )
        )
        region_identity_memo: dict[int, str] = {}
        planned_regions = {
            signed_reductions.expression_merkle_identity(
                item.expression, region_identity_memo
            )
            for item in planned
            if isinstance(item.expression, expr.FunctionalRegion)
        }
        used_names = reserved_names | {item.name for item in planned}
        for root in self.roots:
            for region in _FUNCTIONAL_REGION_PLANNER.regions(root):
                internal_identity = signed_reductions.expression_merkle_identity(
                    region, region_identity_memo
                )
                if internal_identity in planned_regions:
                    continue
                identity = signed_reductions.expression_semantic_identity(region)
                name = identifiers.allocate_private_rtl_identifier(
                    f"region_{identity[:10]}",
                    semantic_identity=(
                        f"{materialization.FUNCTIONAL_REGION_EMISSION_SCHEMA}:"
                        f"module:{identity}"
                    ),
                    used=used_names,
                )
                planned.append(materialization.MaterializedExpression(region, name))
                planned_regions.add(internal_identity)
        return tuple(planned)


def module_materialization_owner(
    module: ir_module.Module,
) -> ModuleMaterializationOwner:
    """Return the design-local owner for one exact module object."""

    names = emission_context.cached_module_rtl_names(module)
    emission = emission_context.current_emission_context()
    if emission is None:
        return ModuleMaterializationOwner(module, names)
    key = (id(module), id(names))
    cached = emission.materialization_owners.get(key)
    if cached is not None and cached[0] is module and cached[1] is names:
        owner = cached[2]
        if not isinstance(owner, ModuleMaterializationOwner):
            raise SystemVerilogEmissionError(
                "module materialization cache contains an incompatible owner"
            )
        return owner
    owner = ModuleMaterializationOwner(module, names)
    emission.materialization_owners[key] = (module, names, owner)
    return owner


def _continuous_value_assignments(
    name: str,
    value: expr.Expression,
    *,
    indent: str,
) -> tuple[str, ...]:
    """Render one exact value without rebuilding a wide aggregate RHS."""

    if isinstance(value, (expr.Generate, expr.Map)) and isinstance(
        value.type, ir_types.VecType
    ):
        element_width = sv_rendering._width(value.type.element_type)
        return tuple(
            f"{indent}assign {name}[{index * element_width} +: "
            f"{element_width}] = {sv_expression._expression(element)};"
            for index, element in enumerate(value.elements)
        )
    return (f"{indent}assign {name} = {sv_expression._expression(value)};",)


def _output_value_assignments(
    name: str,
    value: expr.Expression,
    render: Callable[[expr.Expression], str],
    *,
    indent: str,
) -> tuple[str, ...]:
    """Render a public value while retaining an exact packed-array shape."""

    boundary = emission_context.current_top_boundary()
    direct_vector = (
        boundary is not None
        and boundary.is_direct_vector(name)
        and isinstance(value.type, ir_types.VecType)
    )
    if isinstance(value, (expr.Generate, expr.Map)) and isinstance(
        value.type, ir_types.VecType
    ):
        element_width = sv_rendering._width(value.type.element_type)
        return tuple(
            (
                f"{indent}assign {identifiers.rtl_identifier(name)}[{index}] = "
                f"{render(element)};"
                if direct_vector
                else f"{indent}assign {sv_rendering._identifier(name)}"
                f"[{index * element_width} +: {element_width}] = "
                f"{render(element)};"
            )
            for index, element in enumerate(value.elements)
        )
    return (f"{indent}assign {sv_rendering._identifier(name)} = {render(value)};",)


def _contains_functional_scope_reference(value: object) -> bool:
    """Return whether ``value`` needs a FunctionalRegion rendering context."""
    return isinstance(value, expr.TracedExpression) and any(
        isinstance(
            item,
            (
                functional_regions.CompileTimeBinderRef,
                expr.FunctionalCaptureRef,
                expr.FunctionalValue,
                expr.FunctionalTableLookup,
                expr.FunctionalRegion,
            ),
        )
        for item in walk_expression(value)
    )


def _shared_functional_region_materialization(
    records: dict[
        str, tuple[expr.FunctionalRegion, functional.FunctionalRegionEmissionPlan]
    ],
    *,
    used_names: set[str],
) -> tuple[materialization.MaterializedExpression, ...]:
    """Select exact binder-free scatter invariants shared by several regions."""

    invariant_counts: dict[str, int] = {}
    invariant_values: dict[str, expr.Expression] = {}
    identity_memo: dict[int, str] = {}
    for _region, plan in records.values():
        if plan.scatter is None:
            continue
        binder_identities = tuple(
            dimension.region.binder.identity
            for dimension in plan.scatter.dimensions
        )
        for candidate in plan.scatter.expression_temporaries:
            if _contains_functional_scope_reference(candidate.expression) or any(
                _FUNCTIONAL_REGION_PLANNER.binder_dependent(
                    candidate.expression, identity
                )
                for identity in binder_identities
            ):
                continue
            identity = signed_reductions.expression_merkle_identity(
                candidate.expression, identity_memo
            )
            invariant_counts[identity] = invariant_counts.get(identity, 0) + 1
            invariant_values.setdefault(identity, candidate.expression)

    return tuple(
        materialization.MaterializedExpression(
            invariant_values[identity],
            identifiers.allocate_private_rtl_identifier(
                f"zlang_region_shared_{ordinal}",
                semantic_identity=(
                    f"{materialization.FUNCTIONAL_REGION_EMISSION_SCHEMA}:shared:{identity}"
                ),
                used=used_names,
            ),
        )
        for ordinal, identity in enumerate(
            sorted(key for key, count in invariant_counts.items() if count > 1)
        )
    )


def _append_materialized_functional_region(
    item: materialization.MaterializedExpression,
    *,
    aliases: materialization.ExpressionAliasMap,
    records: dict[str, tuple[expr.FunctionalRegion, functional.FunctionalRegionEmissionPlan]],
    outer_materialized: tuple[materialization.MaterializedExpression, ...],
    declarations: list[str],
    statements: list[str],
) -> None:
    """Render one planned region for combinational or staged module owners."""

    region = records.get(item.name, (None, None))[0]
    if region is None:
        region = materialization.replace_materialized(
            item.expression,
            aliases,
            keep=item.expression,
            rewrite_region_owned=True,
        )
        assert isinstance(region, expr.FunctionalRegion)
    replication = functional_region._functional_region_replication(region)
    if replication is not None:
        statements.append(f"  assign {item.name} = {replication};")
        return
    plan = records[item.name][1]
    generated = functional_region._functional_region_generate_rendering(
        region,
        plan,
        declaration_indent="  ",
        statement_indent="  ",
        outer_materialized=outer_materialized,
    )
    if generated is not None:
        region_declarations, region_statements = generated
        declarations.extend(region_declarations)
        statements.extend(region_statements)
        return
    region_declarations, region_statements = functional_region._functional_region_rendering(
        region,
        plan,
        declaration_indent="  ",
        statement_indent="    ",
        outer_materialized=outer_materialized,
    )
    declarations.extend(region_declarations)
    statements.extend(("  always_comb begin", *region_statements, "  end"))


def _materialized_emission(module: ir_module.Module):
    owner = module_materialization_owner(module)
    names = owner.names
    materialized = owner.materialization_plan
    aliases = materialization.ExpressionAliasMap(
        (item.expression, item.name) for item in materialized
    )

    region_records: dict[
        str, tuple[expr.FunctionalRegion, functional.FunctionalRegionEmissionPlan]
    ] = {}
    for item in materialized:
        if not isinstance(item.expression, expr.FunctionalRegion):
            continue
        region = materialization.replace_materialized(
            item.expression,
            aliases,
            keep=item.expression,
            rewrite_region_owned=True,
        )
        assert isinstance(region, expr.FunctionalRegion)
        if functional_region._functional_region_replication(region) is not None:
            continue
        region_records[item.name] = (
            region,
            functional_region.functional_region_plan(
                region,
                item.name,
                reserved_names=tuple(entry.name for entry in materialized),
            ),
        )

    shared_region_materialized = _shared_functional_region_materialization(
        region_records,
        used_names=(set(names.allocated_names) | {item.name for item in materialized}),
    )
    shared_region_aliases = materialization.ExpressionAliasMap(
        (item.expression, item.name) for item in shared_region_materialized
    )

    def render(value: expr.Expression, *, keep: expr.Expression | None = None) -> str:
        physical = owner.physicalize(value)
        return sv_expression._expression(materialization.replace_materialized(physical, aliases, keep=keep))

    declarations = []
    assignments = []
    for item in materialized:
        signed = " signed" if isinstance(item.expression.type, (ir_types.SIntType, ir_types.FixedType)) else ""
        declarations.append(
            f"  logic{signed} {sv_rendering._range(sv_rendering._width(item.expression.type))}{item.name};"
        )
    for item in materialization.dependency_ordered_materialization(shared_region_materialized):
        signed = " signed" if isinstance(item.expression.type, (ir_types.SIntType, ir_types.FixedType)) else ""
        declarations.append(
            f"  logic{signed} {sv_rendering._range(sv_rendering._width(item.expression.type))}{item.name};"
        )
        rewritten = materialization.replace_materialized(
            item.expression,
            shared_region_aliases,
            keep=item.expression,
        )
        assignments.extend(
            _continuous_value_assignments(item.name, rewritten, indent="  ")
        )
    for item in materialized:
        if isinstance(item.expression, expr.FunctionalRegion):
            _append_materialized_functional_region(
                item,
                aliases=aliases,
                records=region_records,
                outer_materialized=shared_region_materialized,
                declarations=declarations,
                statements=assignments,
            )
        else:
            physical = owner.physicalize(item.expression)
            rewritten = materialization.replace_materialized(
                physical,
                aliases,
                keep=item.expression,
            )
            assignments.extend(
                _continuous_value_assignments(item.name, rewritten, indent="  ")
            )
    return declarations, assignments, render


def _embedded_staging_emission(
    module: ir_module.Module,
) -> tuple[list[str], list[str], object]:
    """Materialize staged and shared values without moving timing boundaries.

    Rule/sequential emission uses this renderer even when a module contains no
    ``Delay`` or ``Pipeline`` node.  Keep its pure combinational expressions on
    the same module-wide DAG materialization policy as ordinary combinational
    emission; otherwise every rule guard and register action recursively
    prints the complete logical expression tree at each use site.
    """

    staged: list[expr.Delay | expr.Pipeline] = []
    owner = module_materialization_owner(module)
    local_names = owner.names
    seen: set[tuple[type[expr.Expression], int]] = set()

    def collect(value: expr.Expression) -> None:
        for child in materialization.expression_children(value):
            collect(child)
        if isinstance(value, (expr.Delay, expr.Pipeline)):
            key = (type(value), value.instance)
            if key not in seen:
                seen.add(key)
                staged.append(value)

    for root in owner.roots:
        collect(root)

    if staged and not module.clock_domains:
        raise SystemVerilogEmissionError(
            "staged hierarchical component requires clock and reset"
        )

    aliases = materialization.ExpressionAliasMap()
    declarations: list[str] = []
    resets: dict[str, list[str]] = {}
    updates: dict[str, list[str]] = {}
    for value in staged:
        value_domain = (
            value.domain if isinstance(value, expr.Pipeline) else module.clock
        )
        if value_domain is None:
            raise SystemVerilogEmissionError(
                "delay in a multi-clock module requires domain-qualified "
                "lowering before direct emission"
            )
        domain_resets = resets.setdefault(value_domain, [])
        domain_updates = updates.setdefault(value_domain, [])
        count = value.cycles if isinstance(value, expr.Delay) else value.stages
        kind = "delay" if isinstance(value, expr.Delay) else "pipeline"
        signed = " signed" if isinstance(value.type, (ir_types.SIntType, ir_types.FixedType)) else ""
        for index in range(1, count + 1):
            name = local_names.stage(kind, value.instance, index)
            declarations.append(
                f"  logic{signed} {sv_rendering._range(sv_rendering._width(value.type))}{name};"
            )
            domain_resets.append(f"      {name} <= '0;")
        aliases[value] = local_names.stage(kind, value.instance, count)

    # Publish every backend-local alias before rendering either a temporary or
    # a staged input.  A shared expression may depend on a staged value, and a
    # staged input may itself contain shared pure subexpressions.  The existing
    # exact typed DAG plan is authoritative for both cases; Delay/Pipeline
    # nodes already have their timing-preserving stage aliases above and must
    # not acquire a second combinational temporary.
    planned_items = owner.materialization_plan
    materialized_items = tuple(
        item
        for item in planned_items
        if not isinstance(item.expression, (expr.Delay, expr.Pipeline))
        and item.expression not in aliases
    )
    for item in materialized_items:
        aliases[item.expression] = item.name

    for value in staged:
        value_domain = (
            value.domain if isinstance(value, expr.Pipeline) else module.clock
        )
        assert value_domain is not None
        domain_updates = updates[value_domain]
        count = value.cycles if isinstance(value, expr.Delay) else value.stages
        kind = "delay" if isinstance(value, expr.Delay) else "pipeline"
        physical_input = materialization.replace_materialized(value.expression, aliases)
        domain_updates.append(
            f"      {local_names.stage(kind, value.instance, 1)} <= "
            f"{sv_expression._expression(physical_input)};"
        )
        for index in range(2, count + 1):
            domain_updates.append(
                f"      {local_names.stage(kind, value.instance, index)} <= "
                f"{local_names.stage(kind, value.instance, index - 1)};"
            )

    sequential: list[str] = []
    for domain in module.clock_domains:
        if domain.clock not in updates:
            continue
        sequential.extend((
            f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier, domain.clock)}) begin",
            f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, domain.clock)}) begin",
            *resets[domain.clock],
            "    end else begin",
            *updates[domain.clock],
            "    end",
            "  end",
        ))

    # Rules and sequential modules historically used this staging renderer
    # directly instead of the ordinary combinational materialization path.
    # Compact functional regions retain statement-owned lowering; every other
    # selected pure DAG node uses an exact-width continuous assignment.
    combinational_logic: list[str] = []
    region_items = tuple(
        item
        for item in materialized_items
        if isinstance(item.expression, expr.FunctionalRegion)
    )
    ordinary_items = tuple(
        item
        for item in materialized_items
        if not isinstance(item.expression, expr.FunctionalRegion)
    )
    materialized_names = tuple(item.name for item in materialized_items)
    region_records: dict[
        str, tuple[expr.FunctionalRegion, functional.FunctionalRegionEmissionPlan]
    ] = {}
    for item in region_items:
        region = materialization.replace_materialized(
            item.expression,
            aliases,
            keep=item.expression,
            rewrite_region_owned=True,
        )
        assert isinstance(region, expr.FunctionalRegion)
        if functional_region._functional_region_replication(region) is not None:
            continue
        region_records[item.name] = (
            region,
            functional_region.functional_region_plan(
                region,
                item.name,
                reserved_names=materialized_names,
            ),
        )

    shared_region_materialized = _shared_functional_region_materialization(
        region_records,
        used_names=(
            set(local_names.allocated_names)
            | {item.name for item in materialized_items}
        ),
    )
    shared_region_aliases = materialization.ExpressionAliasMap(
        (item.expression, item.name) for item in shared_region_materialized
    )
    for item in materialized_items:
        signed = (
            " signed"
            if isinstance(item.expression.type, (ir_types.SIntType, ir_types.FixedType))
            else ""
        )
        declarations.append(
            f"  logic{signed} {sv_rendering._range(sv_rendering._width(item.expression.type))}{item.name};"
        )
    for item in materialization.dependency_ordered_materialization(ordinary_items):
        physical = owner.physicalize(item.expression)
        rewritten = materialization.replace_materialized(
            physical,
            aliases,
            keep=item.expression,
        )
        combinational_logic.extend(
            _continuous_value_assignments(item.name, rewritten, indent="  ")
        )
    for item in materialization.dependency_ordered_materialization(shared_region_materialized):
        signed = (
            " signed"
            if isinstance(item.expression.type, (ir_types.SIntType, ir_types.FixedType))
            else ""
        )
        declarations.append(
            f"  logic{signed} {sv_rendering._range(sv_rendering._width(item.expression.type))}{item.name};"
        )
        rewritten = materialization.replace_materialized(
            item.expression,
            shared_region_aliases,
            keep=item.expression,
        )
        combinational_logic.extend(
            _continuous_value_assignments(item.name, rewritten, indent="  ")
        )
    for item in region_items:
        _append_materialized_functional_region(
            item,
            aliases=aliases,
            records=region_records,
            outer_materialized=shared_region_materialized,
            declarations=declarations,
            statements=combinational_logic,
        )

    def render(value: expr.Expression) -> str:
        physical = owner.physicalize(value)
        return sv_expression._expression(materialization.replace_materialized(physical, aliases))

    return declarations, [*combinational_logic, *sequential], render
