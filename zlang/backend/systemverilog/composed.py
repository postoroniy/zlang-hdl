"""Backend-local direct-SystemVerilog lowering for composed hierarchy.

The subsystem consumes already-typed hierarchy, endpoint, connection, and state
IR. It deliberately knows nothing about source syntax. Static rendering rules
come directly from their authoritative owners; only per-run external and CDC
renderers are injected.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from zlang.ir import expressions as ir_expr
from zlang.ir.expressions import Expression
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir.traversal import walk_expression
from zlang.backend import expression_materialization as materialization
from zlang.backend import identifiers
from zlang.backend import naming as naming
from zlang.backend.systemverilog import boundary as sv_boundary
from zlang.backend.systemverilog import context as emission_context
from zlang.backend.systemverilog import csr as sv_csr
from zlang.backend.systemverilog import materialized as sv_materialized
from zlang.backend.systemverilog import module_rendering as sv_module_rendering
from zlang.backend.systemverilog import physicalization as sv_physicalization
from zlang.backend.systemverilog import pipeline as sv_pipeline
from zlang.backend.systemverilog import rendering as sv_rendering
from zlang.backend.systemverilog import rules as sv_rules
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog import request_response as sv_request_response
from zlang.backend.systemverilog import state as sv_state
from zlang.backend.systemverilog import state_planning as sv_state_planning
from zlang.backend.systemverilog import storage as sv_storage
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog.composed_model import (
    ComposedRendering,
    FormalBufferCountProjection,
)


ExpressionRenderer = Callable[[Expression], str]


def component_name(
    module: ir_module.Module,
    specialization: str | None,
    rendering: ComposedRendering,
    *,
    top: bool = False,
) -> str:
    """Resolve a physical definition from the complete typed catalog."""

    if top:
        return sv_rendering._identifier(module.name)
    if rendering.component_names is None or specialization is None:
        raise SystemVerilogEmissionError(
            "physical component naming requires a validated specialization catalog"
        )
    return rendering.component_names.component(module.name, specialization)


def request_response_tracker_name(
    descriptor: ir_module.RequestResponseConnection,
    module: ir_module.Module | None = None,
) -> str:
    """Return the ledger token shared by production and formal projection."""

    if module is not None:
        return sv_request_response.tracker_name(
            descriptor,
            module,
            identifier=sv_rendering._identifier,
        )
    request_owner = identifiers.rtl_instance_identifier(
        descriptor.request.source.owner
    )
    response_owner = identifiers.rtl_instance_identifier(
        descriptor.response.destination.owner
    )
    return sv_rendering._identifier(
        f"rr_{request_owner}_"
        f"{sv_rendering._identifier(descriptor.request.source.name)}_"
        f"{response_owner}_outstanding"
    )


def emit_composed_design(module: ir_module.Module, rendering: ComposedRendering) -> str:
    """Emit every specialization once and every physical instance once."""

    hierarchy = sv_physicalization.validated_hierarchy(
        module, cache=rendering.hierarchy_cache
    )
    rendering = replace(
        rendering,
        component_names=naming.build_component_name_plan(
            hierarchy, identifier=sv_rendering._identifier,
        ),
    )
    emitted: dict[tuple[str, str], str] = {}
    definitions: list[str] = []

    def visit(current: ir_module.Module, name: str, *, root: bool = False) -> None:
        # Only the selected root owns the physical reset conditioner.  Every
        # reusable child specialization receives the already-conditioned reset
        # through its ordinary reset port and therefore uses a native internal
        # release ABI.
        emitted_current = current if root else sv_sequential.native_release_module(current)
        child_names: dict[str, str] = {}
        for child, elaborated in zip(
            emitted_current.children,
            emitted_current.elaborated_instances,
            strict=True,
        ):
            child_name = component_name(
                child, elaborated.specialization_identity, rendering
            )
            child_names[elaborated.instance.name] = child_name
            key = (child_name, elaborated.specialization_identity or "")
            if key not in emitted:
                with emission_context.top_boundary_scope(None):
                    visit(child, child_name)
                emitted[key] = child_name
        closed_state_component = (
            emitted_current.resolved_transition is not None
            and not emitted_current.elaborated_instances
            and not emitted_current.connections
            and not emitted_current.hierarchical_connections
            and not emitted_current.csr_blocks
            and (
                emitted_current.resolved_transition.resources
                or emitted_current.resolved_transition.action_groups
                or emitted_current.fifos
                or emitted_current.registers
                or emitted_current.rules
                or emitted_current.next_assignments
            )
            and not any(
                not fifo.scheduled for fifo in emitted_current.fifos
            )
        )
        # A child with a resolved transition is a complete stateful
        # specialization, not a FIFO-only helper.  Preserve all of its
        # registers, rules, protocol ports, and scalar expressions in one
        # reusable component.  The standalone FIFO path remains for the
        # legacy one-resource module shape only.
        if emitted_current.external_contract is not None:
            definitions.append(rendering.emit_external(emitted_current, name))
        elif (
            emitted_current.elastic_pipeline_regions
            and not emitted_current.elaborated_instances
        ):
            definitions.append(
                sv_pipeline._emit_elastic_pipeline(
                    replace(emitted_current, name=name)
                )
            )
        elif (
            emitted_current.fifos
            and not emitted_current.elaborated_instances
            and any(not fifo.scheduled for fifo in emitted_current.fifos)
            and not emitted_current.registers
            and not emitted_current.rules
        ):
            definitions.append(
                sv_storage._emit_fifo(replace(emitted_current, name=name))
            )
        elif (
            emitted_current.memories
            and not emitted_current.elaborated_instances
            and all(
                not memory.scheduled for memory in emitted_current.memories
            )
            and not emitted_current.registers
            and not emitted_current.rules
        ):
            definitions.append(
                sv_storage._emit_memory(replace(emitted_current, name=name))
            )
        elif (
            emitted_current.roms
            and not emitted_current.elaborated_instances
            and not emitted_current.registers
            and not emitted_current.rules
        ):
            definitions.append(
                sv_storage._emit_rom_module(replace(emitted_current, name=name))
            )
        elif (
            closed_state_component
            and sv_state_planning.requires_unified_state(emitted_current)
        ):
            definitions.append(
                sv_storage._emit_unified_state_module(
                    replace(emitted_current, name=name)
                )
            )
        elif (
            closed_state_component
            and emitted_current.rules
            and not emitted_current.request_responses
            and not emitted_current.protocol_endpoints
            and not emitted_current.aggregate_protocol_endpoints
            and all(
                port.protocol is ir_interfaces.InterfaceProtocol.WIRE
                for port in emitted_current.ports
            )
        ):
            definitions.append(
                sv_rules._emit_rules(replace(emitted_current, name=name))
            )
        elif (
            emitted_current.csr_blocks
            and not emitted_current.elaborated_instances
        ):
            # A CSR bank is an ordinary closed child specialization.  Its
            # behavior still comes from typed CSR IR; the composed parent only
            # wires the canonical scalar access ABI.
            definitions.append(
                sv_csr._emit_csr(
                    replace(emitted_current, name=name), expose_internal_abi=True
                )
            )
        elif (
            any(
                connection.crossing is not None
                for connection in emitted_current.connections
            )
            and not emitted_current.elaborated_instances
        ):
            # A typed CDC child owns synchronizer/storage state.  It cannot be
            # rendered as the ordinary combinational hierarchy shell.
            definitions.append(
                rendering.emit_cdc_child(
                    replace(emitted_current, name=name)
                )
            )
        else:
            definitions.append(
                _emit_composed_component(
                    emitted_current, name, child_names, rendering
                )
            )

    visit(
        module,
        component_name(module, None, rendering, top=True),
        root=True,
    )
    return "\n".join(definitions)


def _endpoint_base(
    module: ir_module.Module,
    endpoint: ir_module.ProtocolEndpoint,
) -> str:
    identifier = (
        sv_rendering._identifier
        if endpoint.owner == module.name
        else identifiers.rtl_identifier
    )
    base = identifier(endpoint.name)
    if endpoint.channel is not None:
        base += f"_{endpoint.channel.value}"
    return base


def _connection_key(
    module: ir_module.Module,
    endpoint: ir_module.ProtocolEndpoint,
    signal: str,
) -> tuple[str, str, str]:
    return (endpoint.owner, _endpoint_base(module, endpoint), signal)


def _connection_pair_keys(
    module: ir_module.Module,
    connection: ir_module.HierarchicalConnection,
    signal: str,
) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    return (
        _connection_key(module, connection.source, signal),
        _connection_key(module, connection.destination, signal),
    )


def _external_protocol_signal(
    module: ir_module.Module,
    endpoint: ir_module.ProtocolEndpoint,
    signal: str,
    identifier: Callable[[str], str],
) -> str | None:
    """Return the already-declared top ABI signal for a top endpoint."""

    if endpoint.owner != module.name:
        return None
    base = identifier(endpoint.name)
    if endpoint.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
        return f"{base}_{signal}"
    if signal == "wire":
        return base
    return None


def composed_component_identifier_claims(
    module: ir_module.Module,
    rendering: ComposedRendering,
) -> tuple[tuple[str, str], ...]:
    """Return typed internal names emitted by one composed component.

    The namespace validator consumes this plan before rendering.  Keep the
    decision rules aligned with :func:`_emit_composed_component`: in
    particular, a scalar child output gets a private interconnect only when no
    typed hierarchy/delegation edge already supplies that connection.
    """

    local_names = emission_context.cached_module_rtl_names(module)
    sv_physicalization.validated_hierarchy(
        module, cache=rendering.hierarchy_cache
    )
    identifier = sv_rendering._identifier
    claims: list[tuple[str, str]] = []
    connection_keys: set[tuple[str, str, str]] = set()

    def claim(token: str, owner: str) -> None:
        claims.append((token, owner))

    rr_edges = {
        id(edge): (descriptor, channel)
        for descriptor in module.request_response_connections
        for channel, edge in (
            (ir_interfaces.RequestResponseChannel.REQUEST, descriptor.request),
            (ir_interfaces.RequestResponseChannel.RESPONSE, descriptor.response),
        )
    }
    for descriptor in module.request_response_connections:
        tracker = request_response_tracker_name(descriptor, module)
        claim(tracker, f"request/response '{descriptor.semantic_id}' ledger")
        claim(
            f"{tracker}_request_transfer",
            f"request/response '{descriptor.semantic_id}' request transfer",
        )
        claim(
            f"{tracker}_response_transfer",
            f"request/response '{descriptor.semantic_id}' response transfer",
        )

    for index, connection in enumerate(module.connections):
        if connection.buffer_depth:
            claim(
                f"zlang_local_fifo_{index}",
                f"buffered local connection {index} instance",
            )

    for index, connection in enumerate(module.hierarchical_connections):
        rr_info = rr_edges.get(id(connection))
        if connection.source.protocol is ir_interfaces.InterfaceProtocol.WIRE:
            shared = (
                _external_protocol_signal(
                    module, connection.source, "wire", identifier,
                )
                or _external_protocol_signal(
                    module, connection.destination, "wire", identifier,
                )
            )
            if shared is None:
                claim(f"zlang_conn_{index}", f"scalar hierarchy connection {index}")
            connection_keys.update(
                _connection_pair_keys(module, connection, "wire")
            )
            continue

        depth = connection.buffer_depth
        if connection.source.channel is ir_interfaces.RequestResponseChannel.REQUEST:
            depth = connection.request_buffer_depth
        elif connection.source.channel is ir_interfaces.RequestResponseChannel.RESPONSE:
            depth = connection.response_buffer_depth
        base = f"zlang_conn_{index}"
        if depth:
            for direction in ("up", "down"):
                for signal in ("payload", "valid", "ready"):
                    claim(
                        f"{base}_{direction}_{signal}",
                        f"buffered hierarchy connection {index} {direction} {signal}",
                    )
            if rr_info is not None:
                claim(
                    f"{base}_down_valid_allowed",
                    f"request/response connection {index} admitted valid",
                )
            claim(f"{base}_fifo", f"buffered hierarchy connection {index} instance")
            for projection in rendering.formal_buffer_counts:
                descriptor, channel = rr_info or (None, None)
                if (
                    descriptor is not None
                    and projection.request_response_semantic_id
                    == descriptor.semantic_id
                    and projection.channel is channel
                ):
                    claim(
                        projection.signal,
                        f"formal hierarchy buffer projection {index}",
                    )
            for signal in ("payload", "valid", "ready"):
                connection_keys.update(
                    _connection_pair_keys(module, connection, signal)
                )
            continue

        if rr_info is None:
            shared = (
                _external_protocol_signal(
                    module, connection.source, "payload", identifier,
                )
                or _external_protocol_signal(
                    module, connection.destination, "payload", identifier,
                )
            )
            if shared is None:
                for signal in ("payload", "valid", "ready"):
                    claim(
                        f"{base}_{signal}",
                        f"hierarchy connection {index} {signal}",
                    )
        else:
            for side in ("src", "dst"):
                for signal in ("payload", "valid", "ready"):
                    claim(
                        f"{base}_{side}_{signal}",
                        f"request/response connection {index} {side} {signal}",
                    )
        for signal in ("payload", "valid", "ready"):
            connection_keys.update(
                _connection_pair_keys(module, connection, signal)
            )

    aggregates = {item.name: item for item in module.aggregate_protocol_endpoints}
    children_by_instance = {
        item.instance.name: module.children[index]
        for index, item in enumerate(module.elaborated_instances)
    }
    for connection in module.aggregate_protocol_connections:
        if not connection.delegation or "." not in connection.destination:
            continue
        top_name = connection.source
        child_owner, child_name = connection.destination.split(".", 1)
        top = aggregates[top_name]
        child = next(
            item
            for item in children_by_instance[child_owner].aggregate_protocol_endpoints
            if item.name == child_name
        )
        for member in top.members:
            if member.protocol is ir_interfaces.InterfaceProtocol.WIRE:
                connection_keys.add(
                    (
                        child_owner,
                        f"{identifiers.rtl_identifier(child.name)}__"
                        f"{identifiers.rtl_identifier(member.name)}",
                        "wire",
                    )
                )

    for index, elaborated in enumerate(module.elaborated_instances):
        child = module.children[index]
        owner = elaborated.instance.name
        for port in child.ports:
            port_name = identifiers.rtl_identifier(port.name)
            if (
                port.protocol is ir_interfaces.InterfaceProtocol.WIRE
                and port.direction is ir_module.PortDirection.OUTPUT
                and (owner, port_name, "wire") not in connection_keys
            ):
                claim(
                    local_names.child_signal(owner, port.name),
                    f"child '{owner}' scalar output '{port.name}'",
                )

    return tuple(claims)


def _append_composed_connection_logic(
    module, declarations, logic, connection_signals, fifo_helpers,
    rr_edges, rr_tracker_names, formal_count_by_edge,
) -> None:
    """Render local/hierarchical protocol edges and request ledgers."""

    identifier = sv_rendering._identifier
    packed_width = sv_rendering._width
    packed_range = sv_rendering._range
    rr_transfer_signals: dict[int, tuple[str, str]] = {}
    for index, connection in enumerate(module.connections):
        if not connection.buffer_depth:
            continue
        if (
            connection.source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            or connection.destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
        ):
            raise SystemVerilogEmissionError(
                "composed buffering currently requires ready/valid endpoints"
            )
        if module.clock is None or module.reset is None:
            raise SystemVerilogEmissionError(
                "buffered connection requires clock/reset"
            )
        width = packed_width(connection.source.type)
        helper = f"ZLangRvFifo_{width}_{connection.buffer_depth}"
        fifo_helpers.append(
            (helper, width, connection.buffer_depth, False, True)
        )
        source = identifier(connection.source.name)
        destination = identifier(connection.destination.name)
        logic.extend((
            f"  {helper} zlang_local_fifo_{index} (",
            f"    .clk({identifier(module.clock)}), "
            f".rst({sv_sequential.effective_reset_signal(module, identifier)}),",
            f"    .in_payload({source}_payload), "
            f".in_valid({source}_valid), .in_ready({source}_ready),",
            f"    .out_payload({destination}_payload), "
            f".out_valid({destination}_valid), "
            f".out_ready({destination}_ready));",
        ))

    for index, connection in enumerate(module.hierarchical_connections):
        rr_info = rr_edges.get(id(connection))
        width = packed_width(connection.source.payload_type)
        if connection.source.protocol is ir_interfaces.InterfaceProtocol.WIRE:
            if connection.destination.protocol is not ir_interfaces.InterfaceProtocol.WIRE:
                raise SystemVerilogEmissionError(
                    "hierarchical scalar connection requires two wire endpoints"
                )
            if (
                connection.buffer_depth
                or connection.request_buffer_depth
                or connection.response_buffer_depth
                or connection.adapter is not None
                or connection.crossing is not None
            ):
                raise SystemVerilogEmissionError(
                    "hierarchical scalar connection does not accept protocol options"
                )
            base = f"zlang_conn_{index}"
            source_external = _external_protocol_signal(
                module, connection.source, "wire", identifier,
            )
            destination_external = _external_protocol_signal(
                module, connection.destination, "wire", identifier,
            )
            shared = source_external or destination_external
            if shared is None:
                shared = base
                declarations.append(f"  logic {packed_range(width)}{shared};")
            for key in _connection_pair_keys(module, connection, "wire"):
                connection_signals[key] = shared
            continue
        if (
            connection.source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            or connection.destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
        ):
            raise SystemVerilogEmissionError(
                "hierarchical protocol connection requires matching "
                "ready/valid endpoints"
            )
        depth = connection.buffer_depth
        if connection.source.channel is ir_interfaces.RequestResponseChannel.REQUEST:
            depth = connection.request_buffer_depth
        elif connection.source.channel is ir_interfaces.RequestResponseChannel.RESPONSE:
            depth = connection.response_buffer_depth
        if depth:
            count_projection = formal_count_by_edge.get(id(connection))
            up = f"zlang_conn_{index}_up"
            down = f"zlang_conn_{index}_down"
            for prefix in (up, down):
                declarations.extend((
                    f"  logic {packed_range(width)}{prefix}_payload;",
                    f"  logic {prefix}_valid;",
                    f"  logic {prefix}_ready;",
                ))
            destination_valid = f"{down}_valid"
            if rr_info is not None:
                descriptor, channel = rr_info
                tracker, maximum = rr_tracker_names[id(descriptor)]
                count_width = max(1, maximum.bit_length())
                active = f"!({sv_sequential.reset_asserted(module, identifier)})"
                allow = (
                    f"({active} && {tracker} < {count_width}'d{maximum})"
                    if channel is ir_interfaces.RequestResponseChannel.REQUEST
                    else f"({active} && ({tracker} != '0 || "
                    f"{tracker}_request_transfer))"
                )
                destination_valid = f"{down}_valid_allowed"
                declarations.append(f"  logic {destination_valid};")
                logic.append(
                    f"  assign {destination_valid} = {down}_valid && {allow};"
                )
                rr_transfer_signals[id(connection)] = (
                    destination_valid,
                    f"{down}_ready",
                )
            for signal in ("payload", "valid", "ready"):
                connection_signals[
                    _connection_key(module, connection.source, signal)
                ] = f"{up}_{signal}"
                connection_signals[
                    _connection_key(
                        module,
                        connection.destination,
                        signal,
                    )
                ] = destination_valid if signal == "valid" else f"{down}_{signal}"
            source_payload = _external_protocol_signal(
                module, connection.source, "payload", identifier,
            )
            source_valid = _external_protocol_signal(
                module, connection.source, "valid", identifier,
            )
            source_ready = _external_protocol_signal(
                module, connection.source, "ready", identifier,
            )
            if source_payload is not None:
                assert source_valid is not None and source_ready is not None
                logic.extend((
                    f"  assign {up}_payload = {source_payload};",
                    f"  assign {up}_valid = {source_valid};",
                    f"  assign {source_ready} = {up}_ready;",
                ))
            destination_payload = _external_protocol_signal(
                module, connection.destination, "payload", identifier,
            )
            destination_ready = _external_protocol_signal(
                module, connection.destination, "ready", identifier,
            )
            destination_external_valid = _external_protocol_signal(
                module, connection.destination, "valid", identifier,
            )
            if destination_payload is not None:
                assert (
                    destination_ready is not None
                    and destination_external_valid is not None
                )
                logic.extend((
                    f"  assign {destination_payload} = {down}_payload;",
                    f"  assign {destination_external_valid} = {destination_valid};",
                    f"  assign {down}_ready = {destination_ready};",
                ))
            allow_full_replace = rr_info is None
            helper_family = (
                "ZLangRvFifo"
                if allow_full_replace else "ZLangRvFifoConservative"
            )
            helper = (
                f"{helper_family}Formal_{width}_{depth}"
                if count_projection is not None
                else f"{helper_family}_{width}_{depth}"
            )
            fifo_helpers.append(
                (
                    helper, width, depth, count_projection is not None,
                    allow_full_replace,
                )
            )
            if module.clock is None or module.reset is None:
                raise SystemVerilogEmissionError(
                    "buffered hierarchy requires clock/reset"
                )
            instance_lines = [
                f"  {helper} zlang_conn_{index}_fifo (",
                f"    .clk({identifier(module.clock)}), "
                f".rst({sv_sequential.effective_reset_signal(module, identifier)}),",
                f"    .in_payload({up}_payload), "
                f".in_valid({up}_valid), .in_ready({up}_ready),",
                f"    .out_payload({down}_payload), "
                f".out_valid({down}_valid), .out_ready({down}_ready)"
                + ("," if count_projection is not None else ");"),
            ]
            if count_projection is not None:
                declarations.append(
                    f"  logic {packed_range(count_projection.width)}"
                    f"{count_projection.signal};"
                )
                instance_lines.append(
                    f"    .formal_count({count_projection.signal}));"
                )
            logic.extend(instance_lines)
        else:
            base = f"zlang_conn_{index}"
            if rr_info is None:
                source_external = _external_protocol_signal(
                    module, connection.source, "payload", identifier,
                )
                destination_external = _external_protocol_signal(
                    module, connection.destination, "payload", identifier,
                )
                shared = source_external or destination_external
                if shared is None:
                    declarations.extend((
                        f"  logic {packed_range(width)}{base}_payload;",
                        f"  logic {base}_valid;",
                        f"  logic {base}_ready;",
                    ))
                for signal in ("payload", "valid", "ready"):
                    value = (
                        _external_protocol_signal(
                            module, connection.source, signal, identifier,
                        )
                        or _external_protocol_signal(
                            module, connection.destination, signal, identifier,
                        )
                        or f"{base}_{signal}"
                    )
                    for key in _connection_pair_keys(module, connection, signal):
                        connection_signals[key] = value
            else:
                descriptor, channel = rr_info
                tracker, maximum = rr_tracker_names[id(descriptor)]
                count_width = max(1, maximum.bit_length())
                active = f"!({sv_sequential.reset_asserted(module, identifier)})"
                allow = (
                    f"({active} && {tracker} < {count_width}'d{maximum})"
                    if channel is ir_interfaces.RequestResponseChannel.REQUEST
                    else f"({active} && ({tracker} != '0 || "
                    f"{tracker}_request_transfer))"
                )
                src = f"{base}_src"
                dst = f"{base}_dst"
                declarations.extend((
                    f"  logic {packed_range(width)}{src}_payload;",
                    f"  logic {src}_valid, {src}_ready;",
                    f"  logic {packed_range(width)}{dst}_payload;",
                    f"  logic {dst}_valid, {dst}_ready;",
                ))
                # Both peers must observe the same admitted transfer.  Gating
                # only source-side ready lets the destination consume an
                # over-limit request (or a response from an old epoch) that
                # the source did not transfer.  Payload remains combinational;
                # only physical valid/ready are qualified by the typed ledger.
                logic.extend((
                    f"  assign {dst}_payload = {src}_payload;",
                    f"  assign {dst}_valid = {src}_valid && {allow};",
                    f"  assign {src}_ready = {dst}_ready && {allow};",
                ))
                for signal in ("payload", "valid"):
                    connection_signals[
                        _connection_key(
                            module,
                            connection.source,
                            signal,
                        )
                    ] = f"{src}_{signal}"
                    connection_signals[
                        _connection_key(
                            module,
                            connection.destination,
                            signal,
                        )
                    ] = f"{dst}_{signal}"
                connection_signals[
                    _connection_key(
                        module,
                        connection.source,
                        "ready",
                    )
                ] = f"{src}_ready"
                connection_signals[
                    _connection_key(
                        module,
                        connection.destination,
                        "ready",
                    )
                ] = f"{dst}_ready"
                rr_transfer_signals[id(connection)] = (
                    f"{dst}_valid",
                    f"{dst}_ready",
                )

    for descriptor in module.request_response_connections:
        request_valid, request_ready = rr_transfer_signals.get(
            id(descriptor.request), ("1'b0", "1'b0")
        )
        response_valid, response_ready = rr_transfer_signals.get(
            id(descriptor.response), ("1'b0", "1'b0")
        )
        tracker, maximum = rr_tracker_names[id(descriptor)]
        count_width = max(1, maximum.bit_length())
        request_transfer = f"{tracker}_request_transfer"
        response_transfer = f"{tracker}_response_transfer"
        declarations.append(
            f"  logic {request_transfer}, {response_transfer};"
        )
        logic.extend((
            f"  assign {request_transfer} = {request_valid} && {request_ready};",
            f"  assign {response_transfer} = {response_valid} && {response_ready};",
            f"  always_ff @({sv_sequential.clock_event(module, identifier)}) begin",
            f"    if ({sv_sequential.reset_asserted(module, identifier)}) {tracker} <= '0;",
            "    else begin",
            f"      case ({{{request_transfer}, {response_transfer}}})",
            f"        2'b10: if ({tracker} < {count_width}'d{maximum}) "
            f"{tracker} <= {tracker} + 1'b1;",
            f"        2'b01: if ({tracker} != '0) {tracker} <= {tracker} - 1'b1;",
            f"        default: {tracker} <= {tracker};",
            "      endcase",
            "    end",
            "  end",
        ))



def _emit_composed_component(
    module: ir_module.Module,
    component_name_: str,
    child_names: dict[str, str],
    rendering: ComposedRendering,
) -> str:
    local_names = emission_context.cached_module_rtl_names(module)
    identifier = sv_rendering._identifier
    packed_width = sv_rendering._width
    packed_range = sv_rendering._range
    ports = sv_boundary._physical_port_declarations(module)
    declarations: list[str] = []
    logic: list[str] = []
    connection_signals: dict[tuple[str, str, str], str] = {}
    fifo_helpers: list[tuple[str, int, int, bool, bool]] = []

    # Aggregate scalar members are ordinary typed wire ports after semantic
    # expansion, but they retain ownership in the aggregate schema.  Validate
    # their physical driver explicitly so a missing reverse member never turns
    # into an undriven RTL output (and a connection plus assignment never
    # becomes a multiple driver).
    aggregate_wire_outputs = {
        f"{aggregate.name}__{member.name}"
        for aggregate in module.aggregate_protocol_endpoints
        for member in aggregate.members
        if member.protocol is ir_interfaces.InterfaceProtocol.WIRE
        and any(
            port.name == f"{aggregate.name}__{member.name}"
            and port.direction is ir_module.PortDirection.OUTPUT
            for port in module.ports
        )
    }
    assignment_drivers = {
        sv_rendering._assignment_name(assignment) for assignment in module.assignments
    }
    connection_drivers = {
        endpoint.name
        for connection in module.hierarchical_connections
        for endpoint in (connection.source, connection.destination)
        if endpoint.owner == module.name
        and endpoint.protocol is ir_interfaces.InterfaceProtocol.WIRE
    }
    for connection in module.aggregate_protocol_connections:
        if not connection.delegation:
            continue
        aggregate = next(
            (
                item
                for item in module.aggregate_protocol_endpoints
                if item.name == connection.source
            ),
            None,
        )
        if aggregate is not None:
            connection_drivers.update(
                f"{aggregate.name}__{member.name}"
                for member in aggregate.members
                if member.protocol is ir_interfaces.InterfaceProtocol.WIRE
            )
    for name in sorted(aggregate_wire_outputs):
        driver_count = int(name in assignment_drivers) + int(
            name in connection_drivers
        )
        if driver_count == 0:
            raise SystemVerilogEmissionError(
                f"aggregate scalar output '{name}' has no driver"
            )
        if driver_count > 1:
            raise SystemVerilogEmissionError(
                f"aggregate scalar output '{name}' has multiple drivers"
            )

    stage_declarations, stage_logic, stage_render = (
        sv_materialized._embedded_staging_emission(module)
    )
    if stage_declarations:
        # A composed scalar child keeps its own physical pipeline registers.
        # Do not flatten or re-expand the staged expression in the parent.
        declarations.extend(stage_declarations)
        logic.extend(stage_logic)
        render = stage_render
    else:
        materialized_declarations, materialized_assignments, render = (
            sv_materialized._materialized_emission(module)
        )
        declarations.extend(materialized_declarations)
        logic.extend(materialized_assignments)
    rom_declarations, rom_lines = sv_storage._emit_rom_logic(module, render)
    declarations.extend(rom_declarations)
    logic.extend(rom_lines)
    has_unified_state = sv_state_planning.requires_unified_state(module)
    if has_unified_state:
        sv_state._append_unified_state(module, declarations, logic, render)
    rr_edges = {
        id(edge): (descriptor, channel)
        for descriptor in module.request_response_connections
        for channel, edge in (
            (ir_interfaces.RequestResponseChannel.REQUEST, descriptor.request),
            (ir_interfaces.RequestResponseChannel.RESPONSE, descriptor.response),
        )
    }
    rr_tracker_names: dict[int, tuple[str, int]] = {}
    formal_count_by_edge: dict[int, FormalBufferCountProjection] = {}
    for descriptor in module.request_response_connections:
        if descriptor.ordering.value != "in_order" or descriptor.max_outstanding <= 0:
            raise SystemVerilogEmissionError(
                "direct hierarchical request/response requires positive "
                "in_order max_outstanding"
            )
        tracker = request_response_tracker_name(descriptor, module)
        rr_tracker_names[id(descriptor)] = (tracker, descriptor.max_outstanding)
        count_width = max(1, descriptor.max_outstanding.bit_length())
        declarations.append(f"  logic [{count_width - 1}:0] {tracker};")
        for channel, edge, depth in (
            (
                ir_interfaces.RequestResponseChannel.REQUEST,
                descriptor.request,
                descriptor.request.request_buffer_depth,
            ),
            (
                ir_interfaces.RequestResponseChannel.RESPONSE,
                descriptor.response,
                descriptor.response.response_buffer_depth,
            ),
        ):
            matches = tuple(
                item for item in rendering.formal_buffer_counts
                if item.request_response_semantic_id == descriptor.semantic_id
                and item.channel is channel
            )
            if len(matches) > 1:
                raise SystemVerilogEmissionError(
                    "duplicate formal directional-buffer count projection for "
                    f"'{descriptor.semantic_id}:{channel.value}'"
                )
            if not matches:
                continue
            projection = matches[0]
            expected_width = max(1, depth.bit_length()) if depth else 0
            if depth <= 0 or projection.depth != depth:
                raise SystemVerilogEmissionError(
                    "formal directional-buffer count projection does not match "
                    f"the typed {channel.value} buffer depth"
                )
            if projection.width != expected_width:
                raise SystemVerilogEmissionError(
                    "formal directional-buffer count projection has width "
                    f"{projection.width}, expected {expected_width}"
                )
            if identifier(projection.signal) != projection.signal:
                raise SystemVerilogEmissionError(
                    "formal directional-buffer count projection has an invalid "
                    "physical signal"
                )
            formal_count_by_edge[id(edge)] = projection

    _append_composed_connection_logic(
        module, declarations, logic, connection_signals, fifo_helpers,
        rr_edges, rr_tracker_names, formal_count_by_edge,
    )

    # Top-level aggregate delegation connects the child's flattened leaves
    # directly to the already-published top ABI.
    aggregates = {
        item.name: item for item in module.aggregate_protocol_endpoints
    }
    children_by_instance = {
        item.instance.name: module.children[index]
        for index, item in enumerate(module.elaborated_instances)
    }
    for connection in module.aggregate_protocol_connections:
        if not connection.delegation or "." not in connection.destination:
            continue
        top_name = connection.source
        child_owner, child_name = connection.destination.split(".", 1)
        top = aggregates[top_name]
        child = next(
            item
            for item in children_by_instance[
                child_owner
            ].aggregate_protocol_endpoints
            if item.name == child_name
        )
        for member in top.members:
            top_port = identifier(f"{top_name}__{member.name}")
            child_port = (
                f"{identifiers.rtl_identifier(child_name)}__"
                f"{identifiers.rtl_identifier(member.name)}"
            )
            if member.protocol is ir_interfaces.InterfaceProtocol.WIRE:
                connection_signals[(child_owner, child_port, "wire")] = top_port
            else:
                for signal in ("payload", "valid", "ready"):
                    connection_signals[
                        (child_owner, child_port, signal)
                    ] = f"{top_port}_{signal}"

    bindings = {
        (item.instance, item.port): item.expression
        for item in module.instance_bindings
        if ir_interfaces.parse_ready_valid_field_name(item.port) is None
    }
    protocol_bindings = {
        (item.instance, item.port): item.expression
        for item in module.instance_bindings
        if ir_interfaces.parse_ready_valid_field_name(item.port) is not None
    }
    if len(bindings) + len(protocol_bindings) != len(module.instance_bindings):
        raise SystemVerilogEmissionError(
            "composed child bindings have duplicate targets"
        )
    referenced_protocol_outputs = set()
    for root in materialization.module_expression_roots(module):
        for value in walk_expression(root):
            if not isinstance(value, ir_expr.InstanceOutputRef):
                continue
            projection = ir_interfaces.parse_ready_valid_field_name(value.port)
            if projection is not None:
                referenced_protocol_outputs.add(
                    (value.instance, projection[0], projection[1].value)
                )
    for index, elaborated in enumerate(module.elaborated_instances):
        child = module.children[index]
        owner = elaborated.instance.name
        instance_name = local_names.instance(owner)
        connections: list[str] = []
        if len(child.clock_domains) == 1:
            child_domain = child.clock_domains[0]
            if elaborated.clock is None or elaborated.reset is None:
                raise SystemVerilogEmissionError(
                    f"sequential child '{owner}' has no resolved parent domain"
                )
            parent_domain = next(
                (
                    item for item in module.clock_domains
                    if item.clock == elaborated.clock
                    and item.reset == elaborated.reset
                ),
                None,
            )
            if parent_domain is None:
                raise SystemVerilogEmissionError(
                    f"sequential child '{owner}' has no exact parent clock/reset contract"
                )
            connections.append(
                f".{identifiers.rtl_identifier(child_domain.clock)}"
                f"({identifier(parent_domain.clock)})"
            )
            connections.append(
                f".{identifiers.rtl_identifier(child_domain.reset)}"
                f"({sv_sequential.effective_reset_signal(module, identifier, parent_domain.clock)})"
            )
        elif child.clock_domains:
            parent_domains = {item.clock: item for item in module.clock_domains}
            for child_domain in child.clock_domains:
                parent_domain = parent_domains.get(child_domain.clock)
                if parent_domain != child_domain:
                    raise SystemVerilogEmissionError(
                        f"sequential child '{owner}' domain '{child_domain.clock}' "
                        "has no exact parent clock/reset contract"
                    )
                connections.append(
                    f".{identifiers.rtl_identifier(child_domain.clock)}"
                    f"({identifier(parent_domain.clock)})"
                )
                connections.append(
                    f".{identifiers.rtl_identifier(child_domain.reset)}"
                    f"({sv_sequential.effective_reset_signal(module, identifier, parent_domain.clock)})"
                )
        for port in child.ports:
            port_name = identifiers.rtl_identifier(port.name)
            if port.protocol is ir_interfaces.InterfaceProtocol.WIRE:
                delegated = connection_signals.get((owner, port_name, "wire"))
                if delegated is not None:
                    signal = delegated
                elif port.direction is ir_module.PortDirection.INPUT:
                    expression = bindings.get((owner, port.name))
                    if expression is None:
                        raise SystemVerilogEmissionError(
                            f"child '{owner}' input '{port.name}' is unbound"
                        )
                    signal = render(expression)
                else:
                    signal = local_names.child_signal(owner, port.name)
                    declarations.append(
                        f"  logic {packed_range(packed_width(port.type))}{signal};"
                    )
                connections.append(f".{port_name}({signal})")
                continue
            if port.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID:
                raise SystemVerilogEmissionError(
                    f"unsupported hierarchical protocol {port.protocol.value}"
                )
            for signal_name in ("payload", "valid", "ready"):
                signal = connection_signals.get(
                    (owner, port_name, signal_name)
                )
                scalar_name = ir_interfaces.ready_valid_field_name(port.name, signal_name)
                bound = protocol_bindings.get((owner, scalar_name))
                if signal is not None and bound is not None:
                    raise SystemVerilogEmissionError(
                        f"child protocol signal '{owner}.{port.name}."
                        f"{signal_name}' has multiple drivers"
                    )
                child_output = (
                    signal_name in {"payload", "valid"}
                    if port.direction is ir_module.PortDirection.OUTPUT
                    else signal_name == "ready"
                )
                if signal is None and bound is not None:
                    if child_output:
                        raise SystemVerilogEmissionError(
                            f"child-owned signal '{owner}.{port.name}."
                            f"{signal_name}' cannot be bound"
                        )
                    signal = render(bound)
                if signal is None and child_output:
                    signal = local_names.child_signal(owner, port.name, signal_name)
                    width = (
                        packed_width(port.type)
                        if signal_name == "payload" else 1
                    )
                    declarations.append(
                        f"  logic {packed_range(width)}{signal};"
                    )
                elif (
                    child_output
                    and (owner, port.name, signal_name)
                    in referenced_protocol_outputs
                ):
                    alias = local_names.child_signal(
                        owner, port.name, signal_name
                    )
                    if alias != signal:
                        width = (
                            packed_width(port.type)
                            if signal_name == "payload" else 1
                        )
                        declarations.append(
                            f"  logic {packed_range(width)}{alias};"
                        )
                        logic.append(f"  assign {alias} = {signal};")
                if signal is None:
                    raise SystemVerilogEmissionError(
                        f"child protocol signal "
                        f"'{owner}.{port.name}.{signal_name}' is unconnected"
                    )
                connections.append(f".{port_name}_{signal_name}({signal})")
        for interface in child.request_responses:
            base = identifiers.rtl_identifier(interface.name)
            for channel in (
                ir_interfaces.RequestResponseChannel.REQUEST,
                ir_interfaces.RequestResponseChannel.RESPONSE,
            ):
                for signal_name in ("payload", "valid", "ready"):
                    signal = connection_signals.get(
                        (owner, f"{base}_{channel.value}", signal_name)
                    )
                    if signal is None:
                        raise SystemVerilogEmissionError(
                            f"child request/response signal "
                            f"'{owner}.{base}.{channel.value}.{signal_name}' "
                            "is unconnected"
                        )
                    connections.append(
                        f".{base}_{channel.value}_{signal_name}({signal})"
                    )
        logic.append(
            f"  {child_names[owner]} {instance_name} (\n    "
            + ",\n    ".join(connections)
            + "\n  );"
        )

    if not has_unified_state:
        sv_rules._append_rule_state(
            module, declarations, logic, render, compact_reset=True
        )
    body = sv_module_rendering._named_module(
        component_name_,
        ports,
        declarations + logic,
        typed_module=module,
    )
    helpers = "\n".join(
        rv_fifo_helper(
            name, width, depth, module, expose_count=expose_count,
            allow_full_replace=allow_full_replace,
        )
        for name, width, depth, expose_count, allow_full_replace
        in dict.fromkeys(fifo_helpers)
    )
    return helpers + ("\n" if helpers else "") + body


def rv_fifo_helper(
    name: str,
    width: int,
    depth: int,
    module: ir_module.Module | None = None,
    *,
    expose_count: bool = False,
    allow_full_replace: bool = True,
) -> str:
    count_width = max(1, depth.bit_length())
    ptr_width = max(1, (depth - 1).bit_length())
    def helper_identifier(value: str) -> str:
        if module is None:
            return value
        return "clk" if value == module.clock else "rst"

    helper_module = (
        sv_sequential.native_release_module(module) if module is not None else None
    )
    reset_deasserted_expression = (
        f"!({sv_sequential.reset_asserted(helper_module, helper_identifier)})"
        if helper_module is not None
        else "!rst"
    )

    return "\n".join((
        f"module {name}(",
        "  input logic clk, input logic rst,",
        f"  input logic [{width - 1}:0] in_payload, "
        "input logic in_valid, output logic in_ready,",
        f"  output logic [{width - 1}:0] out_payload, "
        "output logic out_valid, input logic out_ready"
        + ("," if expose_count else ");"),
        *(
            (f"  output logic [{count_width - 1}:0] formal_count);",)
            if expose_count else ()
        ),
        f"  logic [{width - 1}:0] storage [0:{depth - 1}];",
        f"  logic [{count_width - 1}:0] count;",
        f"  logic [{ptr_width - 1}:0] rd, wr;",
        "  logic push, pop;",
        "  assign push = in_valid && in_ready;",
        "  assign pop = out_valid && out_ready;",
        (
            f"  assign in_ready = {reset_deasserted_expression} && "
            f"((count < {count_width}'d{depth}) || "
            "(out_valid && out_ready));"
            if allow_full_replace else
            f"  assign in_ready = {reset_deasserted_expression} && "
            f"(count < {count_width}'d{depth});"
        ),
        f"  assign out_valid = {reset_deasserted_expression} && (count != '0);",
        "  assign out_payload = storage[rd];",
        *(("  assign formal_count = count;",) if expose_count else ()),
        (
            f"  always_ff @({sv_sequential.clock_event(helper_module, helper_identifier)}) begin"
            if helper_module is not None
            else "  always_ff @(posedge clk) begin"
        ),
        (
            f"    if ({sv_sequential.reset_asserted(helper_module, helper_identifier)}) "
            "begin count <= '0; rd <= '0; wr <= '0; end"
            if helper_module is not None
            else "    if (rst) begin count <= '0; rd <= '0; wr <= '0; end"
        ),
        "    else begin",
        "      if (push) begin storage[wr] <= in_payload; "
        f"wr <= (wr == {ptr_width}'d{depth - 1}) ? '0 : wr + 1'b1; end",
        f"      if (pop) rd <= (rd == {ptr_width}'d{depth - 1}) "
        "? '0 : rd + 1'b1;",
        "      case ({push,pop})",
        "        2'b10: count <= count + 1'b1;",
        "        2'b01: count <= count - 1'b1;",
        "        default: count <= count;",
        "      endcase",
        "    end",
        "  end",
        "endmodule",
        "",
    ))
