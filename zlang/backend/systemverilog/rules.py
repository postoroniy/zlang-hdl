"""Rule ordering and sequential rule-state SystemVerilog emission."""

from __future__ import annotations

from typing import Callable

from zlang.ir import expressions as expr
from zlang.ir import module as ir_module
from zlang.backend import identifiers as identifiers
from zlang.backend.systemverilog import boundary as sv_boundary
from zlang.backend.systemverilog import materialized as sv_materialized
from zlang.backend.systemverilog import module_rendering as sv_module_rendering
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import rendering as sv_rendering


def _append_rule_state(
    module: ir_module.Module,
    declarations: list[str],
    logic: list[str],
    render: Callable[[expr.Expression], str],
    *,
    compact_reset: bool = False,
) -> None:
    """Append classifier-approved scalar register/rule state to a module body."""

    if module.registers and not module.clock_domains:
        raise SystemVerilogEmissionError("direct rule emission requires clock/reset")
    ordered_rules = _ordered_rules(module)
    declarations.extend(
        sv_rendering._logic_declaration(
            identifiers.rtl_register_state_identifier(register.name), register.type
        )
        for register in module.registers
        if not any(
            port.name == register.name and port.registered
            for port in module.outputs
        )
    )
    for register in module.registers:
        if register.domain is None:
            raise SystemVerilogEmissionError(
                f"register '{register.name}' has no resolved clock domain"
            )
        writers = tuple(
            (rule, action)
            for rule in ordered_rules
            for action in rule.actions
            if action.target.name == register.name and rule.domain == register.domain
        )
        default = next(
            (
                assignment.expression
                for assignment in module.next_assignments
                if assignment.target.name == register.name
            ),
            None,
        )
        if register.initial is None and not writers and default is None:
            continue
        if register.initial is None:
            logic.append(
                f"  always_ff @({sv_sequential.active_clock_event(module, sv_rendering._identifier, register.domain)}) begin"
            )
            body_indent = "    "
        else:
            logic.append(
                f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier, register.domain)}) begin"
            )
            if compact_reset:
                logic.extend((
                    f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, register.domain)}) "
                    f"{sv_rendering._identifier(register.name)} <= {render(register.initial)};",
                    "    else begin",
                ))
            else:
                logic.extend((
                    f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, register.domain)}) begin",
                    f"      {sv_rendering._identifier(register.name)} <= {render(register.initial)};",
                    "    end else begin",
                ))
            body_indent = "      "
        for index, (rule, action) in enumerate(writers):
            keyword = "if" if index == 0 else "else if"
            update = sv_rendering._register_update_statement(
                register.name, action.expression, render
            )
            logic.append(
                f"{body_indent}{keyword} ({render(rule.guard)}) "
                f"{update}"
            )
        if default is not None:
            prefix = f"{body_indent}else " if writers else body_indent
            logic.append(
                f"{prefix}{sv_rendering._identifier(register.name)} <= {render(default)};"
            )
        elif writers:
            logic.append(
                f"{body_indent}else {sv_rendering._identifier(register.name)} <= "
                f"{sv_rendering._identifier(register.name)};"
            )
        if register.initial is not None:
            logic.append("    end")
        logic.append("  end")
    logic.extend(
        f"  assign {sv_rendering._assignment_name(assignment)} = "
        f"{render(assignment.expression)};"
        for assignment in module.assignments
        if not (
            isinstance(assignment.target, ir_module.Port)
            and assignment.target.registered
            and isinstance(assignment.expression, expr.RegisterRef)
            and assignment.expression.name == assignment.target.name
        )
    )


def _emit_rules(module: ir_module.Module) -> str:
    if not module.clock_domains:
        raise SystemVerilogEmissionError("direct rule emission requires clock/reset")
    ports = [
        *(
            item
            for domain in module.clock_domains
            for item in (
                f"input logic {sv_rendering._identifier(domain.clock)}",
                f"input logic {sv_rendering._identifier(domain.reset)}",
            )
        ),
        *(sv_boundary._port_declaration(port) for port in module.inputs),
        *(sv_boundary._port_declaration(port) for port in module.outputs),
    ]
    declarations, logic, render = sv_materialized._embedded_staging_emission(module)
    _append_rule_state(module, declarations, logic, render)
    return sv_module_rendering._module(module, ports, [*declarations, *logic])
def _ordered_rules(module: ir_module.Module) -> tuple[ir_module.Rule, ...]:
    remaining = list(module.rules)
    edges = {(priority.higher, priority.lower) for priority in module.rule_priorities}
    ordered: list[ir_module.Rule] = []
    while remaining:
        ready = next(
            (
                rule for rule in remaining
                if not any(
                    lower == rule.name
                    and any(item.name == higher for item in remaining)
                    for higher, lower in edges
                )
            ),
            None,
        )
        if ready is None:
            raise SystemVerilogEmissionError("rule priority graph is cyclic")
        ordered.append(ready)
        remaining.remove(ready)
    return tuple(ordered)
