"""Name resolution and type checking for ZLang HDL."""

from __future__ import annotations

import hashlib

from zlang.ir import module as ir_module
from zlang.ir import cdc as ir_cdc
from zlang.ir import arbitration as ir_arbitration
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import types as ir_types
from .errors import SemanticError
from . import context as semantic_context
from . import csr as semantic_csr
from . import hierarchy as semantic_hierarchy
from . import module_pipeline
from .hardware_interface_validation import validate_hardware_interface
from .external_model_analysis import resolve_external_model_function
from .port_analysis import analyze_ports


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
        port_product = analyze_ports(
            module,
            type_resolver=type_resolver,
            expression_context=pure_context,
            default_clock=clock,
            clock_domain_count=len(clock_domains),
            allow_external_enum_inputs=allow_external_enum_inputs,
        )
        symbols = port_product.symbols
        ports = list(port_product.ports)
        port_origins = port_product.origins
        source_scalar_ports = port_product.ports

        external_model_function = resolve_external_model_function(
            module,
            source_scalar_ports,
            preparation.functions,
            preparation.generic_functions,
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

        csr_names = validate_hardware_interface(
            clock_domains=clock_domains,
            clock=clock,
            reset=reset,
            timing_names=timing_names,
            symbols=symbols,
            ports=ports,
            csr_blocks=csr_blocks,
            connections=connections,
            arbiters=arbiters,
            request_responses=request_responses,
            request_response_symbols=request_response_symbols,
        )

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
            csr_names,
        )
