"""Request/response endpoint SystemVerilog emission."""

from __future__ import annotations

from typing import Callable

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.backend.systemverilog import boundary as sv_boundary
from zlang.backend.systemverilog import materialized as sv_materialized
from zlang.backend.systemverilog import module_rendering as sv_module_rendering
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog import state as sv_state
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering
from zlang.backend import identifiers
from zlang.backend import naming


def tracker_name(
    descriptor: ir_module.RequestResponseConnection,
    module: ir_module.Module,
    *,
    identifier: Callable[[str], str] = identifiers.rtl_identifier,
) -> str:
    """Return the shared physical ledger token for one typed RR connection."""

    instance_identifier = naming.module_rtl_names(module).instance
    request_owner = instance_identifier(descriptor.request.source.owner)
    response_owner = instance_identifier(descriptor.response.destination.owner)
    return identifier(
        f"rr_{request_owner}_"
        f"{identifier(descriptor.request.source.name)}_"
        f"{response_owner}_outstanding"
    )


def _emit_request_response(module: ir_module.Module) -> str:
    if (
        module.clock is None
        or module.reset is None
        or len(module.request_responses) != 1
    ):
        raise SystemVerilogEmissionError(
            "direct request/response emission requires one clocked interface"
        )
    interface = module.request_responses[0]
    if interface.ordering is ir_interfaces.RequestResponseOrdering.IN_ORDER:
        return _emit_in_order_request_response(module, interface)
    if module.registers or module.next_assignments or module.rules:
        raise SystemVerilogEmissionError(
            "standalone out-of-order request/response with ordinary user state "
            "is not implemented by the direct-SystemVerilog endpoint emitter",
            semantic_path=(module.name, interface.name),
            code="ZL-BACKEND-SYSTEMVERILOG-REQUEST-RESPONSE-STATE",
            notes=(
                "emission stopped before publishing an artifact so the typed "
                "state transition cannot be omitted",
            ),
        )
    if interface.role is ir_interfaces.RequestResponseRole.RESPONDER:
        raise SystemVerilogEmissionError(
            "standalone out-of-order responder emission is not implemented"
        )
    if interface.match_by is None or interface.id_type is None:
        raise SystemVerilogEmissionError("out-of-order interface lacks an ID field")
    request_payload = _find_channel_assignment(
        module, interface.name, ir_interfaces.RequestResponseChannel.REQUEST,
        ir_interfaces.ReadyValidSignal.PAYLOAD,
    )
    request_valid = _find_channel_assignment(
        module, interface.name, ir_interfaces.RequestResponseChannel.REQUEST,
        ir_interfaces.ReadyValidSignal.VALID,
    )
    response_ready = _find_channel_assignment(
        module, interface.name, ir_interfaces.RequestResponseChannel.RESPONSE,
        ir_interfaces.ReadyValidSignal.READY,
    )
    response_output = next(
        assignment for assignment in module.assignments
        if isinstance(assignment.target, ir_module.Port)
        and isinstance(assignment.expression, expr.RequestResponseRef)
    )
    name = sv_rendering._identifier(interface.name)
    request_payload_signal = f"{name}_request_payload"
    request_valid_signal = f"{name}_request_valid"
    request_ready_signal = f"{name}_request_ready"
    response_payload_signal = f"{name}_response_payload"
    response_valid_signal = f"{name}_response_valid"
    response_ready_signal = f"{name}_response_ready"
    count_width = max(1, interface.max_outstanding.bit_length())
    id_width = sv_rendering._width(interface.id_type)
    request_payload_value = sv_expression._expression(request_payload.expression)
    request_id = sv_rendering._struct_field_expression(
        request_payload_value, interface.request_type, interface.match_by
    )
    response_id = sv_rendering._struct_field_expression(
        response_payload_signal, interface.response_type, interface.match_by
    )
    ports = [
        f"input logic {module.clock}",
        f"input logic {module.reset}",
        *(sv_boundary._port_declaration(port) for port in module.inputs),
        f"input logic {name}_request_ready",
        sv_boundary._logic_port(
            "input", f"{name}_response_payload", interface.response_type
        ),
        f"input logic {name}_response_valid",
        *(sv_boundary._port_declaration(port) for port in module.outputs),
        sv_boundary._logic_port(
            "output", f"{name}_request_payload", interface.request_type
        ),
        f"output logic {name}_request_valid",
        f"output logic {name}_response_ready",
    ]
    max_count = interface.max_outstanding
    lines = [
        f"  logic [{count_width - 1}:0] {name}_outstanding;",
        f"  logic [{id_width - 1}:0] {name}_ids [0:{max_count - 1}];",
        f"  logic [{id_width - 1}:0] {name}_ids_next [0:{max_count - 1}];",
        f"  logic [{max_count - 1}:0] {name}_ids_valid, {name}_ids_valid_next;",
        f"  logic {name}_duplicate, {name}_missing;",
        f"  logic {name}_request_id_present, {name}_response_id_present;",
        f"  logic {name}_request_transfer, {name}_response_transfer;",
        "  integer zlang_scan;",
        "  integer zlang_next;",
        "  integer zlang_state;",
        "  logic zlang_inserted;",
        f"  assign {request_payload_signal} = {request_payload_value};",
        f"  assign {sv_rendering._identifier(response_output.target.name)} = "
        f"{response_payload_signal};",
        "  always_comb begin",
        f"    {name}_request_id_present = 1'b0;",
        f"    {name}_response_id_present = 1'b0;",
        f"    for (zlang_scan = 0; zlang_scan < {max_count}; zlang_scan = zlang_scan + 1) begin",
        f"      if ({name}_ids_valid[zlang_scan] && {name}_ids[zlang_scan] == {request_id}) "
        f"{name}_request_id_present = 1'b1;",
        f"      if ({name}_ids_valid[zlang_scan] && {name}_ids[zlang_scan] == {response_id}) "
        f"{name}_response_id_present = 1'b1;",
        "    end",
        f"    {name}_duplicate = {sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
        f"{sv_expression._expression(request_valid.expression)} && {request_ready_signal} && "
        f"({name}_outstanding < {count_width}'d{max_count}) && "
        f"{name}_request_id_present;",
        f"    {name}_missing = {sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
        f"{sv_expression._expression(response_ready.expression)} && {response_valid_signal} && "
        f"({name}_outstanding != '0) && !{name}_response_id_present;",
        "  end",
        f"  assign {request_valid_signal} = {sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
        f"({name}_outstanding < {count_width}'d{max_count}) && !{name}_duplicate "
        f"&& {sv_expression._expression(request_valid.expression)};",
        f"  assign {response_ready_signal} = {sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
        f"({name}_outstanding != '0) && !{name}_missing "
        f"&& {sv_expression._expression(response_ready.expression)};",
        f"  assign {name}_request_transfer = {request_valid_signal} && "
        f"{request_ready_signal};",
        f"  assign {name}_response_transfer = {response_valid_signal} && "
        f"{response_ready_signal};",
        "  always_comb begin",
        f"    {name}_ids_valid_next = {name}_ids_valid;",
        f"    for (zlang_next = 0; zlang_next < {max_count}; zlang_next = zlang_next + 1) "
        f"{name}_ids_next[zlang_next] = {name}_ids[zlang_next];",
        f"    if ({name}_response_transfer) begin",
        f"      for (zlang_next = 0; zlang_next < {max_count}; zlang_next = zlang_next + 1) begin",
        f"        if ({name}_ids_valid_next[zlang_next] && "
        f"{name}_ids_next[zlang_next] == {response_id}) "
        f"{name}_ids_valid_next[zlang_next] = 1'b0;",
        "      end",
        "    end",
        "    zlang_inserted = 1'b0;",
        f"    if ({name}_request_transfer) begin",
        f"      for (zlang_next = 0; zlang_next < {max_count}; zlang_next = zlang_next + 1) begin",
        f"        if (!zlang_inserted && !{name}_ids_valid_next[zlang_next]) begin",
        f"          {name}_ids_next[zlang_next] = {request_id};",
        f"          {name}_ids_valid_next[zlang_next] = 1'b1;",
        "          zlang_inserted = 1'b1;",
        "        end",
        "      end",
        "    end",
        "  end",
        f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier)}) begin",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier)}) begin",
        f"      {name}_outstanding <= '0;",
        f"      {name}_ids_valid <= '0;",
        f"      for (zlang_state = 0; zlang_state < {max_count}; zlang_state = zlang_state + 1) "
        f"{name}_ids[zlang_state] <= '0;",
        "    end else begin",
        f"      case ({{{name}_request_transfer, {name}_response_transfer}})",
        f"        2'b10: if ({name}_outstanding < {count_width}'d{max_count}) "
        f"{name}_outstanding <= {name}_outstanding + 1'b1;",
        f"        2'b01: if ({name}_outstanding != '0) "
        f"{name}_outstanding <= {name}_outstanding - 1'b1;",
        f"        default: {name}_outstanding <= {name}_outstanding;",
        "      endcase",
        f"      {name}_ids_valid <= {name}_ids_valid_next;",
        f"      for (zlang_state = 0; zlang_state < {max_count}; zlang_state = zlang_state + 1) "
        f"{name}_ids[zlang_state] <= {name}_ids_next[zlang_state];",
        "    end",
        "  end",
    ]
    return sv_module_rendering._module(module, ports, lines)


def _emit_in_order_request_response(
    module: ir_module.Module,
    interface: object,
) -> str:
    """Emit one role-qualified, in-order standalone endpoint.

    The endpoint's role is already frozen in semantic IR.  This emitter owns
    only the local unbuffered transaction ledger: a physical request transfer
    increments it and a physical response transfer decrements it.  A
    same-cycle response is legal when the request also transfers in that cycle,
    matching the existing hierarchical in-order accounting semantics.
    """

    if any(port.protocol is not ir_interfaces.InterfaceProtocol.WIRE for port in module.ports):
        raise SystemVerilogEmissionError(
            "mixing request/response with other protocols is not implemented"
        )

    name = sv_rendering._identifier(interface.name)
    request_payload_signal = f"{name}_request_payload"
    request_valid_signal = f"{name}_request_valid"
    request_ready_signal = f"{name}_request_ready"
    response_payload_signal = f"{name}_response_payload"
    response_valid_signal = f"{name}_response_valid"
    response_ready_signal = f"{name}_response_ready"
    requester = interface.role is ir_interfaces.RequestResponseRole.REQUESTER
    count_width = max(1, interface.max_outstanding.bit_length())
    maximum = interface.max_outstanding
    has_user_transition = bool(
        module.registers or module.next_assignments or module.rules
    )
    if has_user_transition:
        if module.resolved_transition is None:
            raise SystemVerilogEmissionError(
                "stateful request/response emission requires resolved transition IR"
            )
        stage_declarations, stage_logic, stage_render = (
            sv_materialized._embedded_staging_emission(module)
        )
        if stage_declarations:
            declarations = list(stage_declarations)
            materialized = list(stage_logic)
            render = stage_render
        else:
            materialized_declarations, materialized_assignments, render = (
                sv_materialized._materialized_emission(module)
            )
            declarations = list(materialized_declarations)
            materialized = list(materialized_assignments)
    else:
        materialized_declarations, materialized_assignments, render = (
            sv_materialized._materialized_emission(module)
        )
        declarations = list(materialized_declarations)
        materialized = list(materialized_assignments)

    rr_declarations = [
        f"  logic [{count_width - 1}:0] {name}_outstanding;",
        f"  logic {name}_request_transfer, {name}_response_transfer;",
    ]
    rr_logic: list[str] = []
    owned_assignment_names: set[str] = set()

    def owned(
        channel: ir_interfaces.RequestResponseChannel,
        signal: ir_interfaces.ReadyValidSignal,
    ) -> str:
        assignment = _find_channel_assignment(
            module, interface.name, channel, signal
        )
        owned_assignment_names.add(sv_rendering._assignment_name(assignment))
        return render(assignment.expression)

    if requester:
        request_payload = owned(
            ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.PAYLOAD
        )
        request_valid = owned(
            ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.VALID
        )
        response_ready = owned(
            ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.READY
        )
        rr_logic.extend((
            f"  assign {request_payload_signal} = {request_payload};",
            f"  assign {request_valid_signal} = "
            f"{sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
            f"({name}_outstanding < {count_width}'d{maximum}) && "
            f"({request_valid});",
            f"  assign {name}_request_transfer = "
            f"{request_valid_signal} && {request_ready_signal};",
            f"  assign {response_ready_signal} = "
            f"{sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
            f"({name}_outstanding != '0 || {name}_request_transfer) && "
            f"({response_ready});",
            f"  assign {name}_response_transfer = "
            f"{response_valid_signal} && {response_ready_signal};",
        ))
    else:
        request_ready = owned(
            ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.READY
        )
        response_payload = owned(
            ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.PAYLOAD
        )
        response_valid = owned(
            ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.VALID
        )
        rr_logic.extend((
            f"  assign {request_ready_signal} = "
            f"{sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
            f"({name}_outstanding < {count_width}'d{maximum}) && "
            f"({request_ready});",
            f"  assign {name}_request_transfer = "
            f"{request_valid_signal} && {request_ready_signal};",
            f"  assign {response_payload_signal} = {response_payload};",
            f"  assign {response_valid_signal} = "
            f"{sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
            f"({name}_outstanding != '0 || {name}_request_transfer) && "
            f"({response_valid});",
            f"  assign {name}_response_transfer = "
            f"{response_valid_signal} && {response_ready_signal};",
        ))

    rr_logic.extend((
        f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier)}) begin",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier)}) "
        f"{name}_outstanding <= '0;",
        "    else begin",
        f"      case ({{{name}_request_transfer, {name}_response_transfer}})",
        f"        2'b10: if ({name}_outstanding < "
        f"{count_width}'d{maximum}) {name}_outstanding <= "
        f"{name}_outstanding + 1'b1;",
        f"        2'b01: if ({name}_outstanding != '0) "
        f"{name}_outstanding <= {name}_outstanding - 1'b1;",
        f"        default: {name}_outstanding <= {name}_outstanding;",
        "      endcase",
        "    end",
        "  end",
    ))

    if has_user_transition:
        state_logic = list(materialized)
        sv_state._append_unified_state(
            module,
            declarations,
            state_logic,
            render,
            excluded_assignment_names=frozenset(owned_assignment_names),
        )
        lines = [
            *declarations,
            *rr_declarations,
            *state_logic,
            *rr_logic,
        ]
    else:
        lines = [
            *declarations,
            *materialized,
            *rr_declarations,
            *rr_logic,
        ]
        for assignment in module.assignments:
            if not isinstance(assignment.target, ir_module.Port):
                continue
            lines.append(
                f"  assign {sv_rendering._identifier(assignment.target.name)} = "
                f"{render(assignment.expression)};"
            )
    return sv_module_rendering._module(
        module, sv_boundary._physical_port_declarations(module), lines
    )
def _find_channel_assignment(
    module: ir_module.Module,
    interface: str,
    channel: ir_interfaces.RequestResponseChannel,
    signal: ir_interfaces.ReadyValidSignal,
):
    matches = tuple(
        assignment for assignment in module.assignments
        if assignment.target.name == interface
        and assignment.channel is channel
        and assignment.signal is signal
    )
    if len(matches) != 1:
        raise SystemVerilogEmissionError(
            f"interface '{interface}.{channel.value}' requires one "
            f"'{signal.value}' assignment"
        )
    return matches[0]
