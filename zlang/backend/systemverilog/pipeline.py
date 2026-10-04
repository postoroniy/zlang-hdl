"""Fixed and elastic pipeline SystemVerilog emission."""

from __future__ import annotations

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.backend import expression_materialization as materialization
from zlang.backend.systemverilog import boundary as sv_boundary
from zlang.backend.systemverilog import materialized as sv_materialized
from zlang.backend.systemverilog import module_rendering as sv_module_rendering
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering


def _emit_pipeline(module: ir_module.Module) -> str:
    assigned_names = {
        assignment.target.name
        for assignment in module.assignments
        if assignment.signal is None and assignment.channel is None
    }
    if module.clock is None and len(module.clock_domains) > 1:
        if (
            len(module.assignments) != len(module.outputs)
            or assigned_names != {port.name for port in module.outputs}
        ):
            raise SystemVerilogEmissionError(
                "multi-clock pipeline emission requires every output to have "
                "one assignment"
            )
        declarations, sequential, render = (
            sv_materialized._embedded_staging_emission(module)
        )
        if not sequential:
            raise SystemVerilogEmissionError(
                "multi-clock sequential datapath requires a domain-qualified "
                "pipeline"
            )
        assignments = [
            f"  assign {sv_rendering._identifier(assignment.target.name)} = "
            f"{render(assignment.expression)};"
            for assignment in module.assignments
        ]
        return sv_module_rendering._module(
            module,
            [
                *sv_boundary._clock_reset_port_declarations(module),
                *(sv_boundary._port_declaration(port) for port in module.inputs),
                *(sv_boundary._port_declaration(port) for port in module.outputs),
            ],
            [*declarations, *sequential, *assignments],
        )
    if (
        module.clock is None
        or module.reset is None
        or len(module.assignments) != len(module.outputs)
        or assigned_names != {port.name for port in module.outputs}
    ):
        raise SystemVerilogEmissionError(
            "direct pipeline emission requires every output to have one assignment"
        )
    staged_assignments: list[ir_module.Assignment] = []
    for candidate in module.assignments:
        candidate_staging: dict[int, expr.Delay | expr.Pipeline] = {}
        _collect_staged_expressions(
            sv_materialized._instance_expression(module, candidate.expression),
            candidate_staging,
        )
        if candidate_staging:
            staged_assignments.append(candidate)
    if len(staged_assignments) != 1:
        raise SystemVerilogEmissionError(
            "direct pipeline emission requires exactly one staged output assignment"
        )
    assignment = staged_assignments[0]
    root = sv_materialized._instance_expression(module, assignment.expression)
    staged: dict[int, expr.Delay | expr.Pipeline] = {}
    _collect_staged_expressions(root, staged)
    if not staged:
        raise SystemVerilogEmissionError(
            "direct sequential datapath emission requires a typed pipeline or delay"
        )
    aliases = materialization.ExpressionAliasMap(
        (node, _staged_signal_name(node, _staged_count(node)))
        for node in staged.values()
    )
    physical_roots = tuple(
        _replace_staged_expressions(node.expression, aliases)
        for node in staged.values()
    )
    stage_materialized = materialization.plan_materialization(
        physical_roots,
        reserved_names=(
            *(sv_rendering._identifier(port.name) for port in module.ports),
            *aliases.values(),
        ),
        generated_prefix="zlang_stage_expr_",
        # Pipeline-stage roots are already explicit DAG fragments.  A shared
        # multiply has only three expression nodes but must still remain one
        # physical combinational value rather than being copied into siblings.
        minimum_shared_size=2,
    )
    materialized_aliases = materialization.ExpressionAliasMap(
        (item.expression, item.name) for item in stage_materialized
    )

    def render(value: expr.Expression) -> str:
        physical = _replace_staged_expressions(value, aliases)
        return sv_expression._expression(materialization.replace_materialized(physical, materialized_aliases))

    registers: list[str] = []
    resets: list[str] = []
    updates: list[str] = []
    for node in staged.values():
        stages = _staged_count(node)
        if stages <= 0:
            raise SystemVerilogEmissionError("pipeline/delay depth must be positive")
        signed = " signed" if isinstance(node.type, (ir_types.SIntType, ir_types.FixedType)) else ""
        for index in range(1, stages + 1):
            name = _staged_signal_name(node, index)
            registers.append(
                f"  logic{signed} {sv_rendering._range(sv_rendering._width(node.type))}{name};"
            )
            resets.append(f"      {name} <= '0;")
        updates.append(
            f"      {_staged_signal_name(node, 1)} <= {render(node.expression)};"
        )
        updates.extend(
            f"      {_staged_signal_name(node, index)} <= "
            f"{_staged_signal_name(node, index - 1)};"
            for index in range(2, stages + 1)
        )
    ports = [
        "input wire logic " + module.clock,
        "input wire logic " + module.reset,
        *(sv_boundary._port_declaration(port) for port in module.inputs),
        *(sv_boundary._port_declaration(port) for port in module.outputs),
    ]
    materialized_declarations = [
        "  logic"
        + (" signed" if isinstance(item.expression.type, (ir_types.SIntType, ir_types.FixedType)) else "")
        + f" {sv_rendering._range(sv_rendering._width(item.expression.type))}{item.name};"
        for item in stage_materialized
    ]
    materialized_assignments = [
        f"  assign {item.name} = "
        f"{sv_expression._expression(materialization.replace_materialized(item.expression, materialized_aliases, keep=item.expression))};"
        for item in materialization.dependency_ordered_materialization(stage_materialized)
    ]
    lines = [
        *materialized_declarations,
        *materialized_assignments,
        *registers,
        f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier)}) begin",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier)}) begin",
        *resets,
        "    end else begin",
        *updates,
        "    end",
        "  end",
        f"  assign {sv_rendering._identifier(assignment.target.name)} = {render(root)};",
        *(
            f"  assign {sv_rendering._identifier(item.target.name)} = "
            f"{render(sv_materialized._instance_expression(module, item.expression))};"
            for item in module.assignments
            if item is not assignment
        ),
    ]
    return sv_module_rendering._module(module, ports, lines)


def _emit_elastic_pipeline(module: ir_module.Module) -> str:
    """Emit the frozen single-region global-clock-enable elastic kernel."""

    if (
        module.clock is None
        or module.reset is None
        or len(module.elastic_pipeline_regions) != 1
    ):
        raise SystemVerilogEmissionError(
            "elastic pipeline emission requires one region and clock/reset"
        )
    region = module.elastic_pipeline_regions[0]
    source = next(port for port in module.ports if port.name == region.source_endpoint)
    destination = next(
        port for port in module.ports if port.name == region.destination_endpoint
    )
    if (
        source.direction is not ir_module.PortDirection.INPUT
        or destination.direction is not ir_module.PortDirection.OUTPUT
        or source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
        or destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
    ):
        raise SystemVerilogEmissionError(
            "elastic pipeline endpoints do not match their typed ready/valid ABI"
        )
    # Production elastic modules have no ordinary assignments.  The formal
    # artifact instrumentation may add explicit output-only observation
    # projections, and the specialized emitter must preserve those instead of
    # silently dropping them.  Keep this narrowly fail-closed so this does not
    # become a second compositional assignment engine.
    if any(
        assignment.target.direction is not ir_module.PortDirection.OUTPUT
        or assignment.target.protocol is not ir_interfaces.InterfaceProtocol.WIRE
        or assignment.signal is not None
        or assignment.channel is not None
        for assignment in module.assignments
    ):
        raise SystemVerilogEmissionError(
            "elastic pipeline accepts only formal output observation projections"
        )
    root = region.selected_candidate.expression
    staged: dict[int, expr.Delay | expr.Pipeline] = {}
    _collect_staged_expressions(root, staged)
    if any(isinstance(node, expr.Delay) for node in staged.values()):
        raise SystemVerilogEmissionError(
            "elastic pipeline does not accept fixed Delay nodes"
        )
    physical = tuple(sorted((_stage.instance, _stage.stages) for _stage in staged.values()))
    if physical != region.plan.data_stage_instances:
        raise SystemVerilogEmissionError(
            "elastic pipeline typed data stages do not match the frozen plan"
        )
    aliases = {
        node: _staged_signal_name(node, _staged_count(node))
        for node in staged.values()
    }

    def render(value: expr.Expression) -> str:
        return sv_expression._expression(_replace_staged_expressions(value, aliases))

    registers: list[str] = []
    resets: list[str] = []
    updates: list[str] = []
    for node in staged.values():
        signed = " signed" if isinstance(node.type, (ir_types.SIntType, ir_types.FixedType)) else ""
        for index in range(1, node.stages + 1):
            name = _staged_signal_name(node, index)
            registers.append(f"  logic{signed} {sv_rendering._range(sv_rendering._width(node.type))}{name};")
            resets.append(f"      {name} <= '0;")
        updates.append(
            f"      {_staged_signal_name(node, 1)} <= {render(node.expression)};"
        )
        updates.extend(
            f"      {_staged_signal_name(node, index)} <= "
            f"{_staged_signal_name(node, index - 1)};"
            for index in range(2, node.stages + 1)
        )

    latency = region.timing.minimum_unstalled_latency
    valid_names = [f"zlang_elastic_valid_{index}" for index in range(latency)]
    source_name = sv_rendering._identifier(source.name)
    destination_name = sv_rendering._identifier(destination.name)
    advance = "zlang_elastic_advance"
    lines = [
        *registers,
        *(f"  logic {name};" for name in valid_names),
        f"  logic {advance};",
        f"  assign {advance} = {sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
        f"(!{valid_names[-1]} || {destination_name}_ready);",
        f"  assign {source_name}_ready = {advance};",
        f"  assign {destination_name}_payload = {render(root)};",
        f"  assign {destination_name}_valid = "
        f"{sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && {valid_names[-1]};",
        *(
            f"  assign {sv_rendering._identifier(assignment.target.name)} = "
            f"{sv_expression._expression(assignment.expression)};"
            for assignment in module.assignments
        ),
        f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier)}) begin",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier)}) begin",
        *resets,
        *(f"      {name} <= 1'b0;" for name in valid_names),
        f"    end else if ({advance}) begin",
        *updates,
        f"      {valid_names[0]} <= {source_name}_valid;",
        *(
            f"      {valid_names[index]} <= {valid_names[index - 1]};"
            for index in range(1, latency)
        ),
        "    end",
        "  end",
    ]
    return sv_module_rendering._module(
        module, sv_boundary._physical_port_declarations(module), lines
    )


def _staged_count(value: expr.Delay | expr.Pipeline) -> int:
    return value.cycles if isinstance(value, expr.Delay) else value.stages


def _staged_signal_name(value: expr.Delay | expr.Pipeline, stage: int) -> str:
    prefix = "delay" if isinstance(value, expr.Delay) else "pipeline"
    # Trivial fixed pipelines retain one enclosing N-cycle node for the public
    # IR shape.  Expose each physical register with the historical per-stage
    # instance spelling so hierarchy-local naming and emitted RTL remain
    # compatible (pipeline_0_s1, pipeline_1_s1, ...).
    if (
        isinstance(value, expr.Pipeline)
        and value.pipeline_plan is None
        and value.stages > 1
    ):
        return f"{prefix}_{value.instance + stage - 1}_s1"
    return f"{prefix}_{value.instance}_s{stage}"


def _collect_staged_expressions(
    value: expr.Expression,
    found: dict[int, expr.Delay | expr.Pipeline],
) -> None:
    for child in materialization.expression_children(value):
        _collect_staged_expressions(child, found)
    if isinstance(value, (expr.Delay, expr.Pipeline)):
        previous = found.get(value.instance)
        if previous is not None and previous != value:
            raise SystemVerilogEmissionError(
                f"pipeline instance {value.instance} has conflicting typed definitions"
            )
        found.setdefault(value.instance, value)


def _replace_staged_expressions(
    value: expr.Expression,
    aliases: materialization.ExpressionAliasMap,
) -> expr.Expression:
    return materialization.replace_materialized(value, aliases)
