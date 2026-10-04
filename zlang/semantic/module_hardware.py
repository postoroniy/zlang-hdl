"""Name resolution and type checking for ZLang HDL."""

from __future__ import annotations

import hashlib

from zlang.ast import nodes as ast
from zlang.ir import module as ir_module
from zlang.ir import cdc as ir_cdc
from zlang.ir import arbitration as ir_arbitration
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin
from .errors import SemanticError
from . import context as semantic_context
from . import csr as semantic_csr
from . import hierarchy as semantic_hierarchy
from . import module_pipeline
from . import module_interfaces as semantic_module_interfaces
from . import observations as _observations
from . import type_resolution


_CSR_LAYOUT_BUILDER = semantic_csr.CsrLayoutBuilder()
_CSR_BINDING_RESOLVER = semantic_csr.CsrBindingResolver()
_CSR_ANALYZER = semantic_csr.CsrAnalyzer(
    _CSR_LAYOUT_BUILDER,
    _CSR_BINDING_RESOLVER,
)


class HardwareInterfacePreparer:
    """Own public ports, protocol interfaces, CSR ABI, and timing domains."""

    def prepare(
        self,
        context: semantic_context.AnalysisContext,
        preparation: module_pipeline.DeclarationPreparationProduct,
    ) -> module_pipeline.HardwareInterfaceProduct:
        module = preparation.module
        type_resolver = preparation.type_resolver
        pure_context = preparation.expression_context
        effective_source_unit = preparation.source_unit
        effective_source_digest = preparation.source_digest
        allow_external_enum_inputs = context.source.allow_external_enum_inputs
        domain_product = semantic_hierarchy.ClockDomainAnalyzer().analyze(
            module, context.hierarchy.inherited_domain,
            source_unit=effective_source_unit,
            source_digest=effective_source_digest,
        )
        clock_domains = domain_product.domains
        clock = domain_product.default_clock
        reset = domain_product.default_reset
        timing_names = domain_product.timing_names
        symbols: dict[str, ir_module.Port] = {}
        ports: list[ir_module.Port] = []
        port_origins: dict[str, SourceOrigin | None] = {}
        for declaration in module.ports:
            if declaration.name in symbols:
                raise SemanticError(f"duplicate port '{declaration.name}'")
            direction = (
                ir_module.PortDirection.INPUT
                if declaration.direction is ast.Direction.INPUT
                else ir_module.PortDirection.OUTPUT
            )
            port_syntax = declaration.type_name
            if isinstance(port_syntax, ast.InterfaceTypeName):
                protocol = semantic_module_interfaces.interface_protocol(
                    port_syntax.kind
                )
                payload_syntax = port_syntax.payload_type
                capacity = port_syntax.capacity
                virtual_channels = port_syntax.virtual_channels
            else:
                protocol = ir_interfaces.InterfaceProtocol.WIRE
                payload_syntax = port_syntax
                capacity = None
                virtual_channels = None
            port = ir_module.Port(
                direction=direction,
                name=declaration.name,
                type=type_resolver.resolve(payload_syntax),
                protocol=protocol,
                capacity=capacity,
                domain=declaration.domain if declaration.domain is not None else clock,
                virtual_channels=virtual_channels,
            )
            if (
                direction is ir_module.PortDirection.INPUT
                and not allow_external_enum_inputs
                and type_resolution.contains_nominal_type(port.type, ir_types.EnumType)
            ):
                boundary_message = (
                    f"top-level input '{port.name}' cannot expose enum type "
                    f"{port.type.name}; use an internal child interface"
                    if isinstance(port.type, ir_types.EnumType)
                    else f"top-level input '{port.name}' cannot expose type "
                    f"{port.type} because it contains an enum-valued field; "
                    "use an internal child interface"
                )
                raise SemanticError(boundary_message)
            if protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT:
                if (
                    virtual_channels is None
                    or virtual_channels < 2
                    or virtual_channels & (virtual_channels - 1)
                ):
                    raise SemanticError(
                        f"vc_credit interface '{port.name}' virtual-channel count "
                        "must be a power of two and at least 2"
                    )
                if capacity is None or capacity < 1:
                    raise SemanticError(
                        f"vc_credit interface '{port.name}' requires at least one "
                        "credit per virtual channel"
                    )
            if declaration.domain is not None and declaration.domain not in module.clocks:
                raise SemanticError(
                    f"port '{declaration.name}' references unknown clock domain "
                    f"'{declaration.domain}'"
                )
            if len(clock_domains) > 1 and port.domain is None:
                raise SemanticError(
                    f"port '{declaration.name}' requires an explicit clock domain"
                )
            symbols[port.name] = port
            port_origin = _observations.declaration_origin(
                declaration.name_origins[0]
                if declaration.name_origins else declaration.origin,
                f"port {declaration.name}",
                pure_context,
            )
            port_origins[port.name] = port_origin
            _observations.remember_definition_target(
                pure_context, port, port_origin,
                name=declaration.name, kind="port",
            )
            ports.append(port)

        source_scalar_ports = tuple(ports)

        external_model_function: ir_module.Function | None = None
        if module.external_model is not None:
            aggregate_port = next(
                (
                    port
                    for port in source_scalar_ports
                    if isinstance(port.type, (ir_types.StructType, ir_types.TupleType, ir_types.VecType))
                ),
                None,
            )
            if aggregate_port is not None:
                raise SemanticError(
                    f"external module '{module.name}' port '{aggregate_port.name}' "
                    "must be a scalar wire in this first slice",
                    code="ZL-EXTERN-UNSUPPORTED",
                )
            external_model_function = next(
                (item for item in preparation.functions if item.name == module.external_model),
                None,
            )
            if external_model_function is None:
                if module.external_model in preparation.generic_functions:
                    detail = "must be non-generic"
                else:
                    detail = "does not name an existing function"
                raise SemanticError(
                    f"external module '{module.name}' model '{module.external_model}' "
                    f"{detail}",
                    code="ZL-EXTERN-MODEL",
                )
            external_inputs = tuple(
                port for port in source_scalar_ports
                if port.direction is ir_module.PortDirection.INPUT
            )
            external_outputs = tuple(
                port for port in source_scalar_ports
                if port.direction is ir_module.PortDirection.OUTPUT
            )
            expected_parameters = tuple(
                (port.name, port.type) for port in external_inputs
            )
            actual_parameters = tuple(
                (parameter.name, parameter.type)
                for parameter in external_model_function.parameters
            )
            if actual_parameters != expected_parameters:
                raise SemanticError(
                    f"external module '{module.name}' model parameters must exactly "
                    f"match inputs {expected_parameters}, got {actual_parameters}",
                    code="ZL-EXTERN-MODEL",
                )
            if external_model_function.return_type != external_outputs[0].type:
                raise SemanticError(
                    f"external module '{module.name}' model returns "
                    f"{external_model_function.return_type}, expected "
                    f"{external_outputs[0].type}",
                    code="ZL-EXTERN-MODEL",
                )

        csr_module_payload = (
            f"{module.source_identity or module.name}|{module.source_hash or ''}|"
            f"{tuple((p.name, p.kind, p.default) for p in module.parameters)}"
        )
        csr_module_identity = hashlib.sha256(
            csr_module_payload.encode()
        ).hexdigest()[:24]
        csr_blocks = _CSR_ANALYZER.analyze(
            module.csr_blocks,
            module.csr_groups,
            type_resolver,
            symbols,
            module_identity=csr_module_identity,
            clock_domain=clock,
            clock_domains=clock_domains,
            source_unit=effective_source_unit,
            source_digest=effective_source_digest,
        )

        connections: list[ir_module.Connection] = []
        connected_sources: set[str] = set()
        connected_destinations: set[str] = set()
        readable_cdc_outputs: set[str] = set()
        aggregate_interface_names = {
            declaration.name for declaration in module.aggregate_interfaces
        }
        for declaration in module.connections:
            if declaration.transform is not None:
                # The transform owns ready/valid data and control.  It is lowered
                # after locals/callables are available and must not also become an
                # ordinary pass-through connection.
                continue
            if "." in declaration.source or "." in declaration.destination:
                continue
            if (
                declaration.source in aggregate_interface_names
                or declaration.destination in aggregate_interface_names
            ):
                # Aggregate endpoints are specialized below, after their source
                # protocol declarations have been resolved.  Do not misclassify
                # an undotted aggregate pass-through as a scalar/rv port edge.
                continue
            source = symbols.get(declaration.source)
            destination = symbols.get(declaration.destination)
            if source is None or destination is None:
                missing_name = (
                    declaration.source if source is None else declaration.destination
                )
                raise SemanticError(f"connection endpoint '{missing_name}' is not a port")
            if source.direction is not ir_module.PortDirection.INPUT:
                raise SemanticError(
                    f"connection source '{source.name}' must be an input interface",
                    code="ZL-PROTOCOL-OWNERSHIP",
                )
            if destination.direction is not ir_module.PortDirection.OUTPUT:
                raise SemanticError(
                    f"connection destination '{destination.name}' must be an output interface",
                    code="ZL-PROTOCOL-OWNERSHIP",
                )
            if source.type != destination.type:
                raise SemanticError(
                    f"connection payload mismatch: {source.name} is {source.type}, "
                    f"{destination.name} is {destination.type}",
                    code="ZL-PROTOCOL-TYPE",
                )
            if source.protocol in {
                ir_interfaces.InterfaceProtocol.PACKET,
                ir_interfaces.InterfaceProtocol.VC_CREDIT,
            } or destination.protocol in {
                ir_interfaces.InterfaceProtocol.PACKET,
                ir_interfaces.InterfaceProtocol.VC_CREDIT,
            }:
                raise SemanticError(
                    "packet and virtual-channel credit ports require their "
                    "explicit composition forms"
                )
            if source.name in connected_sources or destination.name in connected_destinations:
                raise SemanticError("an interface may participate in only one connection")
            adapter = (
                ir_interfaces.ConnectionAdapter(declaration.adapter.value)
                if declaration.adapter is not None
                else None
            )
            crossing = (
                ir_cdc.Crossing(
                    ir_cdc.CrossingKind(declaration.crossing.kind.value),
                    (
                        type_resolver._eval_storage_depth(
                            declaration.crossing.depth,
                            kind="async_fifo",
                            name=f"{declaration.source}->{declaration.destination}",
                        )
                        if declaration.crossing.depth is not None
                        else None
                    ),
                )
                if declaration.crossing is not None
                else None
            )
            if crossing is not None and (
                declaration.buffer_depth or adapter is not None
            ):
                raise SemanticError(
                    "a clock-domain crossing cannot also specify a buffer or adapter"
                )
            expected_adapter: ir_interfaces.ConnectionAdapter | None
            if source.protocol is destination.protocol:
                expected_adapter = None
            elif (
                source.protocol is ir_interfaces.InterfaceProtocol.READY_VALID
                and destination.protocol is ir_interfaces.InterfaceProtocol.CREDIT
            ):
                expected_adapter = ir_interfaces.ConnectionAdapter.READY_VALID_TO_CREDIT
            elif (
                source.protocol is ir_interfaces.InterfaceProtocol.CREDIT
                and destination.protocol is ir_interfaces.InterfaceProtocol.READY_VALID
            ):
                expected_adapter = ir_interfaces.ConnectionAdapter.CREDIT_TO_READY_VALID
            else:
                raise SemanticError(
                    f"no explicit adapter exists from {source.protocol.value} to "
                    f"{destination.protocol.value}"
                )
            if expected_adapter is None and adapter is not None:
                raise SemanticError("identical protocols must not specify an adapter")
            if expected_adapter is not None and adapter is not expected_adapter:
                raise SemanticError(
                    f"connection from {source.protocol.value} to "
                    f"{destination.protocol.value} requires adapter "
                    f"{expected_adapter.value}"
                )
            if (
                adapter is ir_interfaces.ConnectionAdapter.CREDIT_TO_READY_VALID
                and declaration.buffer_depth == 0
            ):
                raise SemanticError("credit_to_rv requires an explicit buffer depth")
            if (
                source.protocol is ir_interfaces.InterfaceProtocol.CREDIT
                and destination.protocol is ir_interfaces.InterfaceProtocol.CREDIT
                and source.capacity != destination.capacity
            ):
                raise SemanticError(
                    "identical credit connections require equal capacities"
                )
            if declaration.buffer_depth and expected_adapter is None and (
                source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            ):
                raise SemanticError(
                    "explicit buffers are currently supported only for ready/valid "
                    "connections and credit_to_rv adapters"
                )
            if (
                adapter is ir_interfaces.ConnectionAdapter.READY_VALID_TO_CREDIT
                and declaration.buffer_depth
            ):
                raise SemanticError(
                    "rv_to_credit does not accept a buffer; add a separate buffered "
                    "ready/valid connection"
                )
            if (
                adapter is ir_interfaces.ConnectionAdapter.CREDIT_TO_READY_VALID
                and source.capacity is not None
                and declaration.buffer_depth < source.capacity
            ):
                raise SemanticError(
                    f"credit_to_rv buffer depth {declaration.buffer_depth} is smaller "
                    f"than source capacity {source.capacity}"
                )
            if source.domain != destination.domain:
                if crossing is None:
                    raise SemanticError(
                        f"implicit clock-domain crossing from '{source.name}' "
                        f"({source.domain}) to '{destination.name}' "
                        f"({destination.domain}) is not allowed",
                        code="ZL-DOMAIN-CROSSING",
                        fixes=("use an explicit crossing with compatible endpoints",),
                    )
            elif crossing is not None:
                raise SemanticError(
                    f"crossing '{crossing.kind.value}' requires different domains"
                )
            if crossing is not None:
                if crossing.kind in {
                    ir_cdc.CrossingKind.SYNC_LEVEL,
                    ir_cdc.CrossingKind.PULSE_TOGGLE,
                }:
                    if (
                        source.protocol is not ir_interfaces.InterfaceProtocol.WIRE
                        or destination.protocol is not ir_interfaces.InterfaceProtocol.WIRE
                        or source.type != ir_types.BitType()
                    ):
                        raise SemanticError(
                            f"{crossing.kind.value} crossing requires bit wire endpoints"
                        )
                    if crossing.depth is not None:
                        raise SemanticError(
                            f"{crossing.kind.value} crossing does not accept a depth"
                        )
                else:
                    if (
                        source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
                        or destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
                    ):
                        raise SemanticError(
                            f"{crossing.kind.value} crossing requires ready/valid endpoints"
                        )
                    if crossing.kind is ir_cdc.CrossingKind.HANDSHAKE:
                        if not isinstance(
                            source.type, (ir_types.BitType, ir_types.UIntType, ir_types.SIntType, ir_types.BitsType)
                        ):
                            raise SemanticError(
                                "handshake crossing currently requires a scalar payload"
                            )
                        if crossing.depth is not None:
                            raise SemanticError(
                                "handshake crossing does not accept a depth"
                            )
                    else:
                        depth = crossing.depth
                        if (
                            depth is None
                            or depth < 4
                            or depth & (depth - 1)
                        ):
                            raise SemanticError(
                                "async_fifo depth must be a power of two and at least 4"
                            )
            connections.append(
                ir_module.Connection(
                    source,
                    destination,
                    declaration.buffer_depth,
                    adapter,
                    crossing,
                )
            )
            connected_sources.add(source.name)
            connected_destinations.add(destination.name)
            if (
                crossing is not None
                and destination.direction is ir_module.PortDirection.OUTPUT
                and destination.protocol is ir_interfaces.InterfaceProtocol.WIRE
            ):
                readable_cdc_outputs.add(destination.name)

        arbiters: list[ir_arbitration.PacketArbiter] = []
        arbitrated_ports: set[str] = set()
        if len(module.arbiters) > 1:
            raise SemanticError("a module currently supports exactly one packet arbiter")
        for declaration in module.arbiters:
            if clock is None or reset is None:
                raise SemanticError("packet arbiters require one module clock and reset")
            source_ports: list[ir_module.Port] = []
            for source_name in declaration.sources:
                source = symbols.get(source_name)
                if source is None:
                    raise SemanticError(f"arbiter source '{source_name}' is not a port")
                if source.direction is not ir_module.PortDirection.INPUT:
                    raise SemanticError(f"arbiter source '{source_name}' must be an input")
                if source.protocol is not ir_interfaces.InterfaceProtocol.PACKET:
                    raise SemanticError(
                        f"arbiter source '{source_name}' must use packet<T>"
                    )
                if source.name in arbitrated_ports:
                    raise SemanticError(
                        f"packet port '{source.name}' participates in more than one arbiter"
                    )
                source_ports.append(source)
            if len({port.name for port in source_ports}) != len(source_ports):
                raise SemanticError("arbiter sources must be distinct")
            destination = symbols.get(declaration.destination)
            if destination is None:
                raise SemanticError(
                    f"arbiter destination '{declaration.destination}' is not a port"
                )
            if destination.direction is not ir_module.PortDirection.OUTPUT:
                raise SemanticError(
                    f"arbiter destination '{destination.name}' must be an output"
                )
            if destination.protocol is not ir_interfaces.InterfaceProtocol.PACKET:
                raise SemanticError(
                    f"arbiter destination '{destination.name}' must use packet<T>"
                )
            if destination.name in arbitrated_ports:
                raise SemanticError(
                    f"packet port '{destination.name}' participates in more than one arbiter"
                )
            for source in source_ports:
                if source.type != destination.type:
                    raise SemanticError(
                        f"arbiter payload mismatch: {source.name} is {source.type}, "
                        f"{destination.name} is {destination.type}"
                    )
                if source.domain != destination.domain:
                    raise SemanticError("packet arbiter endpoints must share one clock domain")
            arbiter = ir_arbitration.PacketArbiter(
                tuple(source_ports),
                destination,
                ir_arbitration.ArbitrationPolicy(declaration.policy.value),
                ir_arbitration.GrantScope(declaration.grant_scope.value),
            )
            arbiters.append(arbiter)
            arbitrated_ports.update(port.name for port in source_ports)
            arbitrated_ports.add(destination.name)

        request_responses: list[ir_module.RequestResponseInterface] = []
        request_response_symbols: dict[str, ir_module.RequestResponseInterface] = {}
        for declaration in module.request_responses:
            if declaration.name in symbols or declaration.name in request_response_symbols:
                raise SemanticError(f"duplicate interface or port '{declaration.name}'")
            request_type = type_resolver.resolve(declaration.request_type)
            response_type = type_resolver.resolve(declaration.response_type)
            if declaration.max_outstanding <= 0:
                raise SemanticError(
                    f"request/response interface '{declaration.name}' requires "
                    "max_outstanding > 0"
                )
            ordering = ir_interfaces.RequestResponseOrdering(declaration.ordering.value)
            match_by = declaration.match_by
            id_type: ir_types.HardwareType | None = None
            if ordering is ir_interfaces.RequestResponseOrdering.IN_ORDER:
                if match_by is not None:
                    raise SemanticError(
                        f"in-order interface '{declaration.name}' must not use match_by"
                    )
            else:
                if match_by is None:
                    raise SemanticError(
                        f"out-of-order interface '{declaration.name}' requires match_by"
                    )
                if not isinstance(request_type, ir_types.StructType) or not isinstance(
                    response_type, ir_types.StructType
                ):
                    raise SemanticError(
                        f"out-of-order interface '{declaration.name}' requires struct "
                        "request and response payloads"
                    )
                request_id = request_type.field(match_by)
                response_id = response_type.field(match_by)
                if request_id is None or response_id is None:
                    raise SemanticError(
                        f"match field '{match_by}' must exist in both request and "
                        "response payloads"
                    )
                if request_id.type != response_id.type:
                    raise SemanticError(
                        f"match field '{match_by}' has different request and response types"
                    )
                if not isinstance(
                    request_id.type, (ir_types.BitType, ir_types.UIntType, ir_types.SIntType, ir_types.BitsType)
                ):
                    raise SemanticError(
                        f"match field '{match_by}' must be a scalar hardware type"
                    )
                id_type = request_id.type
            interface = ir_module.RequestResponseInterface(
                declaration.name,
                request_type,
                response_type,
                declaration.max_outstanding,
                ordering,
                match_by,
                id_type,
            )
            request_responses.append(interface)
            request_response_symbols[interface.name] = interface

        # A sequential ready/valid module has the same typed semantics whether it
        # is selected as the top or elaborated as a child.  Backend capability is
        # validated after this context-independent semantic lowering; do not make
        # source legality depend on the private child-specialization entry point.
        if not clock_domains and any(
            port.protocol is ir_interfaces.InterfaceProtocol.CREDIT for port in ports
        ):
            raise SemanticError("credit interfaces require a module clock and reset")
        if not clock_domains and any(
            port.protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT for port in ports
        ):
            raise SemanticError(
                "virtual-channel credit interfaces require a module clock and reset"
            )
        if any(port.protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT for port in ports) and (
            clock is None or reset is None
        ):
            raise SemanticError(
                "virtual-channel credit interfaces require one module clock and reset"
            )
        if any(port.protocol is ir_interfaces.InterfaceProtocol.PACKET for port in ports) and not arbiters:
            raise SemanticError(
                "packet interfaces currently require an explicit packet arbiter"
            )
        if not clock_domains and request_responses:
            raise SemanticError(
                "request/response interfaces require a module clock and reset"
            )
        if not clock_domains and csr_blocks:
            raise SemanticError("CSR blocks require a module clock and reset")
        if not clock_domains and any(
            connection.buffer_depth or connection.adapter is not None
            for connection in connections
        ):
            raise SemanticError(
                "buffered and adapted connections require a module clock and reset"
            )
        for timing_name in timing_names:
            if timing_name in symbols or timing_name in request_response_symbols:
                raise SemanticError(
                    f"clock/reset name '{timing_name}' conflicts with a port"
                )
        csr_names = {block.name for block in csr_blocks}
        if csr_names & (symbols.keys() | request_response_symbols.keys()):
            conflict = sorted(
                csr_names & (symbols.keys() | request_response_symbols.keys())
            )[0]
            raise SemanticError(f"CSR block name '{conflict}' conflicts with a port")

        return module_pipeline.HardwareInterfaceProduct(
            tuple(clock_domains),
            clock,
            reset,
            frozenset(timing_names),
            semantic_hierarchy.StateDomainResolver(clock_domains),
            symbols,
            tuple(ports),
            port_origins,
            source_scalar_ports,
            external_model_function,
            csr_module_identity,
            tuple(csr_blocks),
            tuple(connections),
            frozenset(readable_cdc_outputs),
            tuple(arbiters),
            tuple(request_responses),
            request_response_symbols,
            frozenset(csr_names),
        )
