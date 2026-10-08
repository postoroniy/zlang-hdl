"""Formal-only observation projection for direct SystemVerilog."""

from __future__ import annotations

from dataclasses import replace
import hashlib

from zlang.backend import identifiers, naming
from zlang.backend.systemverilog import composed
from zlang.backend.systemverilog import physicalization as sv_physicalization
from zlang.backend.systemverilog import protocols as sv_protocols
from zlang.backend.systemverilog import request_response as sv_request_response
from zlang.backend.systemverilog import rendering as sv_rendering
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.ir import csr as ir_csr
from zlang.ir import expressions as expr
from zlang.ir import formal_observations
from zlang.ir import hierarchy as ir_hierarchy
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import state as ir_state
from zlang.ir import storage as ir_storage
from zlang.ir import types as ir_types
from zlang.ir.cdc import ResetPolarity


def validate_recursive_hierarchy(
    module: ir_module.Module,
    recursive_design: object,
) -> tuple[ir_hierarchy.HierarchyIndex, dict[str, object]]:
    """Bind recursive metadata only to the exact validated typed hierarchy."""

    hierarchy = sv_physicalization.validated_hierarchy(module)
    instances = tuple(recursive_design.instances)
    nodes = {item.instance_identity: item for item in instances}
    if len(nodes) != len(instances):
        raise SystemVerilogEmissionError(
            "recursive formal design contains duplicate instance identities"
        )
    root = nodes.get(recursive_design.root_instance_identity)
    if root is None:
        raise SystemVerilogEmissionError(
            "recursive formal design root instance is absent"
        )
    if tuple(root.physical_instance_path) != (module.name,):
        raise SystemVerilogEmissionError(
            "recursive formal root path does not match typed top module"
        )

    node_paths: set[tuple[str, ...]] = set()
    for node in instances:
        path = tuple(node.physical_instance_path)
        if path in node_paths:
            raise SystemVerilogEmissionError(
                "recursive formal design contains duplicate physical path: "
                + ".".join(path)
            )
        node_paths.add(path)
        try:
            entry = hierarchy.at(path)
        except ir_hierarchy.HierarchyError as error:
            raise SystemVerilogEmissionError(str(error)) from error
        if entry.module.name != node.module_name:
            raise SystemVerilogEmissionError(
                f"recursive formal path '{'.'.join(path)}' names module "
                f"'{node.module_name}', not typed module '{entry.module.name}'"
            )
        if entry.elaborated is not None:
            if node.source_instance_name != path[-1]:
                raise SystemVerilogEmissionError(
                    f"recursive formal path '{'.'.join(path)}' has source instance "
                    f"'{node.source_instance_name}'"
                )
            if node.specialization_identity != entry.specialization_identity:
                raise SystemVerilogEmissionError(
                    f"recursive formal path '{'.'.join(path)}' specialization "
                    "does not match typed elaboration"
                )
    hierarchy_paths = {item.physical_path for item in hierarchy.entries}
    if node_paths != hierarchy_paths:
        missing = sorted(".".join(path) for path in hierarchy_paths - node_paths)
        extra = sorted(".".join(path) for path in node_paths - hierarchy_paths)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise SystemVerilogEmissionError(
            "recursive formal instance tree does not match typed hierarchy: "
            + "; ".join(details)
        )

    for binding in recursive_design.bindings:
        node = nodes.get(binding.ref.instance_identity)
        if node is None:
            raise SystemVerilogEmissionError(
                f"recursive binding '{binding.semantic_binding_id}' references "
                "an unknown instance identity"
            )
        if tuple(binding.physical_instance_path) != tuple(node.physical_instance_path):
            raise SystemVerilogEmissionError(
                f"recursive binding '{binding.semantic_binding_id}' physical path "
                "does not match its instance"
            )
        if binding.specialization_identity != node.specialization_identity:
            raise SystemVerilogEmissionError(
                f"recursive binding '{binding.semantic_binding_id}' specialization "
                "does not match its instance"
            )
    return hierarchy, nodes

def _formal_buffer_count_projection(
    module: ir_module.Module,
    semantic_id: str,
) -> composed.FormalBufferCountProjection | None:
    """Resolve one typed buffered RR occupancy to its formal-only FIFO ABI."""

    for descriptor in module.request_response_connections:
        for channel, signal, edge, depth in (
            (
                ir_interfaces.RequestResponseChannel.REQUEST,
                formal_observations.RequestResponseObservationSignal.REQUEST_OCCUPANCY,
                descriptor.request,
                descriptor.request.request_buffer_depth,
            ),
            (
                ir_interfaces.RequestResponseChannel.RESPONSE,
                formal_observations.RequestResponseObservationSignal.RESPONSE_OCCUPANCY,
                descriptor.response,
                descriptor.response.response_buffer_depth,
            ),
        ):
            if depth <= 0 or semantic_id != formal_observations.request_response_observation_id(
                descriptor.semantic_id, signal
            ):
                continue
            # Validate the semantic channel relation before allocating a
            # physical signal.  The renderer consumes this exact descriptor;
            # no generated instance/token spelling participates in lookup.
            if edge.source.channel is not channel:
                raise SystemVerilogEmissionError(
                    "request/response directional buffer channel metadata is "
                    "inconsistent"
                )
            token = hashlib.sha256(
                repr((descriptor.semantic_id, channel.value)).encode()
            ).hexdigest()[:16]
            return composed.FormalBufferCountProjection(
                descriptor.semantic_id,
                channel,
                f"zlang_formal_rr_buffer_count_{token}",
                max(1, depth.bit_length()),
                depth,
            )
    return None


def _formal_adapter_count_projection(
    module: ir_module.Module,
    semantic_id: str,
) -> sv_protocols.FormalAdapterCountProjection | None:
    """Resolve receiver-credit occupancy to its typed adapter FIFO count.

    This is deliberately limited to the existing closed
    ``credit_to_rv`` adapter.  A receiver credit endpoint by itself does not
    imply implementation state, so no observation is fabricated unless the
    exact typed connection owns the FIFO that implements that occupancy.
    """

    for connection in module.connections:
        if connection.adapter is not ir_interfaces.ConnectionAdapter.CREDIT_TO_READY_VALID:
            continue
        source = connection.source
        destination = connection.destination
        observation_id = formal_observations.port_observation_id(source.name, "occupancy")
        if semantic_id != observation_id:
            continue
        if (
            source.protocol is not ir_interfaces.InterfaceProtocol.CREDIT
            or source.direction is not ir_module.PortDirection.INPUT
            or destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            or destination.direction is not ir_module.PortDirection.OUTPUT
            or source.capacity is None
            or connection.buffer_depth != source.capacity
            or source.type != destination.type
            or source.domain != destination.domain
        ):
            raise SystemVerilogEmissionError(
                "receiver-credit formal occupancy requires one exact typed "
                "credit_to_rv adapter FIFO"
            )
        depth = connection.buffer_depth
        token = hashlib.sha256(
            repr((
                observation_id,
                source.name,
                destination.name,
                connection.adapter.value,
                depth,
                str(source.type),
                source.domain,
            )).encode()
        ).hexdigest()[:16]
        return sv_protocols.FormalAdapterCountProjection(
            observation_id,
            source.name,
            destination.name,
            connection.adapter,
            f"zlang_formal_adapter_count_{token}",
            max(1, depth.bit_length()),
            depth,
        )
    return None


def _formal_rule_fire_reset(
    module: ir_module.Module,
    accepted: expr.Expression,
    clock: str | None = None,
) -> expr.Expression:
    """Gate the accepted schedule with the reset consumed by emitted state.

    ``module`` is the final backend-local component: synchronized-release roots
    consume their conditioner, while closed children consume the already
    conditioned native-release reset supplied through their component ABI.
    """

    if not module.clock_domains:
        return accepted
    domain = sv_sequential.module_domain(module, clock)
    bit = ir_types.BitType()
    origin = getattr(accepted, "origin", None)
    deasserted = expr.Binary(
        expr.BinaryOperator.EQUAL,
        expr.InputRef(
            sv_sequential.effective_reset_signal(module, sv_rendering._identifier, domain.clock),
            bit,
            origin=origin,
        ),
        expr.Constant(
            0 if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH else 1,
            bit, origin=origin,
        ),
        bit, bit, origin=origin,
    )
    return expr.Binary(
        expr.BinaryOperator.BIT_AND, deasserted, accepted,
        bit, bit, origin=origin,
    )


def _formal_local_expression(
    module: ir_module.Module, semantic_id: str, *, defer_rule_reset: bool = False,
) -> expr.Expression | None:
    """Return the typed expression for one already-frozen safety verification observation."""

    if module.resolved_transition is not None:
        group = next((
            item for item in module.resolved_transition.action_groups
            if formal_observations.rule_fire_observation_id(item.rule_name) == semantic_id
        ), None)
        if group is not None:
            # The legacy direct rule emitter represents accepted firing as an
            # ``if/else if`` control relation rather than a named RTL wire.
            # Project the exact minimized selection regions computed by the
            # authoritative ResolvedTransition scheduler; never substitute a
            # raw guard or infer the relation from emitted identifiers.
            bit = ir_types.BitType()

            def binary(
                operator: expr.BinaryOperator,
                left: expr.Expression,
                right: expr.Expression,
                operand_type: ir_types.HardwareType,
            ) -> expr.Binary:
                return expr.Binary(
                    operator, left, right, operand_type, bit,
                    origin=group.source_origin,
                )

            def conjunction(items: list[expr.Expression]) -> expr.Expression:
                result = items[0] if items else expr.Constant(1, bit)
                for item in items[1:]:
                    result = binary(expr.BinaryOperator.BIT_AND, result, item, bit)
                return result

            def disjunction(items: list[expr.Expression]) -> expr.Expression:
                result = items[0] if items else expr.Constant(0, bit)
                for item in items[1:]:
                    result = binary(expr.BinaryOperator.BIT_OR, result, item, bit)
                return result

            fifos = tuple(
                item for item in module.resolved_transition.resources
                if item.kind.value == "fifo"
            )
            groups = ir_state.ordered_groups(module.resolved_transition)
            activation_predicates = ir_state.conditional_activation_predicates(
                module.resolved_transition
            )
            fifo_by_name = {item.name: item for item in module.fifos}
            regions: list[expr.Expression] = []
            for region in ir_state.selection_regions(
                module.resolved_transition, group.rule_name
            ):
                terms: list[expr.Expression] = []
                for resource, occupancy in zip(
                    fifos, region[:len(fifos)], strict=True
                ):
                    if occupancy is None:
                        continue
                    fifo = fifo_by_name[resource.name]
                    count_type = ir_types.UIntType(fifo.count_width)
                    count = expr.FifoRef(
                        fifo.name, ir_storage.FifoSignal.COUNT, count_type,
                        origin=group.source_origin,
                    )
                    zero = expr.Constant(0, count_type)
                    depth = expr.Constant(fifo.depth, count_type)
                    if occupancy is ir_state.FifoOccupancy.EMPTY:
                        terms.append(binary(
                            expr.BinaryOperator.EQUAL, count, zero, count_type
                        ))
                    elif occupancy is ir_state.FifoOccupancy.FULL:
                        terms.append(binary(
                            expr.BinaryOperator.EQUAL, count, depth, count_type
                        ))
                    else:
                        terms.extend((
                            binary(expr.BinaryOperator.GREATER, count, zero, count_type),
                            binary(expr.BinaryOperator.LESS, count, depth, count_type),
                        ))
                guard_values = region[
                    len(fifos):len(fifos) + len(groups)
                ]
                activation_values = region[len(fifos) + len(groups):]
                for candidate, enabled in zip(
                    groups, guard_values, strict=True
                ):
                    if enabled is None:
                        continue
                    terms.append(
                        candidate.guard
                        if enabled else binary(
                            expr.BinaryOperator.EQUAL,
                            candidate.guard,
                            expr.Constant(0, bit),
                            bit,
                        )
                    )
                for activation, enabled in zip(
                    activation_predicates, activation_values, strict=True
                ):
                    if enabled is None:
                        continue
                    terms.append(
                        activation
                        if enabled else binary(
                            expr.BinaryOperator.EQUAL,
                            activation,
                            expr.Constant(0, bit),
                            bit,
                        )
                    )
                regions.append(conjunction(terms))
            accepted = disjunction(regions)
            return (
                accepted if defer_rule_reset
                else _formal_rule_fire_reset(module, accepted, group.domain)
            )
    if semantic_id.startswith("register:"):
        name = semantic_id.split(":", 1)[1]
        register = next((item for item in module.registers if item.name == name), None)
        return None if register is None else expr.RegisterRef(name, register.type)
    if semantic_id.startswith("fifo:"):
        value = semantic_id.split(":", 1)[1]
        if "." not in value:
            return None
        name, signal_text = value.split(".", 1)
        fifo = next((item for item in module.fifos if item.name == name), None)
        if fifo is None:
            return None
        try:
            signal = ir_storage.FifoSignal(signal_text)
        except ValueError:
            return None
        signal_type: ir_types.HardwareType
        if signal is ir_storage.FifoSignal.COUNT:
            signal_type = ir_types.UIntType(fifo.count_width)
        elif signal in {ir_storage.FifoSignal.PUSH, ir_storage.FifoSignal.POP, ir_storage.FifoSignal.EMPTY,
                        ir_storage.FifoSignal.FULL, ir_storage.FifoSignal.VALID, ir_storage.FifoSignal.READY}:
            signal_type = ir_types.BitType()
        elif signal in {ir_storage.FifoSignal.FRONT, ir_storage.FifoSignal.DATA}:
            signal_type = fifo.element_type
        else:
            return None
        return expr.FifoRef(name, signal, signal_type)
    if semantic_id.startswith("port:"):
        value = semantic_id.split(":", 1)[1]
        name, separator, signal_text = value.partition(".")
        port = next((item for item in module.ports if item.name == name), None)
        if port is None:
            return None
        if not separator:
            # A protocol endpoint is an ownership/aggregate identity, not one
            # physical packed RTL signal.  Only ordinary wire ports have a
            # base-value observation; protocol leaves are projected below.
            return (
                expr.InputRef(name, port.type)
                if port.protocol is ir_interfaces.InterfaceProtocol.WIRE
                else None
            )
        if port.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
            try:
                signal = ir_interfaces.ReadyValidSignal(signal_text)
            except ValueError:
                return None
            signal_type = port.type if signal is ir_interfaces.ReadyValidSignal.PAYLOAD else ir_types.BitType()
            return expr.ReadyValidRef(name, signal, signal_type)
        if port.protocol is ir_interfaces.InterfaceProtocol.CREDIT:
            if signal_text == "occupancy":
                projection = _formal_adapter_count_projection(module, semantic_id)
                if projection is None:
                    return None
                return expr.InputRef(
                    projection.signal,
                    ir_types.UIntType(projection.width),
                )
            try:
                signal = ir_interfaces.CreditSignal(signal_text)
            except ValueError:
                return None
            if signal is ir_interfaces.CreditSignal.PAYLOAD:
                signal_type = port.type
            elif signal is ir_interfaces.CreditSignal.CREDITS:
                signal_type = ir_types.UIntType(max(1, (port.capacity or 1).bit_length()))
            else:
                signal_type = ir_types.BitType()
            return expr.CreditRef(name, signal, signal_type)
        return None
    if semantic_id.startswith("csr-field:"):
        token = _csr_observation_token(module, semantic_id)
        if token is None:
            return None
        port = next((item for item in module.ports if item.name == token), None)
        return None if port is None else expr.InputRef(port.name, port.type)
    if semantic_id.startswith("rr:"):
        count_projection = _formal_buffer_count_projection(module, semantic_id)
        if count_projection is not None:
            descriptor = next(
                item for item in module.request_response_connections
                if item.semantic_id
                == count_projection.request_response_semantic_id
            )
            return expr.InputRef(
                count_projection.signal,
                ir_types.UIntType(count_projection.width),
                origin=descriptor.source_origin,
            )
        for descriptor in module.request_response_connections:
            tracker = sv_request_response.tracker_name(
                descriptor,
                module,
                identifier=sv_rendering._identifier,
            )
            width = max(1, descriptor.max_outstanding.bit_length())
            observations = {
                formal_observations.request_response_observation_id(
                    descriptor.semantic_id,
                    formal_observations.RequestResponseObservationSignal.OUTSTANDING,
                ): expr.InputRef(tracker, ir_types.UIntType(width)),
                formal_observations.request_response_observation_id(
                    descriptor.semantic_id,
                    formal_observations.RequestResponseObservationSignal.REQUEST_ACCEPT,
                ): expr.InputRef(f"{tracker}_request_transfer", ir_types.BitType()),
                formal_observations.request_response_observation_id(
                    descriptor.semantic_id,
                    formal_observations.RequestResponseObservationSignal.RESPONSE_CONSUME,
                ): expr.InputRef(f"{tracker}_response_transfer", ir_types.BitType()),
            }
            # An absent directional buffer has exactly zero occupancy by the
            # typed request/response connection contract.  Publish that fact as
            # a formal-only constant projection.  Buffered occupancy remains
            # unavailable until the FIFO component exposes its count through an
            # explicit typed formal ABI; never recover it from generated names.
            if not descriptor.request.request_buffer_depth:
                observations[formal_observations.request_response_observation_id(
                    descriptor.semantic_id,
                    formal_observations.RequestResponseObservationSignal.REQUEST_OCCUPANCY,
                )] = expr.Constant(
                    0, ir_types.UIntType(width), origin=descriptor.source_origin
                )
            if not descriptor.response.response_buffer_depth:
                observations[formal_observations.request_response_observation_id(
                    descriptor.semantic_id,
                    formal_observations.RequestResponseObservationSignal.RESPONSE_OCCUPANCY,
                )] = expr.Constant(
                    0, ir_types.UIntType(width), origin=descriptor.source_origin
                )
            if semantic_id in observations:
                return observations[semantic_id]
    # Directional-buffer occupancy remains explicitly unavailable until its
    # emitting IR entity exposes a typed value. Never recover observations by
    # parsing generated RTL identifiers.
    return None


def _formal_projection_name(relative_path: tuple[str, ...], semantic_id: str) -> str:
    payload = repr((relative_path, semantic_id)).encode()
    return "zlang_formal_local_" + hashlib.sha256(payload).hexdigest()[:16]


def instrument_direct_formal_module(
    module: ir_module.Module,
    recursive_design: object,
    top_tokens: dict[str, str],
) -> tuple[
    ir_module.Module,
    set[str],
    tuple[composed.FormalBufferCountProjection, ...],
    tuple[sv_protocols.FormalAdapterCountProjection, ...],
]:
    """Project typed observations through explicit component output ports."""

    by_path: dict[tuple[str, ...], list[object]] = {}
    for item in recursive_design.bindings:
        by_path.setdefault(tuple(item.physical_instance_path), []).append(item)
    available: set[str] = set()
    formal_buffer_counts: set[composed.FormalBufferCountProjection] = set()
    formal_adapter_counts: set[sv_protocols.FormalAdapterCountProjection] = set()
    namespace_hierarchy = sv_physicalization.validated_hierarchy(
        sv_physicalization.physicalize_generic_callables(module)
    )
    namespace_plans = {
        entry.physical_path: naming.module_rtl_names(entry.module)
        for entry in namespace_hierarchy.entries
    }

    def walk(current: ir_module.Module, path: tuple[str, ...], *, top: bool) -> tuple[ir_module.Module, dict[str, str]]:
        transformed_children: list[ir_module.Module] = []
        child_outputs: dict[str, dict[str, str]] = {}
        for child_module, elaborated in zip(
            current.children, current.elaborated_instances, strict=True
        ):
            array = elaborated.instance.array_length or 1
            if array != 1:
                transformed_children.append(child_module)
                continue
            child_path = (*path, elaborated.instance.name)
            child, outputs = walk(child_module, child_path, top=False)
            transformed_children.append(child)
            child_outputs[elaborated.instance.name] = outputs
        if not current.elaborated_instances:
            transformed_children = list(current.children)

        ports = list(current.ports)
        assignments = list(current.assignments)
        output_map: dict[str, str] = {}
        namespace = namespace_plans[path]
        used_names = set(namespace.reserved) | {
            item.physical_name for item in namespace.entries
        }
        deferred_rule_outputs: dict[str, str] = {}
        deferred_rr_outputs: dict[str, str] = {}
        local_rule_ids = {
            formal_observations.rule_fire_observation_id(group.rule_name)
            for group in (
                current.resolved_transition.action_groups
                if current.resolved_transition is not None else ()
            )
        }

        def publish(binding: object, expression: expr.Expression,
                    relative: tuple[str, ...]) -> None:
            semantic_binding_id = binding.semantic_binding_id
            preferred_name = (
                top_tokens[semantic_binding_id]
                if top else _formal_projection_name(relative, binding.ref.local_semantic_id)
            )
            name = output_map.get(semantic_binding_id)
            if name is None:
                name = identifiers.allocate_private_rtl_identifier(
                    preferred_name,
                    semantic_identity=f"formal-observation:{relative}:{binding.ref.local_semantic_id}",
                    used=used_names,
                )
                if top:
                    top_tokens[semantic_binding_id] = name
                # Formal observations have one stable packed physical token,
                # even when the observed semantic value is an aggregate.  The
                # production top ABI still exposes aggregate leaves; only this
                # formal-only projection is representation-bit packed.  Using
                # the semantic aggregate type here would make the normal top
                # ABI flattener publish ``token_field`` ports while the formal
                # manifest and harness correctly referred to ``token``.
                physical_type = _hardware_type_for_width(
                    binding.width, binding.signedness
                )
                physical_expression = expression
                if expression.type != physical_type:
                    physical_expression = expr.Bitcast(
                        expression, physical_type,
                        origin=getattr(expression, "origin", None),
                    )
                output = ir_module.Port(
                    ir_module.PortDirection.OUTPUT, name, physical_type,
                    domain=current.clock,
                )
                ports.append(output)
                assignments.append(ir_module.Assignment(output, physical_expression))
            output_map[semantic_binding_id] = name
            available.add(semantic_binding_id)

        for binding in sorted(
            by_path.get(path, ()), key=lambda item: item.semantic_binding_id
        ):
            expression = _formal_local_expression(
                current, binding.ref.local_semantic_id, defer_rule_reset=True,
            )
            if expression is not None:
                projection = _formal_buffer_count_projection(
                    current, binding.ref.local_semantic_id
                )
                if projection is not None:
                    formal_buffer_counts.add(projection)
                adapter_projection = _formal_adapter_count_projection(
                    current, binding.ref.local_semantic_id
                )
                if adapter_projection is not None:
                    formal_adapter_counts.add(adapter_projection)
                publish(binding, expression, ())
                if binding.ref.local_semantic_id in local_rule_ids:
                    deferred_rule_outputs[output_map[binding.semantic_binding_id]] = (
                        binding.ref.local_semantic_id
                    )
                if binding.ref.local_semantic_id.startswith("rr:"):
                    deferred_rr_outputs[output_map[binding.semantic_binding_id]] = (
                        binding.ref.local_semantic_id
                    )

        bindings_by_id = {
            item.semantic_binding_id: item
            for items in by_path.values() for item in items
        }
        for child_name, outputs in sorted(child_outputs.items()):
            for semantic_binding_id, child_port in sorted(outputs.items()):
                binding = bindings_by_id[semantic_binding_id]
                expression = expr.InstanceOutputRef(
                    child_name, child_port,
                    _hardware_type_for_width(binding.width, binding.signedness),
                )
                relative = tuple(binding.physical_instance_path[len(path):])
                publish(binding, expression, relative)

        emitted_name = f"{current.name}__formal" if top else current.name
        transformed_connections = current.hierarchical_connections
        transformed_request_responses = current.request_response_connections
        transformed_protocol_endpoints = current.protocol_endpoints
        if emitted_name != current.name:
            # The composed emitter recognizes a public endpoint by comparing
            # its typed owner with ``module.name``.  The formal-only top name
            # is physical ABI decoration; retain the exact connection graph
            # while moving only references owned by that renamed top.  Child
            # owners and physical instance identities remain untouched.
            def renamed_endpoint(endpoint):
                return (
                    replace(endpoint, owner=emitted_name)
                    if endpoint.owner == current.name
                    else endpoint
                )

            mapped_connections = {
                id(connection): replace(
                    connection,
                    source=renamed_endpoint(connection.source),
                    destination=renamed_endpoint(connection.destination),
                )
                for connection in current.hierarchical_connections
            }
            transformed_connections = tuple(
                mapped_connections[id(connection)]
                for connection in current.hierarchical_connections
            )
            transformed_request_responses = tuple(
                replace(
                    descriptor,
                    request=mapped_connections[id(descriptor.request)],
                    response=mapped_connections[id(descriptor.response)],
                    requester=(
                        emitted_name
                        if descriptor.requester == current.name
                        else descriptor.requester
                    ),
                    responder=(
                        emitted_name
                        if descriptor.responder == current.name
                        else descriptor.responder
                    ),
                )
                for descriptor in current.request_response_connections
            )
            transformed_protocol_endpoints = tuple(
                renamed_endpoint(endpoint)
                for endpoint in current.protocol_endpoints
            )
        transformed_elaboration = tuple(
            replace(
                item,
                semantic_path=(emitted_name, item.instance.name),
            )
            for item in current.elaborated_instances
        )
        transformed = replace(
            current,
            name=emitted_name,
            ports=tuple(ports), assignments=tuple(assignments),
            children=tuple(transformed_children),
            elaborated_instances=transformed_elaboration,
            protocol_endpoints=transformed_protocol_endpoints,
            hierarchical_connections=transformed_connections,
            request_response_connections=transformed_request_responses,
        )
        if deferred_rule_outputs:
            # Resolve reset only after formal ports and the physical top name
            # have been allocated.  The same reset helper and final component
            # scope are used by production state emission; no raw-port or
            # generated-name convention is substituted for that contract.
            reset_component = transformed if top else sv_sequential.native_release_module(transformed)
            rule_domains = {
                formal_observations.rule_fire_observation_id(group.rule_name): group.domain
                for group in transformed.resolved_transition.action_groups
            } if transformed.resolved_transition is not None else {}
            transformed = replace(
                transformed,
                assignments=tuple(
                    replace(
                        assignment,
                        expression=_formal_rule_fire_reset(
                            reset_component,
                            assignment.expression,
                            rule_domains.get(
                                deferred_rule_outputs[assignment.target.name]
                            ),
                        ),
                    )
                    if assignment.target.name in deferred_rule_outputs else assignment
                    for assignment in transformed.assignments
                ),
            )
        if deferred_rr_outputs:
            # Ledger spellings depend on the allocated physical owner. The
            # formal top decoration and generic-helper physicalization can
            # change a collision suffix, so resolve these typed projections
            # only in the final emitted namespace, never the source namespace.
            physical_component = sv_physicalization.physicalize_generic_callables(
                transformed
            )
            projected_assignments = []
            for assignment in transformed.assignments:
                semantic_id = deferred_rr_outputs.get(assignment.target.name)
                if semantic_id is None:
                    projected_assignments.append(assignment)
                    continue
                projection = _formal_local_expression(physical_component, semantic_id)
                if projection is None:
                    raise SystemVerilogEmissionError(
                        "formal request/response projection lost its typed observation"
                    )
                if projection.type != assignment.target.type:
                    projection = expr.Bitcast(
                        projection, assignment.target.type,
                        origin=getattr(projection, "origin", None),
                    )
                projected_assignments.append(replace(assignment, expression=projection))
            transformed = replace(transformed, assignments=tuple(projected_assignments))
        return transformed, output_map

    formal, _ = walk(module, (module.name,), top=True)
    return (
        formal,
        available,
        tuple(sorted(
            formal_buffer_counts,
            key=lambda item: (
                item.request_response_semantic_id,
                item.channel.value,
                item.signal,
            ),
        )),
        tuple(sorted(
            formal_adapter_counts,
            key=lambda item: (
                item.observation_semantic_id,
                item.source_port,
                item.destination_port,
                item.signal,
            ),
        )),
    )


def _hardware_type_for_width(width: int, signedness: str) -> ir_types.HardwareType:
    if width == 1 and signedness == "bit":
        return ir_types.BitType()
    return ir_types.SIntType(width) if signedness == "signed" else ir_types.UIntType(width)

def _csr_observation_token(module: ir_module.Module, semantic_id: str) -> str | None:
    for block in module.csr_blocks:
        for binding in block.state_bindings:
            base = ir_csr.csr_state_port_name(binding)
            if semantic_id == binding.semantic_state_id:
                return base
            if semantic_id == binding.write_hit_id:
                return ir_csr.csr_write_hit_port_name(binding)
            if semantic_id == binding.write_value_id:
                return ir_csr.csr_write_value_port_name(binding)
        for observation in block.access_observations:
            if semantic_id == observation.read_hit_id:
                return ir_csr.csr_read_hit_port_name(observation)
            if semantic_id == observation.write_hit_id:
                return ir_csr.csr_observation_write_hit_port_name(observation)
            if semantic_id == observation.write_value_id:
                return ir_csr.csr_observation_write_value_port_name(observation)
            if semantic_id == observation.value_id:
                return ir_csr.csr_observation_value_port_name(observation)
        for register in block.registers:
            for event in register.events:
                if semantic_id == event.semantic_value_id:
                    return ir_csr.csr_event_port_name(event)
        for view in block.split_views:
            if semantic_id == view.semantic_value_id:
                return ir_csr.csr_split_port_name(block, view)
    return None


def recursive_signal_token(module: ir_module.Module, semantic_id: str) -> str | None:
    """Map a semantic object to a signal emitted by the generic SV ABI.

    This mapping is produced beside the emitter from typed object kinds; it is
    never reconstructed later from a synthesized RTL name.
    """
    if semantic_id == "clock":
        return sv_rendering._identifier(module.clock) if module.clock is not None else None
    if semantic_id == "reset":
        return sv_rendering._identifier(module.reset) if module.reset is not None else None
    if semantic_id.startswith("register:"):
        return sv_rendering._identifier(semantic_id.split(":", 1)[1])
    if semantic_id.startswith("fifo:"):
        return sv_rendering._identifier(semantic_id.split(":", 1)[1].replace(".", "_"))
    if semantic_id.startswith("port:"):
        value = semantic_id.split(":", 1)[1]
        parts = value.split(".")
        if len(parts) == 1:
            port = next((item for item in module.ports if item.name == parts[0]), None)
            if port is None or port.protocol is not ir_interfaces.InterfaceProtocol.WIRE:
                # Aggregate protocols have no physical base wire. Only their
                # typed leaves may receive locators.
                return None
            return sv_rendering._identifier(parts[0])
        return sv_rendering._identifier(parts[0]) + "_" + "_".join(sv_rendering._identifier(part) for part in parts[1:])
    csr_token = _csr_observation_token(module, semantic_id)
    if csr_token is not None:
        return csr_token
    if semantic_id.startswith("rr:"):
        for descriptor in module.request_response_connections:
            tracker = sv_request_response.tracker_name(
                descriptor,
                module,
                identifier=sv_rendering._identifier,
            )
            tokens = {
                formal_observations.request_response_observation_id(
                    descriptor.semantic_id,
                    formal_observations.RequestResponseObservationSignal.OUTSTANDING,
                ): tracker,
                formal_observations.request_response_observation_id(
                    descriptor.semantic_id,
                    formal_observations.RequestResponseObservationSignal.REQUEST_ACCEPT,
                ): f"{tracker}_request_transfer",
                formal_observations.request_response_observation_id(
                    descriptor.semantic_id,
                    formal_observations.RequestResponseObservationSignal.RESPONSE_CONSUME,
                ): f"{tracker}_response_transfer",
            }
            if semantic_id in tokens:
                return tokens[semantic_id]
    return None
