"""Ready/valid, credit, and protocol-adapter SystemVerilog emission."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.backend.systemverilog import boundary as sv_boundary
from zlang.backend.systemverilog import composed as composed
from zlang.backend.systemverilog import materialized as sv_materialized
from zlang.backend.systemverilog import module_rendering as sv_module_rendering
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.ir import formal_observations as formal_observations
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering


@dataclass(frozen=True)
class FormalAdapterCountProjection:
    """Typed formal-only count projection for one closed protocol adapter.

    Every field is checked against the authoritative ``Connection`` before RTL
    is emitted.  The physical signal is allocated here and passed explicitly
    to the FIFO helper; no generated instance or counter name is recovered.
    """

    observation_semantic_id: str
    source_port: str
    destination_port: str
    adapter: ir_interfaces.ConnectionAdapter
    signal: str
    width: int
    depth: int


def _emit_ready_valid(module: ir_module.Module) -> str:
    if module.registers or module.rules or module.fifos or module.memories:
        raise SystemVerilogEmissionError(
            "the direct ready/valid experiment supports combinational endpoints"
        )
    # A stateless protocol component may still declare a clock/reset domain so
    # it composes with clocked children and publishes a stable aggregate ABI.
    # Those timing ports do not make its data path sequential.
    ports: list[str] = [
        *(
            f"input logic {sv_rendering._identifier(name)}"
            for name in (module.clock,)
            if name is not None
        ),
        *(
            f"input logic {sv_rendering._identifier(name)}"
            for name in (module.reset,)
            if name is not None
        ),
    ]
    for port in module.ports:
        name = sv_rendering._identifier(port.name)
        if port.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID:
            ports.append(sv_boundary._port_declaration(port))
        elif port.direction is ir_module.PortDirection.INPUT:
            ports.extend(
                (
                    sv_boundary._logic_port("input", f"{name}_payload", port.type),
                    f"input logic {name}_valid",
                    f"output logic {name}_ready",
                )
            )
        else:
            ports.extend(
                (
                    sv_boundary._logic_port("output", f"{name}_payload", port.type),
                    f"output logic {name}_valid",
                    f"input logic {name}_ready",
                )
            )
    declarations, materialized, render = sv_materialized._materialized_emission(module)
    lines = [
        f"  assign {sv_rendering._assignment_name(assignment)} = "
        f"{render(assignment.expression)};"
        for assignment in module.assignments
    ]
    return sv_module_rendering._module(
        module, ports, [*declarations, *materialized, *lines]
    )


def _emit_credit(module: ir_module.Module) -> str:
    if module.clock is None or module.reset is None:
        raise SystemVerilogEmissionError(
            "direct credit emission requires one clock and reset"
        )
    credit_ports = tuple(
        port for port in module.ports
        if port.protocol is ir_interfaces.InterfaceProtocol.CREDIT
    )
    if len(credit_ports) != 1 or credit_ports[0].direction is not ir_module.PortDirection.OUTPUT:
        raise SystemVerilogEmissionError(
            "the direct credit experiment supports one sender endpoint"
        )
    endpoint = credit_ports[0]
    assert endpoint.capacity is not None
    payload_assignment = _find_signal_assignment(
        module, endpoint.name, ir_interfaces.CreditSignal.PAYLOAD
    )
    send_assignment = _find_signal_assignment(
        module, endpoint.name, ir_interfaces.CreditSignal.SEND
    )
    count_width = max(1, endpoint.capacity.bit_length())
    ports = [
        f"input logic {sv_rendering._identifier(module.clock)}",
        f"input logic {sv_rendering._identifier(module.reset)}",
        *(
            sv_boundary._port_declaration(port)
            for port in module.ports
            if port.protocol is ir_interfaces.InterfaceProtocol.WIRE
        ),
        f"input logic {endpoint.name}_return",
        sv_boundary._logic_port(
            "output", f"{endpoint.name}_payload", endpoint.type
        ),
        f"output logic {endpoint.name}_send",
    ]
    name = endpoint.name
    lines = [
        f"  logic [{count_width - 1}:0] {name}_credits;",
        f"  assign {name}_payload = {sv_expression._expression(payload_assignment.expression)};",
        f"  assign {name}_send = {sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && ({name}_credits != '0) "
        f"&& {sv_expression._expression(send_assignment.expression)};",
        *(
            f"  assign {sv_rendering._assignment_name(assignment)} = "
            f"{sv_expression._expression(assignment.expression)};"
            for assignment in module.assignments
            if isinstance(assignment.target, ir_module.Port)
            and assignment.target.protocol is ir_interfaces.InterfaceProtocol.WIRE
            and assignment.target.direction is ir_module.PortDirection.OUTPUT
        ),
        f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier)}) begin",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier)}) begin",
        f"      {name}_credits <= {count_width}'d{endpoint.capacity};",
        "    end else begin",
        f"      case ({{{name}_send, {name}_return}})",
        f"        2'b10: {name}_credits <= {name}_credits - 1'b1;",
        f"        2'b01: if ({name}_credits < {count_width}'d{endpoint.capacity}) "
        f"{name}_credits <= {name}_credits + 1'b1;",
        f"        default: {name}_credits <= {name}_credits;",
        "      endcase",
        "    end",
        "  end",
    ]
    return sv_module_rendering._module(module, ports, lines)


def _emit_vc_credit(module: ir_module.Module) -> str:
    if module.clock is None or module.reset is None:
        raise SystemVerilogEmissionError(
            "direct VC-credit emission requires one clock and reset"
        )
    endpoints = tuple(
        port for port in module.ports
        if port.protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT
    )
    if len(endpoints) != 1 or endpoints[0].direction is not ir_module.PortDirection.OUTPUT:
        raise SystemVerilogEmissionError(
            "direct VC-credit emission supports one sender endpoint"
        )
    endpoint = endpoints[0]
    if endpoint.capacity is None or endpoint.virtual_channels is None:
        raise SystemVerilogEmissionError("VC-credit endpoint has no typed bounds")
    payload = _find_signal_assignment(
        module, endpoint.name, ir_interfaces.VirtualChannelCreditSignal.PAYLOAD
    )
    channel = _find_signal_assignment(
        module, endpoint.name, ir_interfaces.VirtualChannelCreditSignal.VC
    )
    request = _find_signal_assignment(
        module, endpoint.name, ir_interfaces.VirtualChannelCreditSignal.SEND
    )
    name = sv_rendering._identifier(endpoint.name)
    clock = sv_rendering._identifier(module.clock)
    reset = sv_rendering._identifier(module.reset)
    capacity = endpoint.capacity
    virtual_channels = endpoint.virtual_channels
    count_width = max(1, capacity.bit_length())
    vc_width = max(1, (virtual_channels - 1).bit_length())
    ports = [
        f"input logic {clock}",
        f"input logic {reset}",
        *(
            sv_boundary._port_declaration(port)
            for port in module.ports
            if port.protocol is ir_interfaces.InterfaceProtocol.WIRE
        ),
        f"input logic {name}_return",
        f"input logic {sv_rendering._range(vc_width)}{name}_return_vc",
        sv_boundary._logic_port("output", f"{name}_payload", endpoint.type),
        f"output logic {sv_rendering._range(vc_width)}{name}_vc",
        f"output logic {name}_send",
    ]
    lines = [
        *(
            f"  logic [{count_width - 1}:0] {name}_credits_{index};"
            for index in range(virtual_channels)
        ),
        f"  logic {name}_can_send;",
        f"  assign {name}_payload = {sv_expression._expression(payload.expression)};",
        f"  assign {name}_vc = {sv_expression._expression(channel.expression)};",
        "  always_comb begin",
        f"    {name}_can_send = 1'b0;",
        f"    case ({name}_vc)",
        *(
            f"      {vc_width}'d{index}: {name}_can_send = "
            f"({name}_credits_{index} != '0);"
            for index in range(virtual_channels)
        ),
        "      default: begin end",
        "    endcase",
        "  end",
        f"  assign {name}_send = {sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && "
        f"({sv_expression._expression(request.expression)}) && {name}_can_send;",
        f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier)}) begin",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier)}) begin",
        *(
            f"      {name}_credits_{index} <= {count_width}'d{capacity};"
            for index in range(virtual_channels)
        ),
        "    end else begin",
    ]
    for index in range(virtual_channels):
        sent = f"({name}_send && {name}_vc == {vc_width}'d{index})"
        returned = (
            f"({name}_return && {name}_return_vc == {vc_width}'d{index})"
        )
        lines.extend((
            f"      case ({{{sent}, {returned}}})",
            f"        2'b10: if ({name}_credits_{index} != '0) "
            f"{name}_credits_{index} <= {name}_credits_{index} - 1'b1;",
            f"        2'b01: if ({name}_credits_{index} < "
            f"{count_width}'d{capacity}) {name}_credits_{index} <= "
            f"{name}_credits_{index} + 1'b1;",
            f"        default: {name}_credits_{index} <= {name}_credits_{index};",
            "      endcase",
        ))
    lines.extend(("    end", "  end"))
    return sv_module_rendering._module(module, ports, lines)


def _validate_formal_adapter_count_projection(
    module: ir_module.Module,
    projection: FormalAdapterCountProjection,
) -> tuple[ir_module.Assignment, ...]:
    """Seal one formal projection against its typed adapter and output port."""

    if len(module.connections) != 1:
        raise SystemVerilogEmissionError(
            "formal adapter count projection requires exactly one typed connection"
        )
    connection = module.connections[0]
    source = connection.source
    destination = connection.destination
    expected_width = max(1, connection.buffer_depth.bit_length())
    if (
        projection.observation_semantic_id
        != formal_observations.port_observation_id(source.name, "occupancy")
        or projection.source_port != source.name
        or projection.destination_port != destination.name
        or projection.adapter is not ir_interfaces.ConnectionAdapter.CREDIT_TO_READY_VALID
        or connection.adapter is not projection.adapter
        or source.protocol is not ir_interfaces.InterfaceProtocol.CREDIT
        or source.direction is not ir_module.PortDirection.INPUT
        or destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
        or destination.direction is not ir_module.PortDirection.OUTPUT
        or source.capacity is None
        or projection.depth != source.capacity
        or connection.buffer_depth != projection.depth
        or projection.width != expected_width
        or source.type != destination.type
        or source.domain != destination.domain
    ):
        raise SystemVerilogEmissionError(
            "formal adapter count projection disagrees with the typed "
            "credit_to_rv connection"
        )
    if sv_rendering._identifier(projection.signal) != projection.signal:
        raise SystemVerilogEmissionError(
            "formal adapter count projection has an invalid physical signal"
        )
    count_matches = 0
    seen_targets: set[str] = set()
    validated: list[ir_module.Assignment] = []
    for assignment in module.assignments:
        target = assignment.target
        value = assignment.expression
        if (
            assignment.signal is not None
            or assignment.channel is not None
            or not isinstance(target, ir_module.Port)
            or target.direction is not ir_module.PortDirection.OUTPUT
            or target.protocol is not ir_interfaces.InterfaceProtocol.WIRE
            or target.type != value.type
            or target.name in seen_targets
        ):
            raise SystemVerilogEmissionError(
                "closed protocol adapter accepts only exact formal observation "
                "projection assignments"
            )
        is_count = (
            isinstance(value, expr.InputRef)
            and value.name == projection.signal
            and value.type == ir_types.UIntType(projection.width)
        )
        is_source_leaf = (
            isinstance(value, expr.CreditRef)
            and value.interface == source.name
            and value.signal in {
                ir_interfaces.CreditSignal.PAYLOAD,
                ir_interfaces.CreditSignal.SEND,
                ir_interfaces.CreditSignal.RETURN,
            }
        )
        is_destination_leaf = (
            isinstance(value, expr.ReadyValidRef)
            and value.interface == destination.name
            and value.signal in {
                ir_interfaces.ReadyValidSignal.PAYLOAD,
                ir_interfaces.ReadyValidSignal.VALID,
                ir_interfaces.ReadyValidSignal.READY,
            }
        )
        if not (is_count or is_source_leaf or is_destination_leaf):
            raise SystemVerilogEmissionError(
                "closed protocol adapter formal ABI contains an observation "
                "outside its typed endpoints or FIFO count"
            )
        count_matches += int(is_count)
        seen_targets.add(target.name)
        validated.append(assignment)
    if count_matches != 1:
        raise SystemVerilogEmissionError(
            "closed protocol adapter formal ABI requires exactly one typed "
            "FIFO count projection"
        )
    return tuple(validated)


def _emit_connection_adapter(
    module: ir_module.Module,
    *,
    formal_adapter_counts: tuple[FormalAdapterCountProjection, ...] = (),
) -> str:
    if (
        module.clock is None
        or module.reset is None
        or len(module.connections) != 1
        or module.registers
        or module.rules
        or module.fifos
        or module.memories
        or module.csr_blocks
        or module.elaborated_instances
    ):
        raise SystemVerilogEmissionError(
            "direct protocol adapter requires one closed clocked connection"
        )
    if len(formal_adapter_counts) > 1:
        raise SystemVerilogEmissionError(
            "duplicate formal count projections for one protocol adapter"
        )
    formal_assignments = (
        _validate_formal_adapter_count_projection(
            module, formal_adapter_counts[0]
        )
        if formal_adapter_counts else None
    )
    if formal_assignments is None and module.assignments:
        raise SystemVerilogEmissionError(
            "direct protocol adapter requires one closed clocked connection"
        )
    connection = module.connections[0]
    source, destination = connection.source, connection.destination
    if source.type != destination.type or source.domain != destination.domain:
        raise SystemVerilogEmissionError(
            "direct protocol adapter requires identical payload type and domain"
        )

    ports = [
        f"input logic {sv_rendering._identifier(module.clock)}",
        f"input logic {sv_rendering._identifier(module.reset)}",
    ]
    for port in module.ports:
        name = sv_rendering._identifier(port.name)
        if port.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
            if port.direction is ir_module.PortDirection.INPUT:
                ports.extend((
                    sv_boundary._logic_port("input", f"{name}_payload", port.type),
                    f"input logic {name}_valid",
                    f"output logic {name}_ready",
                ))
            else:
                ports.extend((
                    sv_boundary._logic_port("output", f"{name}_payload", port.type),
                    f"output logic {name}_valid",
                    f"input logic {name}_ready",
                ))
        elif port.protocol is ir_interfaces.InterfaceProtocol.CREDIT:
            if port.direction is ir_module.PortDirection.INPUT:
                ports.extend((
                    sv_boundary._logic_port("input", f"{name}_payload", port.type),
                    f"input logic {name}_send",
                    f"output logic {name}_return",
                ))
            else:
                ports.extend((
                    sv_boundary._logic_port("output", f"{name}_payload", port.type),
                    f"output logic {name}_send",
                    f"input logic {name}_return",
                ))
        elif (
            formal_assignments is not None
            and port in {item.target for item in formal_assignments}
        ):
            ports.append(sv_boundary._port_declaration(port))
        else:
            raise SystemVerilogEmissionError(
                "direct protocol adapter accepts only ready/valid and credit ports"
            )

    src = sv_rendering._identifier(source.name)
    dst = sv_rendering._identifier(destination.name)
    clock = sv_rendering._identifier(module.clock)
    reset = sv_sequential.effective_reset_signal(module, sv_rendering._identifier)
    reset_deasserted = sv_sequential.reset_deasserted(module, sv_rendering._identifier)
    if connection.adapter is ir_interfaces.ConnectionAdapter.READY_VALID_TO_CREDIT:
        if (
            source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            or destination.protocol is not ir_interfaces.InterfaceProtocol.CREDIT
            or destination.capacity is None
        ):
            raise SystemVerilogEmissionError("invalid typed rv_to_credit adapter")
        capacity = destination.capacity
        count_width = max(1, capacity.bit_length())
        lines = [
            f"  logic [{count_width - 1}:0] {dst}_credits;",
            f"  assign {src}_ready = {sv_sequential.reset_deasserted(module, sv_rendering._identifier)} && ({dst}_credits != '0);",
            f"  assign {dst}_payload = {src}_payload;",
            f"  assign {dst}_send = {src}_valid && {src}_ready;",
            f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier)}) begin",
            f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier)}) {dst}_credits <= {count_width}'d{capacity};",
            "    else begin",
            f"      case ({{{dst}_send, {dst}_return}})",
            f"        2'b10: {dst}_credits <= {dst}_credits - 1'b1;",
            f"        2'b01: if ({dst}_credits < {count_width}'d{capacity}) "
            f"{dst}_credits <= {dst}_credits + 1'b1;",
            f"        default: {dst}_credits <= {dst}_credits;",
            "      endcase",
            "    end",
            "  end",
        ]
        return sv_module_rendering._module(module, ports, lines)

    if connection.adapter is ir_interfaces.ConnectionAdapter.CREDIT_TO_READY_VALID:
        if (
            source.protocol is not ir_interfaces.InterfaceProtocol.CREDIT
            or destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            or source.capacity is None
            or connection.buffer_depth != source.capacity
        ):
            raise SystemVerilogEmissionError(
                "invalid typed credit_to_rv adapter buffer"
            )
        depth = connection.buffer_depth
        width = sv_rendering._width(source.type)
        projection = (
            formal_adapter_counts[0] if formal_adapter_counts else None
        )
        helper = (
            f"ZLangRvFifoFormal_{width}_{depth}"
            if projection is not None
            else f"ZLangRvFifo_{width}_{depth}"
        )
        lines = [
            f"  logic {src}_accepted;",
            f"  logic {sv_rendering._range(width)}{dst}_buffer_payload;",
            f"  logic {dst}_buffer_valid, {dst}_buffer_ready;",
            *(
                (f"  logic {sv_rendering._range(projection.width)}{projection.signal};",)
                if projection is not None else ()
            ),
            f"  {helper} zlang_adapter_fifo (",
            f"    .clk({clock}), .rst({reset}),",
            f"    .in_payload({src}_payload), "
            f".in_valid({src}_send && {reset_deasserted}),",
            f"    .in_ready({src}_accepted),",
            f"    .out_payload({dst}_buffer_payload),",
            f"    .out_valid({dst}_buffer_valid), .out_ready({dst}_buffer_ready)"
            + ("," if projection is not None else ");"),
            *(
                (f"    .formal_count({projection.signal}));",)
                if projection is not None else ()
            ),
            f"  assign {dst}_payload = {dst}_buffer_payload;",
            f"  assign {dst}_valid = {reset_deasserted} && "
            f"{dst}_buffer_valid;",
            f"  assign {dst}_buffer_ready = {reset_deasserted} && "
            f"{dst}_ready;",
            f"  assign {src}_return = {dst}_valid && {dst}_ready;",
            *(
                tuple(
                    f"  assign {sv_rendering._identifier(assignment.target.name)} = "
                    f"{sv_expression._expression(assignment.expression)};"
                    for assignment in formal_assignments
                )
                if formal_assignments is not None else ()
            ),
        ]
        return composed.rv_fifo_helper(
            helper, width, depth, module,
            expose_count=projection is not None,
            allow_full_replace=True,
        ) + "\n" + sv_module_rendering._module(module, ports, lines)

    raise SystemVerilogEmissionError(
        f"unsupported direct protocol adapter '{connection.adapter}'"
    )
def _find_signal_assignment(
    module: ir_module.Module, endpoint: str, signal: object
):
    matches = tuple(
        assignment for assignment in module.assignments
        if assignment.target.name == endpoint and assignment.signal is signal
    )
    if len(matches) != 1:
        raise SystemVerilogEmissionError(
            f"endpoint '{endpoint}' requires one '{signal.value}' assignment"
        )
    return matches[0]
