"""Backend-local direct-SystemVerilog lowering for composed hierarchy.

The subsystem consumes already-typed hierarchy, endpoint, connection, and state
IR.  It deliberately knows nothing about source syntax or Clash.  Small renderer
callbacks keep shared low-level SV spelling and the existing state/storage
emitters authoritative without introducing an emitter import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from zlang.ir.expressions import Expression
from zlang.ir.hierarchy import HierarchyIndex
from zlang.ir.interfaces import (
    InterfaceProtocol,
    RequestResponseChannel,
)
from zlang.ir.module import (
    Assignment,
    Module,
    PortDirection,
    ProtocolEndpoint,
    RequestResponseConnection,
    Rule,
)
from zlang.ir.types import HardwareType
from zlang.backend.naming import (
    ComponentNamePlan,
    build_component_name_plan,
    module_rtl_names,
)
from zlang.backend.systemverilog.sequential import (
    clock_event,
    effective_reset_signal,
    native_release_module,
    reset_asserted,
)


ExpressionRenderer = Callable[[Expression], str]
ExpressionEmission = tuple[list[str], list[str], ExpressionRenderer]


@dataclass(frozen=True)
class FormalBufferCountProjection:
    """One typed formal-only request for a directional FIFO count output.

    The request/response semantic identity plus channel identifies the exact
    ``HierarchicalConnection``.  ``signal`` is an explicitly allocated local
    ABI wire, not a name recovered from generated RTL.
    """

    request_response_semantic_id: str
    channel: RequestResponseChannel
    signal: str
    width: int
    depth: int


@dataclass(frozen=True)
class SVPhysicalSyntax:
    """Physical SV spelling and typed-hierarchy validation capability."""

    error: Callable[[str], Exception]
    identifier: Callable[[str], str]
    instance_identifier: Callable[[str], str]
    packed_width: Callable[[HardwareType], int]
    packed_range: Callable[[int], str]
    logic_declaration: Callable[[str, HardwareType], str]
    physical_ports: Callable[[Module], list[str]]
    validate_hierarchy: Callable[[Module], HierarchyIndex]


@dataclass(frozen=True)
class ComposedLeafServices:
    """Existing leaf/body emitters reused by composed physical hierarchy."""

    materialized_emission: Callable[[Module], ExpressionEmission]
    staging_emission: Callable[[Module], ExpressionEmission]
    rom_logic: Callable[
        [Module, ExpressionRenderer], tuple[list[str], list[str]]
    ]
    append_unified_state: Callable[
        [Module, list[str], list[str], ExpressionRenderer], None
    ]
    append_rule_state: Callable[
        [Module, list[str], list[str], ExpressionRenderer], None
    ]
    emit_fifo: Callable[[Module], str]
    emit_memory: Callable[[Module], str]
    emit_rom: Callable[[Module], str]
    emit_unified_state: Callable[[Module], str]
    emit_rules: Callable[[Module], str]
    emit_elastic_pipeline: Callable[[Module], str]
    emit_csr_child: Callable[[Module], str]
    named_module: Callable[[str, list[str], list[str], Module], str]
    requires_unified_state: Callable[[Module], bool]
    ordered_rules: Callable[[Module], tuple[Rule, ...]]
    assignment_name: Callable[[Assignment], str]
    emit_external: Callable[[Module, str], str]


@dataclass(frozen=True)
class ComposedRendering:
    """Two immutable capabilities required by composed hierarchy emission."""

    physical: SVPhysicalSyntax
    services: ComposedLeafServices
    formal_buffer_counts: tuple[FormalBufferCountProjection, ...] = ()
    component_names: ComponentNamePlan | None = None


def component_name(
    module: Module,
    specialization: str | None,
    rendering: ComposedRendering,
    *,
    top: bool = False,
) -> str:
    """Resolve a physical definition from the complete typed catalog."""

    if top:
        return rendering.physical.identifier(module.name)
    if rendering.component_names is None or specialization is None:
        raise rendering.physical.error(
            "physical component naming requires a validated specialization catalog"
        )
    return rendering.component_names.component(module.name, specialization)


def request_response_tracker_name(
    descriptor: RequestResponseConnection,
    rendering: ComposedRendering,
    module: Module | None = None,
) -> str:
    """Return the ledger token shared by production and formal projection."""

    instance_identifier = (
        module_rtl_names(module).instance if module is not None
        else rendering.physical.instance_identifier
    )
    request_owner = instance_identifier(
        descriptor.request.source.owner
    )
    response_owner = instance_identifier(
        descriptor.response.destination.owner
    )
    return rendering.physical.identifier(
        f"rr_{request_owner}_"
        f"{rendering.physical.identifier(descriptor.request.source.name)}_"
        f"{response_owner}_outstanding"
    )


def emit_composed_design(module: Module, rendering: ComposedRendering) -> str:
    """Emit every specialization once and every physical instance once."""

    physical = rendering.physical
    services = rendering.services
    hierarchy = physical.validate_hierarchy(module)
    rendering = replace(
        rendering,
        component_names=build_component_name_plan(
            hierarchy, identifier=physical.identifier,
        ),
    )
    emitted: dict[tuple[str, str], str] = {}
    definitions: list[str] = []

    def visit(current: Module, name: str, *, root: bool = False) -> None:
        # Only the selected root owns the physical reset conditioner.  Every
        # reusable child specialization receives the already-conditioned reset
        # through its ordinary reset port and therefore uses a native internal
        # release ABI.
        emitted_current = current if root else native_release_module(current)
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
            definitions.append(services.emit_external(emitted_current, name))
        elif (
            emitted_current.elastic_pipeline_regions
            and not emitted_current.elaborated_instances
        ):
            definitions.append(
                services.emit_elastic_pipeline(
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
                services.emit_fifo(replace(emitted_current, name=name))
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
                services.emit_memory(replace(emitted_current, name=name))
            )
        elif (
            emitted_current.roms
            and not emitted_current.elaborated_instances
            and not emitted_current.registers
            and not emitted_current.rules
        ):
            definitions.append(
                services.emit_rom(replace(emitted_current, name=name))
            )
        elif (
            closed_state_component
            and services.requires_unified_state(emitted_current)
        ):
            definitions.append(
                services.emit_unified_state(
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
                port.protocol is InterfaceProtocol.WIRE
                for port in emitted_current.ports
            )
        ):
            definitions.append(
                services.emit_rules(replace(emitted_current, name=name))
            )
        elif (
            emitted_current.csr_blocks
            and not emitted_current.elaborated_instances
        ):
            # A CSR bank is an ordinary closed child specialization.  Its
            # behavior still comes from typed CSR IR; the composed parent only
            # wires the canonical scalar access ABI.
            definitions.append(
                services.emit_csr_child(
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
    endpoint: ProtocolEndpoint,
    rendering: ComposedRendering,
) -> str:
    base = rendering.physical.identifier(endpoint.name)
    if endpoint.channel is not None:
        base += f"_{endpoint.channel.value}"
    return base


def _connection_key(
    owner: str,
    endpoint: ProtocolEndpoint,
    signal: str,
    rendering: ComposedRendering,
) -> tuple[str, str, str]:
    return (owner, _endpoint_base(endpoint, rendering), signal)


def composed_component_identifier_claims(
    module: Module,
    rendering: ComposedRendering,
) -> tuple[tuple[str, str], ...]:
    """Return typed internal names emitted by one composed component.

    The namespace validator consumes this plan before rendering.  Keep the
    decision rules aligned with :func:`_emit_composed_component`: in
    particular, a scalar child output gets a private interconnect only when no
    typed hierarchy/delegation edge already supplies that connection.
    """

    physical = rendering.physical
    local_names = module_rtl_names(module)
    physical.validate_hierarchy(module)
    identifier = physical.identifier
    claims: list[tuple[str, str]] = []
    connection_keys: set[tuple[str, str, str]] = set()

    def claim(token: str, owner: str) -> None:
        claims.append((token, owner))

    def external_signal(endpoint: ProtocolEndpoint, signal: str) -> str | None:
        if endpoint.owner != module.name:
            return None
        base = identifier(endpoint.name)
        if endpoint.protocol is InterfaceProtocol.READY_VALID:
            return f"{base}_{signal}"
        if signal == "wire":
            return base
        return None

    rr_edges = {
        id(edge): (descriptor, channel)
        for descriptor in module.request_response_connections
        for channel, edge in (
            (RequestResponseChannel.REQUEST, descriptor.request),
            (RequestResponseChannel.RESPONSE, descriptor.response),
        )
    }
    for descriptor in module.request_response_connections:
        tracker = request_response_tracker_name(descriptor, rendering, module)
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
        if connection.source.protocol is InterfaceProtocol.WIRE:
            shared = (
                external_signal(connection.source, "wire")
                or external_signal(connection.destination, "wire")
            )
            if shared is None:
                claim(f"zlang_conn_{index}", f"scalar hierarchy connection {index}")
            connection_keys.add(
                _connection_key(
                    connection.source.owner, connection.source, "wire", rendering
                )
            )
            connection_keys.add(
                _connection_key(
                    connection.destination.owner,
                    connection.destination,
                    "wire",
                    rendering,
                )
            )
            continue

        depth = connection.buffer_depth
        if connection.source.channel is RequestResponseChannel.REQUEST:
            depth = connection.request_buffer_depth
        elif connection.source.channel is RequestResponseChannel.RESPONSE:
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
                connection_keys.add(
                    _connection_key(
                        connection.source.owner,
                        connection.source,
                        signal,
                        rendering,
                    )
                )
                connection_keys.add(
                    _connection_key(
                        connection.destination.owner,
                        connection.destination,
                        signal,
                        rendering,
                    )
                )
            continue

        if rr_info is None:
            shared = (
                external_signal(connection.source, "payload")
                or external_signal(connection.destination, "payload")
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
            connection_keys.add(
                _connection_key(
                    connection.source.owner,
                    connection.source,
                    signal,
                    rendering,
                )
            )
            connection_keys.add(
                _connection_key(
                    connection.destination.owner,
                    connection.destination,
                    signal,
                    rendering,
                )
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
            if member.protocol is InterfaceProtocol.WIRE:
                connection_keys.add(
                    (child_owner, f"{identifier(child.name)}__{identifier(member.name)}", "wire")
                )

    for index, elaborated in enumerate(module.elaborated_instances):
        child = module.children[index]
        owner = elaborated.instance.name
        for port in child.ports:
            port_name = identifier(port.name)
            if (
                port.protocol is InterfaceProtocol.WIRE
                and port.direction is PortDirection.OUTPUT
                and (owner, port_name, "wire") not in connection_keys
            ):
                claim(
                    local_names.child_signal(owner, port.name),
                    f"child '{owner}' scalar output '{port.name}'",
                )

    return tuple(claims)


def _emit_composed_component(
    module: Module,
    component_name_: str,
    child_names: dict[str, str],
    rendering: ComposedRendering,
) -> str:
    physical = rendering.physical
    local_names = module_rtl_names(module)
    services = rendering.services
    identifier = physical.identifier
    packed_width = physical.packed_width
    packed_range = physical.packed_range
    ports = physical.physical_ports(module)
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
        if member.protocol is InterfaceProtocol.WIRE
        and any(
            port.name == f"{aggregate.name}__{member.name}"
            and port.direction is PortDirection.OUTPUT
            for port in module.ports
        )
    }
    assignment_drivers = {
        services.assignment_name(assignment) for assignment in module.assignments
    }
    connection_drivers = {
        endpoint.name
        for connection in module.hierarchical_connections
        for endpoint in (connection.source, connection.destination)
        if endpoint.owner == module.name
        and endpoint.protocol is InterfaceProtocol.WIRE
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
                if member.protocol is InterfaceProtocol.WIRE
            )
    for name in sorted(aggregate_wire_outputs):
        driver_count = int(name in assignment_drivers) + int(
            name in connection_drivers
        )
        if driver_count == 0:
            raise physical.error(f"aggregate scalar output '{name}' has no driver")
        if driver_count > 1:
            raise physical.error(
                f"aggregate scalar output '{name}' has multiple drivers"
            )

    def external_signal(endpoint: ProtocolEndpoint, signal: str) -> str | None:
        """Return the already-declared top ABI leaf for a top endpoint."""
        if endpoint.owner != module.name:
            return None
        base = identifier(endpoint.name)
        if endpoint.protocol is InterfaceProtocol.READY_VALID:
            return f"{base}_{signal}"
        if signal == "wire":
            return base
        return None

    stage_declarations, stage_logic, stage_render = services.staging_emission(module)
    if stage_declarations:
        # A composed scalar child keeps its own physical pipeline registers.
        # Do not flatten or re-expand the staged expression in the parent.
        declarations.extend(stage_declarations)
        logic.extend(stage_logic)
        render = stage_render
    else:
        materialized_declarations, materialized_assignments, render = (
            services.materialized_emission(module)
        )
        declarations.extend(materialized_declarations)
        logic.extend(materialized_assignments)
    rom_declarations, rom_lines = services.rom_logic(module, render)
    declarations.extend(rom_declarations)
    logic.extend(rom_lines)
    has_unified_state = services.requires_unified_state(module)
    if has_unified_state:
        services.append_unified_state(module, declarations, logic, render)
    rr_edges = {
        id(edge): (descriptor, channel)
        for descriptor in module.request_response_connections
        for channel, edge in (
            (RequestResponseChannel.REQUEST, descriptor.request),
            (RequestResponseChannel.RESPONSE, descriptor.response),
        )
    }
    rr_transfer_signals: dict[int, tuple[str, str]] = {}
    rr_tracker_names: dict[int, tuple[str, int]] = {}
    formal_count_by_edge: dict[int, FormalBufferCountProjection] = {}
    for descriptor in module.request_response_connections:
        if descriptor.ordering.value != "in_order" or descriptor.max_outstanding <= 0:
            raise physical.error(
                "direct hierarchical request/response requires positive "
                "in_order max_outstanding"
            )
        tracker = request_response_tracker_name(descriptor, rendering, module)
        rr_tracker_names[id(descriptor)] = (tracker, descriptor.max_outstanding)
        count_width = max(1, descriptor.max_outstanding.bit_length())
        declarations.append(f"  logic [{count_width - 1}:0] {tracker};")
        for channel, edge, depth in (
            (
                RequestResponseChannel.REQUEST,
                descriptor.request,
                descriptor.request.request_buffer_depth,
            ),
            (
                RequestResponseChannel.RESPONSE,
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
                raise physical.error(
                    "duplicate formal directional-buffer count projection for "
                    f"'{descriptor.semantic_id}:{channel.value}'"
                )
            if not matches:
                continue
            projection = matches[0]
            expected_width = max(1, depth.bit_length()) if depth else 0
            if depth <= 0 or projection.depth != depth:
                raise physical.error(
                    "formal directional-buffer count projection does not match "
                    f"the typed {channel.value} buffer depth"
                )
            if projection.width != expected_width:
                raise physical.error(
                    "formal directional-buffer count projection has width "
                    f"{projection.width}, expected {expected_width}"
                )
            if identifier(projection.signal) != projection.signal:
                raise physical.error(
                    "formal directional-buffer count projection has an invalid "
                    "physical signal"
                )
            formal_count_by_edge[id(edge)] = projection

    for index, connection in enumerate(module.connections):
        if not connection.buffer_depth:
            continue
        if (
            connection.source.protocol is not InterfaceProtocol.READY_VALID
            or connection.destination.protocol is not InterfaceProtocol.READY_VALID
        ):
            raise physical.error(
                "composed buffering currently requires ready/valid endpoints"
            )
        if module.clock is None or module.reset is None:
            raise physical.error("buffered connection requires clock/reset")
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
            f".rst({effective_reset_signal(module, identifier)}),",
            f"    .in_payload({source}_payload), "
            f".in_valid({source}_valid), .in_ready({source}_ready),",
            f"    .out_payload({destination}_payload), "
            f".out_valid({destination}_valid), "
            f".out_ready({destination}_ready));",
        ))

    for index, connection in enumerate(module.hierarchical_connections):
        rr_info = rr_edges.get(id(connection))
        width = packed_width(connection.source.payload_type)
        if connection.source.protocol is InterfaceProtocol.WIRE:
            if connection.destination.protocol is not InterfaceProtocol.WIRE:
                raise physical.error(
                    "hierarchical scalar connection requires two wire endpoints"
                )
            if (
                connection.buffer_depth
                or connection.request_buffer_depth
                or connection.response_buffer_depth
                or connection.adapter is not None
                or connection.crossing is not None
            ):
                raise physical.error(
                    "hierarchical scalar connection does not accept protocol options"
                )
            base = f"zlang_conn_{index}"
            source_external = external_signal(connection.source, "wire")
            destination_external = external_signal(connection.destination, "wire")
            shared = source_external or destination_external
            if shared is None:
                shared = base
                declarations.append(f"  logic {packed_range(width)}{shared};")
            connection_signals[
                _connection_key(
                    connection.source.owner, connection.source, "wire", rendering
                )
            ] = shared
            connection_signals[
                _connection_key(
                    connection.destination.owner,
                    connection.destination,
                    "wire",
                    rendering,
                )
            ] = shared
            continue
        if (
            connection.source.protocol is not InterfaceProtocol.READY_VALID
            or connection.destination.protocol is not InterfaceProtocol.READY_VALID
        ):
            raise physical.error(
                "hierarchical protocol connection requires matching "
                "ready/valid endpoints"
            )
        depth = connection.buffer_depth
        if connection.source.channel is RequestResponseChannel.REQUEST:
            depth = connection.request_buffer_depth
        elif connection.source.channel is RequestResponseChannel.RESPONSE:
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
                active = f"!({reset_asserted(module, identifier)})"
                allow = (
                    f"({active} && {tracker} < {count_width}'d{maximum})"
                    if channel is RequestResponseChannel.REQUEST
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
                    _connection_key(
                        connection.source.owner, connection.source, signal, rendering
                    )
                ] = f"{up}_{signal}"
                connection_signals[
                    _connection_key(
                        connection.destination.owner,
                        connection.destination,
                        signal,
                        rendering,
                    )
                ] = destination_valid if signal == "valid" else f"{down}_{signal}"
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
                raise physical.error("buffered hierarchy requires clock/reset")
            instance_lines = [
                f"  {helper} zlang_conn_{index}_fifo (",
                f"    .clk({identifier(module.clock)}), "
                f".rst({effective_reset_signal(module, identifier)}),",
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
                source_external = external_signal(connection.source, "payload")
                destination_external = external_signal(
                    connection.destination, "payload"
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
                        external_signal(connection.source, signal)
                        or external_signal(connection.destination, signal)
                        or f"{base}_{signal}"
                    )
                    connection_signals[
                        _connection_key(
                            connection.source.owner,
                            connection.source,
                            signal,
                            rendering,
                        )
                    ] = value
                    connection_signals[
                        _connection_key(
                            connection.destination.owner,
                            connection.destination,
                            signal,
                            rendering,
                        )
                    ] = value
            else:
                descriptor, channel = rr_info
                tracker, maximum = rr_tracker_names[id(descriptor)]
                count_width = max(1, maximum.bit_length())
                active = f"!({reset_asserted(module, identifier)})"
                allow = (
                    f"({active} && {tracker} < {count_width}'d{maximum})"
                    if channel is RequestResponseChannel.REQUEST
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
                            connection.source.owner,
                            connection.source,
                            signal,
                            rendering,
                        )
                    ] = f"{src}_{signal}"
                    connection_signals[
                        _connection_key(
                            connection.destination.owner,
                            connection.destination,
                            signal,
                            rendering,
                        )
                    ] = f"{dst}_{signal}"
                connection_signals[
                    _connection_key(
                        connection.source.owner,
                        connection.source,
                        "ready",
                        rendering,
                    )
                ] = f"{src}_ready"
                connection_signals[
                    _connection_key(
                        connection.destination.owner,
                        connection.destination,
                        "ready",
                        rendering,
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
            f"  always_ff @({clock_event(module, identifier)}) begin",
            f"    if ({reset_asserted(module, identifier)}) {tracker} <= '0;",
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
            top_port = f"{identifier(top_name)}__{identifier(member.name)}"
            child_port = f"{identifier(child_name)}__{identifier(member.name)}"
            if member.protocol is InterfaceProtocol.WIRE:
                connection_signals[(child_owner, child_port, "wire")] = top_port
            else:
                for signal in ("payload", "valid", "ready"):
                    connection_signals[
                        (child_owner, child_port, signal)
                    ] = f"{top_port}_{signal}"

    bindings = {
        (item.instance, item.port): item.expression
        for item in module.instance_bindings
    }
    for index, elaborated in enumerate(module.elaborated_instances):
        child = module.children[index]
        owner = elaborated.instance.name
        instance_name = local_names.instance(owner)
        connections: list[str] = []
        if child.clock is not None:
            connections.append(
                f".{identifier(child.clock)}"
                f"({identifier(module.clock or child.clock)})"
            )
        if child.reset is not None:
            connections.append(
                f".{identifier(child.reset)}"
                f"({effective_reset_signal(module, identifier)})"
            )
        for port in child.ports:
            port_name = identifier(port.name)
            if port.protocol is InterfaceProtocol.WIRE:
                delegated = connection_signals.get((owner, port_name, "wire"))
                if delegated is not None:
                    signal = delegated
                elif port.direction is PortDirection.INPUT:
                    expression = bindings.get((owner, port.name))
                    if expression is None:
                        raise physical.error(
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
            if port.protocol is not InterfaceProtocol.READY_VALID:
                raise physical.error(
                    f"unsupported hierarchical protocol {port.protocol.value}"
                )
            for signal_name in ("payload", "valid", "ready"):
                signal = connection_signals.get(
                    (owner, port_name, signal_name)
                )
                if signal is None:
                    raise physical.error(
                        f"child protocol signal "
                        f"'{owner}.{port.name}.{signal_name}' is unconnected"
                    )
                connections.append(f".{port_name}_{signal_name}({signal})")
        for interface in child.request_responses:
            base = identifier(interface.name)
            for channel in (
                RequestResponseChannel.REQUEST,
                RequestResponseChannel.RESPONSE,
            ):
                for signal_name in ("payload", "valid", "ready"):
                    signal = connection_signals.get(
                        (owner, f"{base}_{channel.value}", signal_name)
                    )
                    if signal is None:
                        raise physical.error(
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
        services.append_rule_state(module, declarations, logic, render)
    body = services.named_module(
        component_name_,
        ports,
        declarations + logic,
        module,
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
    module: Module | None = None,
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
        native_release_module(module) if module is not None else None
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
            f"  assign in_ready = (count < {count_width}'d{depth}) || "
            "(out_valid && out_ready);"
            if allow_full_replace else
            f"  assign in_ready = count < {count_width}'d{depth};"
        ),
        "  assign out_valid = count != '0;",
        "  assign out_payload = storage[rd];",
        *(("  assign formal_count = count;",) if expose_count else ()),
        (
            f"  always_ff @({clock_event(helper_module, helper_identifier)}) begin"
            if helper_module is not None
            else "  always_ff @(posedge clk) begin"
        ),
        (
            f"    if ({reset_asserted(helper_module, helper_identifier)}) "
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
