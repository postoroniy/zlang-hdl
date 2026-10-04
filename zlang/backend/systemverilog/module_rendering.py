"""Typed function definitions and final SystemVerilog module assembly."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace

from zlang.ir import expressions as expr
from zlang.ir import callables as ir_callables
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.backend import expression_materialization as materialization
from zlang.backend import identifiers as identifiers
from zlang.backend.systemverilog import context as emission_context
from zlang.backend.systemverilog import boundary as sv_boundary
from zlang.backend.systemverilog import materialized as sv_materialized
from zlang.backend.systemverilog import functional as functional
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.ir import signed_reductions as signed_reductions
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering
from zlang.backend.systemverilog.functional import region as functional_region


_FUNCTIONAL_REGION_PLANNER = functional.FunctionalRegionPlanner(
    error=SystemVerilogEmissionError
)


def _named_module(
    name: str,
    ports: list[str],
    lines: list[str],
    *,
    typed_module: ir_module.Module | None = None,
) -> str:
    boundary = emission_context.current_top_boundary()
    type_prefix = ""
    if boundary is not None and name == boundary.physical_module_name:
        type_prefix = "\n".join(boundary.type_declarations)
        if any((boundary.type_declarations, boundary.base_aliases,
                boundary.direct_vector_bases)):
            ports = list(boundary.public_ports)
        bridge_prefix = (
            ("  // Compiler-generated inline top boundary.",)
            if boundary.declarations
            or boundary.input_bridges
            or boundary.output_bridges
            else ()
        )
        lines = [*bridge_prefix, *boundary.declarations,
                 *boundary.input_bridges, *lines, *boundary.output_bridges]
    declarations = ",\n".join(f"  {port}" for port in ports)
    helpers = _function_definitions(typed_module) if typed_module is not None else []
    conditioner = (
        sv_sequential.reset_conditioner_lines(typed_module, sv_rendering._identifier)
        if typed_module is not None
        else ()
    )
    body = "\n".join(
        line for line in (*helpers, *conditioner, *lines) if line
    )
    prefix = f"{type_prefix}\n" if type_prefix else ""
    return f"{prefix}module {name} (\n{declarations}\n);\n  // Generated from backend-independent typed ZLang IR.\n{body}\nendmodule\n"
def _typed_functions(module: ir_module.Module) -> tuple[object, ...]:
    """Return the local executable callable closure exactly once."""

    try:
        return ir_callables.reachable_module_callables(module)
    except ir_callables.CallableReachabilityError as error:
        raise SystemVerilogEmissionError(str(error)) from error


def _function_region_emission(
    body: expr.Expression,
    *,
    reserved_names: tuple[str, ...],
) -> tuple[expr.Expression, list[str], list[str]]:
    """Materialize every region nested in one function body.

    A FunctionalRegion is a statement-owned value even when it appears below
    a mux, call argument, or aggregate constructor.  Function emission cannot
    delegate those nodes to the scalar expression renderer, so give each one
    a stable local and emit its procedural producer before the final result.
    """

    regions = _FUNCTIONAL_REGION_PLANNER.regions(body)
    if not regions:
        return body, [], []
    used = set(reserved_names)
    aliases = materialization.ExpressionAliasMap()
    for region in regions:
        identity = signed_reductions.expression_semantic_identity(region)
        aliases[region] = identifiers.allocate_private_rtl_identifier(
            f"zlang_fn_region_{identity[:10]}",
            semantic_identity=(
                f"{materialization.FUNCTIONAL_REGION_EMISSION_SCHEMA}:function:{identity}"
            ),
            used=used,
        )
    declarations = [
        f"    logic{(' signed' if isinstance(region.type, (ir_types.SIntType, ir_types.FixedType)) else '')} "
        f"{sv_rendering._range(sv_rendering._width(region.type))}{aliases[region]};"
        for region in regions
    ]
    statements: list[str] = []
    for region in regions:
        rewritten = materialization.replace_materialized(
            region,
            aliases,
            keep=region,
            rewrite_region_owned=True,
        )
        assert isinstance(rewritten, expr.FunctionalRegion)
        result_name = aliases[region]
        replication = functional_region._functional_region_replication(rewritten)
        if replication is not None:
            statements.append(f"    {result_name} = {replication};")
            continue
        plan = functional_region.functional_region_plan(
            rewritten,
            result_name,
            reserved_names=tuple((*reserved_names, *aliases.values())),
        )
        owned_declarations, owned_statements = functional_region._functional_region_rendering(
            rewritten,
            plan,
            declaration_indent="    ",
            statement_indent="    ",
        )
        declarations.extend(owned_declarations)
        statements.extend(owned_statements)
    rewritten_body = materialization.replace_materialized(body, aliases)
    return rewritten_body, declarations, statements


def _function_definitions(module: ir_module.Module) -> list[str]:
    definitions: list[str] = []
    functions = _typed_functions(module)
    function_names = {sv_rendering._identifier(function.name) for function in functions}
    outer_names = {
        sv_rendering._identifier(item.name)
        for item in (*module.ports, *module.registers, *module.locals)
    }
    for function in functions:
        name = sv_rendering._identifier(function.name)
        return_signed = (
            " signed"
            if isinstance(function.return_type, (ir_types.SIntType, ir_types.FixedType))
            else ""
        )
        parameters = []
        # Preserve the source stem without repeating the enclosing helper.
        # A short private prefix excludes ALL bare SV keywords (including ones
        # not covered by the frozen public-port escaping policy).
        used = function_names | outer_names
        parameter_names = {}
        for parameter in sorted(function.parameters, key=lambda item: item.name):
            parameter_names[parameter.name] = identifiers.allocate_private_rtl_identifier(
                f"arg_{parameter.name}",
                semantic_identity=f"{function.callee_identity}:parameter:{parameter.name}",
                used=used,
            )
        for parameter in function.parameters:
            signed = (
                " signed"
                if isinstance(parameter.type, (ir_types.SIntType, ir_types.FixedType))
                else ""
            )
            parameters.append(
                f"input logic{signed} {sv_rendering._range(sv_rendering._width(parameter.type))}"
                f"{parameter_names[parameter.name]}"
            )
        declaration = ",\n    ".join(parameters)
        call_identity = getattr(function, "callee_identity", "")
        identity_note = (
            f"  // ZLang callable identity: {call_identity}\n"
            if call_identity
            else ""
        )
        renamed_body = _rename_parameter_refs(function.body, parameter_names)
        if isinstance(renamed_body, expr.FunctionalRegion):
            replication = functional_region._functional_region_replication(renamed_body)
            if replication is not None:
                definitions.append(
                    f"  function automatic logic{return_signed} "
                    f"{sv_rendering._range(sv_rendering._width(function.return_type))}{name}("
                    f"{declaration});\n"
                    f"{identity_note}"
                    f"    {name} = {replication};\n"
                    "  endfunction"
                )
                continue
            region_plan = functional_region.functional_region_plan(
                renamed_body,
                name,
                reserved_names=tuple((*parameter_names.values(), name, *used)),
            )
            region_declarations, region_statements = functional_region._functional_region_rendering(
                renamed_body,
                region_plan,
                declaration_indent="    ",
                statement_indent="    ",
            )
            region_block = "\n".join(
                (*region_declarations, *region_statements)
            )
            definitions.append(
                f"  function automatic logic{return_signed} "
                f"{sv_rendering._range(sv_rendering._width(function.return_type))}{name}("
                f"{declaration});\n"
                f"{identity_note}"
                f"{region_block}\n"
                "  endfunction"
            )
            continue
        renamed_body, region_declarations, region_statements = (
            _function_region_emission(
                renamed_body,
                reserved_names=tuple((*parameter_names.values(), name, *used)),
            )
        )
        materialized = materialization.plan_materialization(
            (renamed_body,),
            reserved_names=(*parameter_names.values(), name),
            generated_prefix="zlang_fn_expr_",
        )
        aliases = materialization.ExpressionAliasMap(
            (item.expression, item.name) for item in materialized
        )

        # Function statements execute procedurally, so exact typed dependencies
        # must be assigned before the expression that consumes them.  Global
        # first-discovery order is deterministic but is not topological when a
        # shared child was first seen through another root.  Module-level
        # materialization uses continuous assigns and does not need this step.
        function_locals: list[str] = []
        function_assignments: list[str] = []
        for item in materialization.dependency_ordered_materialization(materialized):
            signed = (
                " signed"
                if isinstance(item.expression.type, (ir_types.SIntType, ir_types.FixedType))
                else ""
            )
            function_locals.append(
                f"    logic{signed} {sv_rendering._range(sv_rendering._width(item.expression.type))}{item.name};"
            )
            rewritten = materialization.replace_materialized(
                item.expression,
                aliases,
                keep=item.expression,
            )
            function_assignments.append(
                f"    {item.name} = {sv_expression._expression(rewritten)};"
            )
        rewritten_body = materialization.replace_materialized(renamed_body, aliases)
        local_block = "\n".join(
            (
                *region_declarations,
                *function_locals,
                *region_statements,
                *function_assignments,
            )
        )
        if local_block:
            local_block += "\n"
        definitions.append(
            f"  function automatic logic{return_signed} "
            f"{sv_rendering._range(sv_rendering._width(function.return_type))}{name}("
            f"{declaration});\n"
            f"{identity_note}"
            f"{local_block}"
            f"    {name} = {sv_expression._expression(rewritten_body)};\n"
            "  endfunction"
        )
    return definitions


def _rename_parameter_refs(
    value: object,
    names: dict[str, str],
) -> object:
    """Give helper parameters deterministic backend-private identifiers.

    SystemVerilog functions live inside the generated module.  Reusing a
    source parameter such as ``value`` can therefore hide the top-level port
    with the same name and turns strict Verilator lint into a failure.  Rename
    only typed ``ParameterRef`` leaves; semantic identities and call-site ABI
    remain unchanged.
    """

    if isinstance(value, expr.ParameterRef):
        name = names.get(value.name)
        return replace(value, name=name) if name is not None else value
    if isinstance(value, tuple):
        return tuple(_rename_parameter_refs(item, names) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        updates = {
            item.name: _rename_parameter_refs(getattr(value, item.name), names)
            for item in fields(value)
            if item.init and item.name not in {"type", "origin"}
        }
        return replace(value, **updates) if updates else value
    return value


def _module(module: ir_module.Module, ports: list[str], lines: list[str]) -> str:
    boundary = emission_context.current_top_boundary()
    name = (
        boundary.physical_module_name
        if boundary is not None and boundary.module_name == module.name
        else identifiers.rtl_identifier(module.name)
    )
    return _named_module(name, ports, lines, typed_module=module)


def emit_combinational(module: ir_module.Module) -> str:
    """Emit one complete typed combinational module."""

    output_names = {port.name for port in module.outputs}
    assigned_names = {
        assignment.target.name
        for assignment in module.assignments
        if assignment.signal is None and assignment.channel is None
    }
    if (
        len(module.assignments) != len(module.outputs)
        or assigned_names != output_names
    ):
        raise SystemVerilogEmissionError(
            "direct combinational emission requires one complete scalar "
            "assignment per output"
        )
    ports = [
        *sv_boundary._clock_reset_port_declarations(module),
        *(sv_boundary._port_declaration(port) for port in module.inputs),
        *(sv_boundary._port_declaration(port) for port in module.outputs),
    ]
    declarations, materialized, render = sv_materialized._materialized_emission(module)
    assignments = [
        line
        for assignment in module.assignments
        for line in (
            f"  // ZLang IR output: {assignment.target.name}",
            *sv_materialized._output_value_assignments(
                assignment.target.name,
                assignment.expression,
                render,
                indent="  ",
            ),
        )
    ]
    return _module(
        module,
        ports,
        [
            *declarations,
            *materialized,
            *assignments,
        ],
    )
