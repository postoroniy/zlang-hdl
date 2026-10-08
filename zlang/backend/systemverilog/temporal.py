"""Capacity-one temporal ready/valid SystemVerilog lowering."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.ir.temporal_admission import TemporalAdmissionPolicy
from zlang.shared_arithmetic import SharedArithmeticError, match_shared_multiply_add

from . import boundary as sv_boundary
from . import expression as sv_expression
from . import module_rendering as sv_module_rendering
from . import rendering as sv_rendering
from . import sequential as sv_sequential
from .errors import SystemVerilogEmissionError


def _replace_temporal_input(value: object, *, source: str, capture: str) -> object:
    """Replace only one source RV payload with its captured transaction."""

    if (
        isinstance(value, expr.ReadyValidRef)
        and value.interface == source
        and value.signal is ir_interfaces.ReadyValidSignal.PAYLOAD
    ):
        return expr.InputRef(capture, value.type, origin=value.origin)
    if isinstance(value, expr.Expression):
        updates: dict[str, object] = {}
        for descriptor in fields(value):
            if not descriptor.init or descriptor.name in {"origin", "source_origin"}:
                continue
            original = getattr(value, descriptor.name)
            rewritten = _replace_temporal_input(
                original, source=source, capture=capture
            )
            if rewritten is not original:
                updates[descriptor.name] = rewritten
        return replace(value, **updates) if updates else value
    if isinstance(value, tuple):
        rewritten = tuple(
            _replace_temporal_input(item, source=source, capture=capture)
            for item in value
        )
        return (
            value
            if all(a is b for a, b in zip(value, rewritten, strict=True))
            else rewritten
        )
    if is_dataclass(value) and not isinstance(value, type):
        updates = {}
        for descriptor in fields(value):
            if (
                not descriptor.init
                or descriptor.name in {"origin", "source_origin", "type"}
            ):
                continue
            original = getattr(value, descriptor.name)
            rewritten = _replace_temporal_input(
                original, source=source, capture=capture
            )
            if rewritten is not original:
                updates[descriptor.name] = rewritten
        return replace(value, **updates) if updates else value
    return value


def emit_temporal_elastic_pipeline(
    module: ir_module.Module,
    region: object,
    source: ir_module.Port,
    destination: ir_module.Port,
) -> str:
    """Render the frozen non-interleaved shared multiply/add schedule."""

    graph = getattr(region, "temporal_graph")
    assert graph is not None
    try:
        shape = match_shared_multiply_add(region.source_expression)
    except SharedArithmeticError as error:
        raise SystemVerilogEmissionError(str(error)) from error
    if tuple(item.operation_id for item in graph.operations) != (
        "mul0",
        "mul1",
        "add0",
    ):
        raise SystemVerilogEmissionError(
            "unsupported temporal shared-arithmetic schedule"
        )
    if graph.admission_policy is not TemporalAdmissionPolicy.RETIRE_AND_RELOAD:
        raise SystemVerilogEmissionError("unsupported temporal admission policy")

    prefix = "zlang_temporal_" + region.semantic_id.removeprefix("elastic:")[:16]
    state = f"{prefix}_state"
    storage_names = {
        item.register_id: f"{prefix}_{item.register_id}"
        for item in graph.storage_plan.registers
    }

    def stored(value_id: str) -> str:
        return storage_names[graph.storage_plan.register_for(value_id).register_id]

    capture = stored("input")
    product0 = stored("mul0")
    product1 = stored("mul1")
    result = stored("result")
    source_name = sv_rendering._identifier(source.name)
    destination_name = sv_rendering._identifier(destination.name)

    def render(value: expr.Expression) -> str:
        rewritten = _replace_temporal_input(
            value, source=source.name, capture=capture
        )
        assert isinstance(rewritten, expr.Expression)
        return sv_expression._expression(rewritten)

    state_width = 3
    state_idle = f"({state} == {state_width}'d0)"
    state_mul0 = f"({state} == {state_width}'d1)"
    state_mul1 = f"({state} == {state_width}'d2)"
    state_add = f"({state} == {state_width}'d3)"
    state_output = f"({state} == {state_width}'d4)"

    def storage_declaration(item: object) -> str:
        type_ = getattr(item, "type")
        signed = (
            " signed"
            if isinstance(type_, (ir_types.SIntType, ir_types.FixedType))
            else ""
        )
        return (
            f"  logic{signed} {sv_rendering._range(sv_rendering._width(type_))}"
            f"{storage_names[getattr(item, 'register_id')]};"
        )

    lines = [
        f"  logic [{state_width - 1}:0] {state};",
        *(storage_declaration(item) for item in graph.storage_plan.registers),
        f"  assign {source_name}_ready = "
        f"{sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
        f"({state_idle} || ({state_output} && {destination_name}_ready));",
        f"  assign {destination_name}_payload = {result};",
        f"  assign {destination_name}_valid = "
        f"{sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
        f"{state_output};",
        *(
            f"  assign {sv_rendering._identifier(assignment.target.name)} = "
            f"{sv_expression._expression(assignment.expression)};"
            for assignment in module.assignments
        ),
        f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier)}) begin",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier)}) begin",
        f"      {state} <= {state_width}'d0;",
        *(
            f"      {storage_names[item.register_id]} <= '0;"
            for item in graph.storage_plan.registers
        ),
        "    end else begin",
        f"      if ({state_idle}) begin",
        f"        if ({source_name}_valid && {source_name}_ready) begin",
        f"          {capture} <= {source_name}_payload;",
        f"          {state} <= {state_width}'d1;",
        "        end",
        f"      end else if ({state_mul0}) begin",
        f"        {product0} <= {render(shape.product0)};",
        f"        {state} <= {state_width}'d2;",
        f"      end else if ({state_mul1}) begin",
        f"        {product1} <= {render(shape.product1)};",
        f"        {state} <= {state_width}'d3;",
        f"      end else if ({state_add}) begin",
        f"        {result} <= {sv_expression._expression(expr.Add(expr.InputRef(product0, shape.product0.type), expr.InputRef(product1, shape.product1.type), destination.type))};",
        f"        {state} <= {state_width}'d4;",
        "      end else begin",
        f"        if ({destination_name}_ready) begin",
        f"          if ({source_name}_valid) begin",
        f"            {capture} <= {source_name}_payload;",
        f"            {state} <= {state_width}'d1;",
        "          end else begin",
        f"            {state} <= {state_width}'d0;",
        "          end",
        "        end",
        "      end",
        "    end",
        "  end",
    ]
    return sv_module_rendering._module(
        module, sv_boundary._physical_port_declarations(module), lines
    )


__all__ = ["emit_temporal_elastic_pipeline"]
