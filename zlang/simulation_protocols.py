"""Compiler-side lowering of protocol endpoints to primitive wire fields.

The executable simulation boundary deliberately has no protocol operations.
This module erases the first supported protocol family, ready/valid, into
ordinary typed ports and expressions before :mod:`zlang.simulation_plan`
constructs the language-neutral bit-vector machine.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib

from zlang.ir import expressions as expr
from zlang.ir.arbitration import ArbitrationPolicy, GrantScope
from zlang.ir.interfaces import (
    ConnectionAdapter,
    CreditSignal,
    InterfaceProtocol,
    PacketSignal,
    ReadyValidSignal,
    ready_valid_field_name,
    RequestResponseChannel,
    RequestResponseOrdering,
    RequestResponseRole,
    VirtualChannelCreditSignal,
)
from zlang.ir.module import (
    AggregateProtocolEndpoint,
    Assignment,
    HierarchicalConnection,
    InstancePortBinding,
    Module,
    NextAssignment,
    Port,
    PortDirection,
    ProtocolEndpoint,
    Register,
    RequestResponseInterface,
)
from zlang.ir.storage import Fifo, FifoSignal
from zlang.ir.types import BitType, UIntType, VecType
from zlang.ir.verification import (
    VerificationGoal,
    VerificationGoalKind,
    VerificationScope,
)
from zlang.simulation_rewrite import SimulationExpressionRewriter
from zlang.simulation_primitives import bit_binary as _bit_binary
from zlang.simulation_primitives import bit_not as _bit_not


class ProtocolSimulationLoweringError(ValueError):
    """A protocol module cannot be erased to primitive simulation fields."""


RUNTIME_PROTOCOL_SCOPE_PREFIX = "$zlang_runtime_protocol:"


def credit_field_name(endpoint: str, signal: CreditSignal | str) -> str:
    """Return the private scalar-plan name for one credit endpoint field."""

    value = signal.value if isinstance(signal, CreditSignal) else signal
    return f"$zlang_protocol:{endpoint}:{value}"


def credit_state_name(endpoint: str, kind: str) -> str:
    """Return the deterministic compiler-owned credit accounting state name."""

    return f"$zlang_protocol_credit:{endpoint}:{kind}"


def vc_credit_field_name(
    endpoint: str,
    signal: VirtualChannelCreditSignal | str,
) -> str:
    """Return the private scalar-plan name for one VC-credit field."""

    value = (
        signal.value
        if isinstance(signal, VirtualChannelCreditSignal)
        else signal
    )
    return f"$zlang_protocol:{endpoint}:{value}"


def vc_credit_state_name(endpoint: str, kind: str, channel: int) -> str:
    """Return one deterministic per-channel accounting state name."""

    return f"$zlang_protocol_vc_credit:{endpoint}:{kind}:{channel}"


def packet_field_name(endpoint: str, signal: PacketSignal | str) -> str:
    """Return the private scalar-plan name for one packet endpoint field."""

    value = signal.value if isinstance(signal, PacketSignal) else signal
    return f"$zlang_protocol:{endpoint}:{value}"


def packet_state_name(kind: str, source: str | None = None) -> str:
    """Return a deterministic compiler-owned packet arbiter state name."""

    suffix = "" if source is None else f":{source}"
    return f"$zlang_protocol_packet:{kind}{suffix}"


def request_response_field_name(
    interface: str,
    channel: RequestResponseChannel | str,
    signal: ReadyValidSignal | str,
) -> str:
    """Return one private scalar request/response field name."""

    channel_name = channel.value if isinstance(channel, RequestResponseChannel) else channel
    signal_name = signal.value if isinstance(signal, ReadyValidSignal) else signal
    return f"$zlang_request_response:{interface}:{channel_name}:{signal_name}"


def request_response_state_name(interface: str, kind: str) -> str:
    """Return deterministic compiler-owned request/response state."""

    return f"$zlang_request_response_state:{interface}:{kind}"


def _runtime_protocol_scope(
    *,
    module: Module,
    port: Port,
    conditions: tuple[tuple[str, expr.Expression], ...],
) -> VerificationScope:
    """Encode compiler-owned safety checks as generic primitive probes."""

    domain = port.domain or module.clock
    if domain is None:
        raise ProtocolSimulationLoweringError(
            f"protocol endpoint '{port.name}' has no owning clock domain"
        )
    reset = next(
        (
            candidate.reset
            for candidate in module.clock_domains
            if candidate.clock == domain
        ),
        module.reset if module.clock == domain else None,
    )
    if reset is None:
        raise ProtocolSimulationLoweringError(
            f"protocol endpoint '{port.name}' has no owning reset"
        )
    digest = hashlib.sha256(
        (
            f"{module.name}|{port.name}|{port.protocol.value}|{domain}|"
            "protocol-check"
        ).encode("utf-8")
    ).hexdigest()
    scope_id = RUNTIME_PROTOCOL_SCOPE_PREFIX + digest[:24]
    goals = tuple(
        VerificationGoal(
            semantic_id=f"{scope_id}:{index}",
            scope_id=scope_id,
            kind=VerificationGoalKind.ASSERT,
            name=message,
            expression=condition,
        )
        for index, (message, condition) in enumerate(conditions)
    )
    return VerificationScope(
        semantic_id=scope_id,
        name=f"{port.protocol.value} endpoint {port.name}",
        clock=domain,
        reset=reset,
        goals=goals,
    )


def _field_is_external(port: Port, signal: ReadyValidSignal) -> bool:
    if signal is ReadyValidSignal.TRANSFER:
        return False
    if port.direction is PortDirection.INPUT:
        return signal in {ReadyValidSignal.PAYLOAD, ReadyValidSignal.VALID}
    return signal is ReadyValidSignal.READY


def _field_type(port: Port, signal: ReadyValidSignal):
    return port.type if signal is ReadyValidSignal.PAYLOAD else BitType()


class _ReadyValidExpressionLowerer(SimulationExpressionRewriter):
    def __init__(
        self,
        ports: dict[str, Port],
        producers: dict[tuple[str, ReadyValidSignal], expr.Expression],
    ) -> None:
        super().__init__()
        self._ports = ports
        self._producers = producers
        self._active: set[tuple[str, ReadyValidSignal]] = set()

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if isinstance(value, expr.ReadyValidRef):
            return self._reference(value)
        return None

    def _reference(self, value: expr.ReadyValidRef) -> expr.Expression:
        try:
            port = self._ports[value.interface]
        except KeyError as error:
            raise ProtocolSimulationLoweringError(
                f"ready/valid reference names unknown endpoint '{value.interface}'"
            ) from error
        if value.signal is ReadyValidSignal.TRANSFER:
            valid = self._signal(port, ReadyValidSignal.VALID, value.origin)
            ready = self._signal(port, ReadyValidSignal.READY, value.origin)
            return expr.Binary(
                expr.BinaryOperator.BIT_AND,
                valid,
                ready,
                BitType(),
                BitType(),
                origin=value.origin,
            )
        return self._signal(port, value.signal, value.origin)

    def _signal(
        self,
        port: Port,
        signal: ReadyValidSignal,
        origin,
    ) -> expr.Expression:
        type_ = _field_type(port, signal)
        if _field_is_external(port, signal):
            return expr.InputRef(
                ready_valid_field_name(port.name, signal),
                type_,
                origin=origin,
            )
        key = (port.name, signal)
        if key in self._active:
            raise ProtocolSimulationLoweringError(
                "ready/valid combinational dependency is cyclic at "
                f"'{port.name}.{signal.value}'"
            )
        try:
            producer = self._producers[key]
        except KeyError as error:
            raise ProtocolSimulationLoweringError(
                f"ready/valid field '{port.name}.{signal.value}' has no exact driver"
            ) from error
        self._active.add(key)
        try:
            return self.expression(producer)
        finally:
            self._active.remove(key)


def lower_ready_valid_module(module: Module) -> Module:
    """Erase leaf ready/valid semantics into ordinary scalar wire semantics.

    The returned module is an internal compiler artifact.  Public API shape
    remains attached to the original module held by :class:`zlang.sim.Program`.
    """

    if module.elastic_pipeline_regions:
        from zlang.simulation_elastic import (
            ElasticSimulationLoweringError,
            lower_elastic_pipeline_module,
        )

        try:
            module = lower_elastic_pipeline_module(module)
        except ElasticSimulationLoweringError as error:
            raise ProtocolSimulationLoweringError(str(error)) from error

    protocol_ports = tuple(
        port for port in module.ports if port.protocol is not InterfaceProtocol.WIRE
    )
    if not protocol_ports:
        return module
    unsupported = tuple(
        port
        for port in protocol_ports
        if port.protocol is not InterfaceProtocol.READY_VALID
    )
    if unsupported:
        names = ", ".join(sorted(port.name for port in unsupported))
        raise ProtocolSimulationLoweringError(
            f"primitive simulation protocol lowering does not support: {names}"
        )
    if (
        module.request_responses
        or module.hierarchical_connections
        or module.aggregate_protocol_connections
        or module.request_response_connections
    ):
        raise ProtocolSimulationLoweringError(
            "ready/valid endpoint lowering requires a leaf module without "
            "protocol connections or elastic regions"
        )

    expanded_assignments = list(module.assignments)
    expanded_fifos = list(module.fifos)
    existing_protocol_drivers = {
        (assignment.target.name, assignment.signal)
        for assignment in module.assignments
        if isinstance(assignment.target, Port)
        and assignment.target.protocol is InterfaceProtocol.READY_VALID
        and isinstance(assignment.signal, ReadyValidSignal)
    }
    for connection in module.connections:
        if (
            connection.source.protocol is not InterfaceProtocol.READY_VALID
            or connection.destination.protocol is not InterfaceProtocol.READY_VALID
            or connection.adapter is not None
            or connection.crossing is not None
        ):
            raise ProtocolSimulationLoweringError(
                "primitive simulation supports direct and buffered "
                "ready/valid leaf connections; adapters require their "
                "protocol-specific lowering"
            )
        source = connection.source
        destination = connection.destination
        if connection.buffer_depth:
            identity = hashlib.sha256(
                (
                    f"{module.name}|{source.name}|{destination.name}|"
                    f"{connection.buffer_depth}|{source.type}"
                ).encode("utf-8")
            ).hexdigest()[:20]
            fifo_name = f"$zlang_protocol_buffer_{identity}"
            if any(fifo.name == fifo_name for fifo in expanded_fifos):
                raise ProtocolSimulationLoweringError(
                    f"buffered connection '{source.name}->{destination.name}' "
                    "has a duplicate physical FIFO identity"
                )
            destination_ready = expr.ReadyValidRef(
                destination.name,
                ReadyValidSignal.READY,
                BitType(),
            )
            pop = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                destination_ready,
                expr.FifoRef(fifo_name, FifoSignal.VALID, BitType()),
            )
            expanded_fifos.append(
                Fifo(
                    fifo_name,
                    source.type,
                    connection.buffer_depth,
                    expr.ReadyValidRef(
                        source.name,
                        ReadyValidSignal.PAYLOAD,
                        source.type,
                    ),
                    expr.ReadyValidRef(
                        source.name,
                        ReadyValidSignal.VALID,
                        BitType(),
                    ),
                    pop,
                    domain=source.domain,
                )
            )
            generated = (
                Assignment(
                    destination,
                    expr.FifoRef(fifo_name, FifoSignal.FRONT, source.type),
                    ReadyValidSignal.PAYLOAD,
                ),
                Assignment(
                    destination,
                    expr.FifoRef(fifo_name, FifoSignal.VALID, BitType()),
                    ReadyValidSignal.VALID,
                ),
                Assignment(
                    source,
                    expr.FifoRef(fifo_name, FifoSignal.READY, BitType()),
                    ReadyValidSignal.READY,
                ),
            )
        else:
            generated = (
                Assignment(
                    destination,
                    expr.ReadyValidRef(source.name, ReadyValidSignal.PAYLOAD, source.type),
                    ReadyValidSignal.PAYLOAD,
                ),
                Assignment(
                    destination,
                    expr.ReadyValidRef(source.name, ReadyValidSignal.VALID, BitType()),
                    ReadyValidSignal.VALID,
                ),
                Assignment(
                    source,
                    expr.ReadyValidRef(destination.name, ReadyValidSignal.READY, BitType()),
                    ReadyValidSignal.READY,
                ),
            )
        expanded_assignments.extend(
            assignment
            for assignment in generated
            if (assignment.target.name, assignment.signal)
            not in existing_protocol_drivers
        )

    ports = {port.name: port for port in protocol_ports}
    producers: dict[tuple[str, ReadyValidSignal], expr.Expression] = {}
    for assignment in expanded_assignments:
        target = assignment.target
        if not isinstance(target, Port) or target.protocol is InterfaceProtocol.WIRE:
            continue
        if not isinstance(assignment.signal, ReadyValidSignal):
            raise ProtocolSimulationLoweringError(
                f"ready/valid assignment '{target.name}' has no exact signal"
            )
        if assignment.signal is ReadyValidSignal.TRANSFER:
            raise ProtocolSimulationLoweringError(
                "ready/valid transfer is derived and cannot be assigned"
            )
        key = (target.name, assignment.signal)
        if key in producers:
            raise ProtocolSimulationLoweringError(
                f"ready/valid field '{target.name}.{assignment.signal.value}' "
                "has multiple drivers"
            )
        if _field_is_external(target, assignment.signal):
            raise ProtocolSimulationLoweringError(
                f"ready/valid field '{target.name}.{assignment.signal.value}' "
                "is environment-owned"
            )
        producers[key] = assignment.expression

    scalar_ports = [
        port for port in module.ports if port.protocol is InterfaceProtocol.WIRE
    ]
    scalar_by_name = {port.name: port for port in scalar_ports}
    for port in protocol_ports:
        for signal in (
            ReadyValidSignal.PAYLOAD,
            ReadyValidSignal.VALID,
            ReadyValidSignal.READY,
        ):
            external = _field_is_external(port, signal)
            name = ready_valid_field_name(port.name, signal)
            scalar = Port(
                PortDirection.INPUT if external else PortDirection.OUTPUT,
                name,
                _field_type(port, signal),
                domain=port.domain,
            )
            scalar_ports.append(scalar)
            scalar_by_name[name] = scalar

    lowerer = _ReadyValidExpressionLowerer(ports, producers)
    assignments: list[Assignment] = []
    for assignment in expanded_assignments:
        target = assignment.target
        if isinstance(target, Port) and target.protocol is not InterfaceProtocol.WIRE:
            assert isinstance(assignment.signal, ReadyValidSignal)
            scalar_name = ready_valid_field_name(target.name, assignment.signal)
            assignments.append(
                Assignment(
                    scalar_by_name[scalar_name],
                    lowerer.expression(assignment.expression),
                )
            )
        else:
            assignments.append(
                replace(
                    assignment,
                    expression=lowerer.expression(assignment.expression),
                )
            )

    rewritten = {
        "functions": lowerer.value(module.functions),
        "registers": lowerer.value(module.registers),
        "next_assignments": lowerer.value(module.next_assignments),
        "rules": lowerer.value(module.rules),
        "fifos": lowerer.value(tuple(expanded_fifos)),
        "memories": lowerer.value(module.memories),
        "roms": lowerer.value(module.roms),
        "locals": lowerer.value(module.locals),
        "instance_bindings": lowerer.value(module.instance_bindings),
        "resolved_transition": lowerer.value(module.resolved_transition),
        "callable_definitions": lowerer.value(module.callable_definitions),
        "verification_scopes": lowerer.value(module.verification_scopes),
    }
    return replace(
        module,
        ports=tuple(scalar_ports),
        assignments=tuple(assignments),
        connections=(),
        protocol_endpoints=(),
        module_signature=None,
        semantic_expression_arena_statistics=None,
        semantic_expression_provenance=None,
        **rewritten,
    )


class _CreditExpressionLowerer(SimulationExpressionRewriter):
    def __init__(
        self,
        module: Module,
        ports: dict[str, Port],
        producers: dict[tuple[str, CreditSignal], expr.Expression],
        count_registers: dict[str, Register],
    ) -> None:
        super().__init__()
        self._module = module
        self._ports = ports
        self._producers = producers
        self._count_registers = count_registers
        self._active: set[tuple[str, CreditSignal]] = set()

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if isinstance(value, expr.CreditRef):
            return self.signal(value.interface, value.signal, value.origin)
        return None

    def signal(
        self,
        endpoint: str,
        signal: CreditSignal,
        origin=None,
    ) -> expr.Expression:
        try:
            port = self._ports[endpoint]
        except KeyError as error:
            raise ProtocolSimulationLoweringError(
                f"credit reference names unknown endpoint '{endpoint}'"
            ) from error
        if signal is CreditSignal.TRANSFER:
            signal = CreditSignal.SEND
        if signal is CreditSignal.CREDITS:
            if port.direction is not PortDirection.OUTPUT:
                raise ProtocolSimulationLoweringError(
                    f"receiver credit endpoint '{endpoint}' has no sender credits"
                )
            register = self._count_registers[endpoint]
            return expr.RegisterRef(register.name, register.type, origin=origin)

        externally_owned = (
            signal in {CreditSignal.PAYLOAD, CreditSignal.SEND}
            if port.direction is PortDirection.INPUT
            else signal is CreditSignal.RETURN
        )
        if externally_owned:
            value: expr.Expression = expr.InputRef(
                credit_field_name(endpoint, signal),
                port.type if signal is CreditSignal.PAYLOAD else BitType(),
                origin=origin,
            )
            if (
                port.direction is PortDirection.INPUT
                and signal is CreditSignal.SEND
            ):
                value = _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    self.reset_deasserted(port),
                    value,
                )
            return value

        key = (endpoint, signal)
        if key in self._active:
            raise ProtocolSimulationLoweringError(
                f"credit combinational dependency is cyclic at "
                f"'{endpoint}.{signal.value}'"
            )
        try:
            source = self._producers[key]
        except KeyError as error:
            raise ProtocolSimulationLoweringError(
                f"credit field '{endpoint}.{signal.value}' has no exact driver"
            ) from error
        self._active.add(key)
        try:
            request = self.expression(source)
        finally:
            self._active.remove(key)

        if port.direction is PortDirection.OUTPUT and signal is CreditSignal.SEND:
            register = self._count_registers[endpoint]
            count = expr.RegisterRef(register.name, register.type, origin=origin)
            available = expr.Binary(
                expr.BinaryOperator.NOT_EQUAL,
                count,
                expr.Constant(0, register.type),
                register.type,
                BitType(),
                origin=origin,
            )
            return _bit_binary(
                expr.BinaryOperator.BIT_AND,
                self.reset_deasserted(port),
                _bit_binary(expr.BinaryOperator.BIT_AND, request, available),
            )
        if port.direction is PortDirection.INPUT and signal is CreditSignal.RETURN:
            return _bit_binary(
                expr.BinaryOperator.BIT_AND,
                self.reset_deasserted(port),
                request,
            )
        return request

    def reset_deasserted(self, port: Port) -> expr.Expression:
        domain = port.domain or self._module.clock
        reset = next(
            (
                candidate.reset
                for candidate in self._module.clock_domains
                if candidate.clock == domain
            ),
            self._module.reset if self._module.clock == domain else None,
        )
        if reset is None:
            raise ProtocolSimulationLoweringError(
                f"credit endpoint '{port.name}' has no owning reset"
            )
        return _bit_not(expr.InputRef(f"$reset:{reset}", BitType()))


def lower_credit_module(module: Module) -> Module:
    """Erase bounded credit endpoints into scalar fields and counter state."""

    credit_ports = tuple(
        port for port in module.ports if port.protocol is InterfaceProtocol.CREDIT
    )
    if not credit_ports:
        return module
    if any(
        port.protocol not in {InterfaceProtocol.WIRE, InterfaceProtocol.CREDIT}
        for port in module.ports
    ):
        raise ProtocolSimulationLoweringError(
            "credit endpoint lowering cannot mix unrelated protocol families"
        )
    if (
        module.connections
        or module.request_responses
        or module.hierarchical_connections
        or module.aggregate_protocol_connections
        or module.request_response_connections
    ):
        raise ProtocolSimulationLoweringError(
            "credit endpoint lowering requires a leaf module without protocol "
            "connections"
        )

    producers: dict[tuple[str, CreditSignal], expr.Expression] = {}
    for assignment in module.assignments:
        target = assignment.target
        if not isinstance(target, Port) or target.protocol is InterfaceProtocol.WIRE:
            continue
        if not isinstance(assignment.signal, CreditSignal):
            raise ProtocolSimulationLoweringError(
                f"credit assignment '{target.name}' has no exact signal"
            )
        signal = assignment.signal
        if signal in {CreditSignal.TRANSFER, CreditSignal.CREDITS}:
            raise ProtocolSimulationLoweringError(
                f"derived credit field '{target.name}.{signal.value}' cannot be assigned"
            )
        externally_owned = (
            signal in {CreditSignal.PAYLOAD, CreditSignal.SEND}
            if target.direction is PortDirection.INPUT
            else signal is CreditSignal.RETURN
        )
        if externally_owned:
            raise ProtocolSimulationLoweringError(
                f"credit field '{target.name}.{signal.value}' is environment-owned"
            )
        key = (target.name, signal)
        if key in producers:
            raise ProtocolSimulationLoweringError(
                f"credit field '{target.name}.{signal.value}' has multiple drivers"
            )
        producers[key] = assignment.expression

    count_registers: dict[str, Register] = {}
    for port in credit_ports:
        if port.capacity is None or port.capacity < 1:
            raise ProtocolSimulationLoweringError(
                f"credit endpoint '{port.name}' has no positive capacity"
            )
        type_ = UIntType(max(1, port.capacity.bit_length()))
        domain = port.domain or module.clock
        if domain is None:
            raise ProtocolSimulationLoweringError(
                f"credit endpoint '{port.name}' has no owning clock domain"
            )
        kind = "credits" if port.direction is PortDirection.OUTPUT else "occupancy"
        count_registers[port.name] = Register(
            credit_state_name(port.name, kind),
            type_,
            expr.Constant(port.capacity if kind == "credits" else 0, type_),
            domain,
        )

    ports = {port.name: port for port in credit_ports}
    lowerer = _CreditExpressionLowerer(
        module,
        ports,
        producers,
        count_registers,
    )
    scalar_ports = [
        port for port in module.ports if port.protocol is InterfaceProtocol.WIRE
    ]
    scalar_by_name = {port.name: port for port in scalar_ports}
    for port in credit_ports:
        for signal in (
            CreditSignal.PAYLOAD,
            CreditSignal.SEND,
            CreditSignal.RETURN,
        ):
            external = (
                signal in {CreditSignal.PAYLOAD, CreditSignal.SEND}
                if port.direction is PortDirection.INPUT
                else signal is CreditSignal.RETURN
            )
            name = credit_field_name(port.name, signal)
            scalar = Port(
                PortDirection.INPUT if external else PortDirection.OUTPUT,
                name,
                port.type if signal is CreditSignal.PAYLOAD else BitType(),
                domain=port.domain,
            )
            scalar_ports.append(scalar)
            scalar_by_name[name] = scalar
        if port.direction is PortDirection.OUTPUT:
            credits_name = credit_field_name(port.name, CreditSignal.CREDITS)
            credits_port = Port(
                PortDirection.OUTPUT,
                credits_name,
                count_registers[port.name].type,
                domain=port.domain,
            )
            scalar_ports.append(credits_port)
            scalar_by_name[credits_name] = credits_port

    assignments: list[Assignment] = []
    for assignment in module.assignments:
        target = assignment.target
        if isinstance(target, Port) and target.protocol is InterfaceProtocol.CREDIT:
            continue
        assignments.append(
            replace(assignment, expression=lowerer.expression(assignment.expression))
        )
    for port in credit_ports:
        driven = (
            (CreditSignal.RETURN,)
            if port.direction is PortDirection.INPUT
            else (
                CreditSignal.PAYLOAD,
                CreditSignal.SEND,
                CreditSignal.CREDITS,
            )
        )
        for signal in driven:
            assignments.append(
                Assignment(
                    scalar_by_name[credit_field_name(port.name, signal)],
                    lowerer.signal(port.name, signal),
                )
            )

    next_assignments = list(lowerer.value(module.next_assignments))
    runtime_scopes: list[VerificationScope] = []
    for port in credit_ports:
        register = count_registers[port.name]
        count = expr.RegisterRef(register.name, register.type)
        sent = lowerer.signal(port.name, CreditSignal.SEND)
        returned = lowerer.signal(port.name, CreditSignal.RETURN)
        widened_sent = expr.Extend(sent, register.type)
        widened_returned = expr.Extend(returned, register.type)
        increased = expr.Add(count, widened_returned, register.type)
        updated = expr.Binary(
            expr.BinaryOperator.SUBTRACT,
            increased,
            widened_sent,
            register.type,
            register.type,
        )
        next_assignments.append(NextAssignment(register, updated))
        at_zero = expr.Binary(
            expr.BinaryOperator.EQUAL,
            count,
            expr.Constant(0, register.type),
            register.type,
            BitType(),
        )
        at_capacity = expr.Binary(
            expr.BinaryOperator.EQUAL,
            count,
            expr.Constant(port.capacity, register.type),
            register.type,
            BitType(),
        )
        returned_without_send = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            returned,
            _bit_not(sent),
        )
        sent_without_return = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            sent,
            _bit_not(returned),
        )
        if port.direction is PortDirection.OUTPUT:
            violation = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                returned_without_send,
                at_capacity,
            )
            conditions = ((
                f"credit interface '{port.name}' overflow: return at maximum credits",
                _bit_not(violation),
            ),)
        else:
            underflow = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                returned_without_send,
                at_zero,
            )
            overflow = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                sent_without_return,
                at_capacity,
            )
            conditions = (
                (
                    f"credit interface '{port.name}' underflow: return with no "
                    "outstanding transfer",
                    _bit_not(underflow),
                ),
                (
                    f"credit interface '{port.name}' overflow: transfer at "
                    "maximum occupancy",
                    _bit_not(overflow),
                ),
            )
        runtime_scopes.append(
            _runtime_protocol_scope(module=module, port=port, conditions=conditions)
        )

    rewritten = {
        "functions": lowerer.value(module.functions),
        "registers": (*lowerer.value(module.registers), *count_registers.values()),
        "next_assignments": tuple(next_assignments),
        "rules": lowerer.value(module.rules),
        "fifos": lowerer.value(module.fifos),
        "memories": lowerer.value(module.memories),
        "roms": lowerer.value(module.roms),
        "locals": lowerer.value(module.locals),
        "instance_bindings": lowerer.value(module.instance_bindings),
        "resolved_transition": lowerer.value(module.resolved_transition),
        "callable_definitions": lowerer.value(module.callable_definitions),
        "verification_scopes": (
            *lowerer.value(module.verification_scopes),
            *runtime_scopes,
        ),
    }
    return replace(
        module,
        ports=tuple(scalar_ports),
        assignments=tuple(assignments),
        connections=(),
        protocol_endpoints=(),
        module_signature=None,
        semantic_expression_arena_statistics=None,
        semantic_expression_provenance=None,
        **rewritten,
    )


class _VirtualChannelCreditExpressionLowerer(SimulationExpressionRewriter):
    def __init__(
        self,
        module: Module,
        ports: dict[str, Port],
        producers: dict[
            tuple[str, VirtualChannelCreditSignal], expr.Expression
        ],
        count_registers: dict[str, tuple[Register, ...]],
    ) -> None:
        super().__init__()
        self._module = module
        self._ports = ports
        self._producers = producers
        self._count_registers = count_registers
        self._active: set[tuple[str, VirtualChannelCreditSignal]] = set()

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if isinstance(value, expr.VirtualChannelCreditRef):
            return self.signal(value.interface, value.signal, value.origin)
        return None

    @staticmethod
    def channel_type(port: Port) -> UIntType:
        if port.virtual_channels is None or port.virtual_channels < 1:
            raise ProtocolSimulationLoweringError(
                f"VC-credit endpoint '{port.name}' has no channel count"
            )
        return UIntType(max(1, (port.virtual_channels - 1).bit_length()))

    def _channel_match(
        self,
        selector: expr.Expression,
        port: Port,
        channel: int,
    ) -> expr.Expression:
        type_ = self.channel_type(port)
        return expr.Binary(
            expr.BinaryOperator.EQUAL,
            selector,
            expr.Constant(channel, type_),
            type_,
            BitType(),
        )

    def _selected_count(
        self,
        port: Port,
        selector: expr.Expression,
    ) -> expr.Expression:
        registers = self._count_registers[port.name]
        selected: expr.Expression = expr.RegisterRef(
            registers[0].name,
            registers[0].type,
        )
        for channel, register in enumerate(registers[1:], 1):
            selected = expr.Mux(
                self._channel_match(selector, port, channel),
                expr.RegisterRef(register.name, register.type),
                selected,
                register.type,
            )
        return selected

    def _count_vector(self, port: Port) -> expr.Expression:
        registers = self._count_registers[port.name]
        return expr.VectorConcat(
            tuple(
                expr.RegisterRef(register.name, register.type)
                for register in registers
            ),
            VecType(len(registers), registers[0].type),
        )

    def signal(
        self,
        endpoint: str,
        signal: VirtualChannelCreditSignal,
        origin=None,
    ) -> expr.Expression:
        try:
            port = self._ports[endpoint]
        except KeyError as error:
            raise ProtocolSimulationLoweringError(
                f"VC-credit reference names unknown endpoint '{endpoint}'"
            ) from error
        if signal is VirtualChannelCreditSignal.TRANSFER:
            signal = VirtualChannelCreditSignal.SEND
        if signal is VirtualChannelCreditSignal.CREDITS:
            if port.direction is not PortDirection.OUTPUT:
                raise ProtocolSimulationLoweringError(
                    f"receiver VC-credit endpoint '{endpoint}' has no sender credits"
                )
            return self._count_vector(port)

        external = (
            signal
            in {
                VirtualChannelCreditSignal.PAYLOAD,
                VirtualChannelCreditSignal.VC,
                VirtualChannelCreditSignal.SEND,
            }
            if port.direction is PortDirection.INPUT
            else signal
            in {
                VirtualChannelCreditSignal.RETURN,
                VirtualChannelCreditSignal.RETURN_VC,
            }
        )
        if external:
            if signal is VirtualChannelCreditSignal.PAYLOAD:
                type_ = port.type
            elif signal in {
                VirtualChannelCreditSignal.VC,
                VirtualChannelCreditSignal.RETURN_VC,
            }:
                type_ = self.channel_type(port)
            else:
                type_ = BitType()
            value: expr.Expression = expr.InputRef(
                vc_credit_field_name(endpoint, signal),
                type_,
                origin=origin,
            )
            if (
                port.direction is PortDirection.INPUT
                and signal is VirtualChannelCreditSignal.SEND
            ):
                value = _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    self.reset_deasserted(port),
                    value,
                )
            return value

        key = (endpoint, signal)
        if key in self._active:
            raise ProtocolSimulationLoweringError(
                f"VC-credit combinational dependency is cyclic at "
                f"'{endpoint}.{signal.value}'"
            )
        try:
            source = self._producers[key]
        except KeyError as error:
            raise ProtocolSimulationLoweringError(
                f"VC-credit field '{endpoint}.{signal.value}' has no exact driver"
            ) from error
        self._active.add(key)
        try:
            request = self.expression(source)
        finally:
            self._active.remove(key)

        if (
            port.direction is PortDirection.OUTPUT
            and signal is VirtualChannelCreditSignal.SEND
        ):
            selector = self.signal(
                endpoint,
                VirtualChannelCreditSignal.VC,
                origin,
            )
            count = self._selected_count(port, selector)
            available = expr.Binary(
                expr.BinaryOperator.NOT_EQUAL,
                count,
                expr.Constant(0, count.type),
                count.type,
                BitType(),
                origin=origin,
            )
            return _bit_binary(
                expr.BinaryOperator.BIT_AND,
                self.reset_deasserted(port),
                _bit_binary(expr.BinaryOperator.BIT_AND, request, available),
            )
        if (
            port.direction is PortDirection.INPUT
            and signal is VirtualChannelCreditSignal.RETURN
        ):
            return _bit_binary(
                expr.BinaryOperator.BIT_AND,
                self.reset_deasserted(port),
                request,
            )
        return request

    def reset_deasserted(self, port: Port) -> expr.Expression:
        domain = port.domain or self._module.clock
        reset = next(
            (
                candidate.reset
                for candidate in self._module.clock_domains
                if candidate.clock == domain
            ),
            self._module.reset if self._module.clock == domain else None,
        )
        if reset is None:
            raise ProtocolSimulationLoweringError(
                f"VC-credit endpoint '{port.name}' has no owning reset"
            )
        return _bit_not(expr.InputRef(f"$reset:{reset}", BitType()))


def lower_vc_credit_module(module: Module) -> Module:
    """Erase bounded VC-credit endpoints into scalar fields and counters."""

    vc_ports = tuple(
        port
        for port in module.ports
        if port.protocol is InterfaceProtocol.VC_CREDIT
    )
    if not vc_ports:
        return module
    if any(
        port.protocol not in {InterfaceProtocol.WIRE, InterfaceProtocol.VC_CREDIT}
        for port in module.ports
    ):
        raise ProtocolSimulationLoweringError(
            "VC-credit lowering cannot mix unrelated protocol families"
        )
    if (
        module.connections
        or module.request_responses
        or module.hierarchical_connections
        or module.aggregate_protocol_connections
        or module.request_response_connections
    ):
        raise ProtocolSimulationLoweringError(
            "VC-credit lowering requires a leaf module without protocol connections"
        )

    producers: dict[
        tuple[str, VirtualChannelCreditSignal], expr.Expression
    ] = {}
    for assignment in module.assignments:
        target = assignment.target
        if not isinstance(target, Port) or target.protocol is InterfaceProtocol.WIRE:
            continue
        if not isinstance(assignment.signal, VirtualChannelCreditSignal):
            raise ProtocolSimulationLoweringError(
                f"VC-credit assignment '{target.name}' has no exact signal"
            )
        signal = assignment.signal
        if signal in {
            VirtualChannelCreditSignal.TRANSFER,
            VirtualChannelCreditSignal.CREDITS,
        }:
            raise ProtocolSimulationLoweringError(
                f"derived VC-credit field '{target.name}.{signal.value}' "
                "cannot be assigned"
            )
        external = (
            signal
            in {
                VirtualChannelCreditSignal.PAYLOAD,
                VirtualChannelCreditSignal.VC,
                VirtualChannelCreditSignal.SEND,
            }
            if target.direction is PortDirection.INPUT
            else signal
            in {
                VirtualChannelCreditSignal.RETURN,
                VirtualChannelCreditSignal.RETURN_VC,
            }
        )
        if external:
            raise ProtocolSimulationLoweringError(
                f"VC-credit field '{target.name}.{signal.value}' is environment-owned"
            )
        key = (target.name, signal)
        if key in producers:
            raise ProtocolSimulationLoweringError(
                f"VC-credit field '{target.name}.{signal.value}' has multiple drivers"
            )
        producers[key] = assignment.expression

    count_registers: dict[str, tuple[Register, ...]] = {}
    for port in vc_ports:
        if port.capacity is None or port.capacity < 1:
            raise ProtocolSimulationLoweringError(
                f"VC-credit endpoint '{port.name}' has no positive capacity"
            )
        if port.virtual_channels is None or port.virtual_channels < 1:
            raise ProtocolSimulationLoweringError(
                f"VC-credit endpoint '{port.name}' has no channel count"
            )
        type_ = UIntType(max(1, port.capacity.bit_length()))
        domain = port.domain or module.clock
        if domain is None:
            raise ProtocolSimulationLoweringError(
                f"VC-credit endpoint '{port.name}' has no owning clock domain"
            )
        kind = (
            "credits"
            if port.direction is PortDirection.OUTPUT
            else "occupancy"
        )
        count_registers[port.name] = tuple(
            Register(
                vc_credit_state_name(port.name, kind, channel),
                type_,
                expr.Constant(port.capacity if kind == "credits" else 0, type_),
                domain,
            )
            for channel in range(port.virtual_channels)
        )

    ports = {port.name: port for port in vc_ports}
    lowerer = _VirtualChannelCreditExpressionLowerer(
        module,
        ports,
        producers,
        count_registers,
    )
    scalar_ports = [
        port for port in module.ports if port.protocol is InterfaceProtocol.WIRE
    ]
    scalar_by_name = {port.name: port for port in scalar_ports}
    for port in vc_ports:
        channel_type = lowerer.channel_type(port)
        for signal in (
            VirtualChannelCreditSignal.PAYLOAD,
            VirtualChannelCreditSignal.VC,
            VirtualChannelCreditSignal.SEND,
            VirtualChannelCreditSignal.RETURN,
            VirtualChannelCreditSignal.RETURN_VC,
        ):
            external = (
                signal
                in {
                    VirtualChannelCreditSignal.PAYLOAD,
                    VirtualChannelCreditSignal.VC,
                    VirtualChannelCreditSignal.SEND,
                }
                if port.direction is PortDirection.INPUT
                else signal
                in {
                    VirtualChannelCreditSignal.RETURN,
                    VirtualChannelCreditSignal.RETURN_VC,
                }
            )
            if signal is VirtualChannelCreditSignal.PAYLOAD:
                type_ = port.type
            elif signal in {
                VirtualChannelCreditSignal.VC,
                VirtualChannelCreditSignal.RETURN_VC,
            }:
                type_ = channel_type
            else:
                type_ = BitType()
            name = vc_credit_field_name(port.name, signal)
            scalar = Port(
                PortDirection.INPUT if external else PortDirection.OUTPUT,
                name,
                type_,
                domain=port.domain,
            )
            scalar_ports.append(scalar)
            scalar_by_name[name] = scalar
        count_field = (
            VirtualChannelCreditSignal.CREDITS.value
            if port.direction is PortDirection.OUTPUT
            else "occupancy"
        )
        count_name = vc_credit_field_name(port.name, count_field)
        count_port = Port(
            PortDirection.OUTPUT,
            count_name,
            lowerer._count_vector(port).type,
            domain=port.domain,
        )
        scalar_ports.append(count_port)
        scalar_by_name[count_name] = count_port

    assignments: list[Assignment] = []
    for assignment in module.assignments:
        target = assignment.target
        if isinstance(target, Port) and target.protocol is InterfaceProtocol.VC_CREDIT:
            continue
        assignments.append(
            replace(assignment, expression=lowerer.expression(assignment.expression))
        )
    for port in vc_ports:
        driven = (
            (
                VirtualChannelCreditSignal.RETURN,
                VirtualChannelCreditSignal.RETURN_VC,
            )
            if port.direction is PortDirection.INPUT
            else (
                VirtualChannelCreditSignal.PAYLOAD,
                VirtualChannelCreditSignal.VC,
                VirtualChannelCreditSignal.SEND,
            )
        )
        for signal in driven:
            assignments.append(
                Assignment(
                    scalar_by_name[vc_credit_field_name(port.name, signal)],
                    lowerer.signal(port.name, signal),
                )
            )
        count_field = (
            VirtualChannelCreditSignal.CREDITS.value
            if port.direction is PortDirection.OUTPUT
            else "occupancy"
        )
        assignments.append(
            Assignment(
                scalar_by_name[vc_credit_field_name(port.name, count_field)],
                lowerer._count_vector(port),
            )
        )

    next_assignments = list(lowerer.value(module.next_assignments))
    runtime_scopes: list[VerificationScope] = []
    for port in vc_ports:
        sent = lowerer.signal(port.name, VirtualChannelCreditSignal.SEND)
        returned = lowerer.signal(port.name, VirtualChannelCreditSignal.RETURN)
        vc = lowerer.signal(port.name, VirtualChannelCreditSignal.VC)
        return_vc = lowerer.signal(
            port.name,
            VirtualChannelCreditSignal.RETURN_VC,
        )
        conditions: list[tuple[str, expr.Expression]] = []
        for channel, register in enumerate(count_registers[port.name]):
            count = expr.RegisterRef(register.name, register.type)
            sent_here = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                sent,
                lowerer._channel_match(vc, port, channel),
            )
            returned_here = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                returned,
                lowerer._channel_match(return_vc, port, channel),
            )
            updated = expr.Binary(
                expr.BinaryOperator.SUBTRACT,
                expr.Add(count, expr.Extend(returned_here, register.type), register.type),
                expr.Extend(sent_here, register.type),
                register.type,
                register.type,
            )
            next_assignments.append(NextAssignment(register, updated))
            at_zero = expr.Binary(
                expr.BinaryOperator.EQUAL,
                count,
                expr.Constant(0, register.type),
                register.type,
                BitType(),
            )
            at_capacity = expr.Binary(
                expr.BinaryOperator.EQUAL,
                count,
                expr.Constant(port.capacity, register.type),
                register.type,
                BitType(),
            )
            returned_without_send = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                returned_here,
                _bit_not(sent_here),
            )
            sent_without_return = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                sent_here,
                _bit_not(returned_here),
            )
            if port.direction is PortDirection.OUTPUT:
                violation = _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    returned_without_send,
                    at_capacity,
                )
                conditions.append((
                    f"vc_credit interface '{port.name}' overflow on VC {channel}",
                    _bit_not(violation),
                ))
            else:
                underflow = _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    returned_without_send,
                    at_zero,
                )
                overflow = _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    sent_without_return,
                    at_capacity,
                )
                conditions.extend((
                    (
                        f"vc_credit interface '{port.name}' underflow on VC "
                        f"{channel}",
                        _bit_not(underflow),
                    ),
                    (
                        f"vc_credit interface '{port.name}' overflow on VC "
                        f"{channel}",
                        _bit_not(overflow),
                    ),
                ))
        runtime_scopes.append(
            _runtime_protocol_scope(
                module=module,
                port=port,
                conditions=tuple(conditions),
            )
        )

    rewritten = {
        "functions": lowerer.value(module.functions),
        "registers": (
            *lowerer.value(module.registers),
            *(register for group in count_registers.values() for register in group),
        ),
        "next_assignments": tuple(next_assignments),
        "rules": lowerer.value(module.rules),
        "fifos": lowerer.value(module.fifos),
        "memories": lowerer.value(module.memories),
        "roms": lowerer.value(module.roms),
        "locals": lowerer.value(module.locals),
        "instance_bindings": lowerer.value(module.instance_bindings),
        "resolved_transition": lowerer.value(module.resolved_transition),
        "callable_definitions": lowerer.value(module.callable_definitions),
        "verification_scopes": (
            *lowerer.value(module.verification_scopes),
            *runtime_scopes,
        ),
    }
    return replace(
        module,
        ports=tuple(scalar_ports),
        assignments=tuple(assignments),
        connections=(),
        protocol_endpoints=(),
        module_signature=None,
        semantic_expression_arena_statistics=None,
        semantic_expression_provenance=None,
        **rewritten,
    )


class _PacketExpressionLowerer(SimulationExpressionRewriter):
    def __init__(
        self,
        values: dict[tuple[str, PacketSignal], expr.Expression],
    ) -> None:
        super().__init__()
        self._values = values

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if not isinstance(value, expr.PacketRef):
            return None
        if value.signal is PacketSignal.TRANSFER:
            try:
                valid = self._values[(value.interface, PacketSignal.VALID)]
                ready = self._values[(value.interface, PacketSignal.READY)]
            except KeyError as error:
                raise ProtocolSimulationLoweringError(
                    f"packet transfer names incomplete endpoint '{value.interface}'"
                ) from error
            return _bit_binary(expr.BinaryOperator.BIT_AND, valid, ready)
        try:
            return self._values[(value.interface, value.signal)]
        except KeyError as error:
            raise ProtocolSimulationLoweringError(
                f"packet reference names unknown field "
                f"'{value.interface}.{value.signal.value}'"
            ) from error


def _packet_select(
    selector: expr.Expression,
    choices: tuple[expr.Expression, ...],
) -> expr.Expression:
    selected = choices[0]
    for index, choice in enumerate(choices[1:], 1):
        selected = expr.Mux(
            expr.Binary(
                expr.BinaryOperator.EQUAL,
                selector,
                expr.Constant(index, selector.type),
                selector.type,
                BitType(),
            ),
            choice,
            selected,
            choice.type,
        )
    return selected


def _packet_priority_candidate(
    valid: tuple[expr.Expression, ...],
    order: tuple[int, ...],
    owner_type: UIntType,
) -> tuple[expr.Expression, expr.Expression]:
    candidate: expr.Expression = expr.Constant(0, owner_type)
    candidate_valid: expr.Expression = expr.Constant(0, BitType())
    for index in reversed(order):
        candidate = expr.Mux(
            valid[index],
            expr.Constant(index, owner_type),
            candidate,
            owner_type,
        )
        candidate_valid = _bit_binary(
            expr.BinaryOperator.BIT_OR,
            valid[index],
            candidate_valid,
        )
    return candidate, candidate_valid


def lower_packet_arbiter_module(module: Module) -> Module:
    """Erase one typed packet arbiter into scalar fields and state."""

    packet_ports = tuple(
        port for port in module.ports if port.protocol is InterfaceProtocol.PACKET
    )
    if not packet_ports:
        return module
    if not module.is_sequential or len(module.arbiters) != 1:
        raise ProtocolSimulationLoweringError(
            "packet simulation requires one clocked typed arbiter"
        )
    arbiter = module.arbiters[0]
    endpoint_names = {
        *(source.name for source in arbiter.sources),
        arbiter.destination.name,
    }
    if (
        {port.name for port in module.ports} != endpoint_names
        or any(port.protocol is not InterfaceProtocol.PACKET for port in module.ports)
        or module.assignments
        or module.connections
        or module.registers
        or module.rules
        or module.fifos
        or module.memories
        or module.roms
        or module.csr_blocks
        or module.request_responses
        or module.hierarchical_connections
        or module.aggregate_protocol_connections
        or module.request_response_connections
    ):
        raise ProtocolSimulationLoweringError(
            "packet arbiter lowering requires only its typed packet endpoints"
        )
    sources = arbiter.sources
    if not sources:
        raise ProtocolSimulationLoweringError("packet arbiter has no sources")
    destination = arbiter.destination
    domain = destination.domain or module.clock
    if domain is None:
        raise ProtocolSimulationLoweringError(
            "packet arbiter destination has no owning clock domain"
        )
    owner_type = UIntType(max(1, (len(sources) - 1).bit_length()))
    reset = next(
        (
            candidate.reset
            for candidate in module.clock_domains
            if candidate.clock == domain
        ),
        module.reset if module.clock == domain else None,
    )
    if reset is None:
        raise ProtocolSimulationLoweringError("packet arbiter has no owning reset")
    reset_deasserted = _bit_not(expr.InputRef(f"$reset:{reset}", BitType()))

    active_register = Register(
        packet_state_name("grant_active"),
        BitType(),
        expr.Constant(0, BitType()),
        domain,
    )
    owner_register = Register(
        packet_state_name("grant_owner"),
        owner_type,
        expr.Constant(0, owner_type),
        domain,
    )
    priority_register = (
        Register(
            packet_state_name("next_priority"),
            owner_type,
            expr.Constant(0, owner_type),
            domain,
        )
        if arbiter.policy is ArbitrationPolicy.ROUND_ROBIN
        else None
    )
    stalled_registers = tuple(
        Register(
            packet_state_name("stalled", source.name),
            BitType(),
            expr.Constant(0, BitType()),
            domain,
        )
        for source in sources
    )
    payload_registers = tuple(
        Register(
            packet_state_name("payload", source.name),
            source.type,
            expr.Constant(0, source.type),
            domain,
        )
        for source in sources
    )
    last_registers = tuple(
        Register(
            packet_state_name("last", source.name),
            BitType(),
            expr.Constant(0, BitType()),
            domain,
        )
        for source in sources
    )

    values: dict[tuple[str, PacketSignal], expr.Expression] = {}
    source_payloads: list[expr.Expression] = []
    source_valid: list[expr.Expression] = []
    source_last: list[expr.Expression] = []
    for source in sources:
        payload = expr.InputRef(
            packet_field_name(source.name, PacketSignal.PAYLOAD),
            source.type,
        )
        valid = expr.InputRef(
            packet_field_name(source.name, PacketSignal.VALID),
            BitType(),
        )
        last = expr.InputRef(
            packet_field_name(source.name, PacketSignal.LAST),
            BitType(),
        )
        source_payloads.append(payload)
        source_valid.append(valid)
        source_last.append(last)
        values[(source.name, PacketSignal.PAYLOAD)] = payload
        values[(source.name, PacketSignal.VALID)] = valid
        values[(source.name, PacketSignal.LAST)] = last
    destination_ready = expr.InputRef(
        packet_field_name(destination.name, PacketSignal.READY),
        BitType(),
    )
    values[(destination.name, PacketSignal.READY)] = destination_ready

    valid_tuple = tuple(source_valid)
    if arbiter.policy is ArbitrationPolicy.FIXED_PRIORITY:
        candidate, candidate_valid = _packet_priority_candidate(
            valid_tuple,
            tuple(range(len(sources))),
            owner_type,
        )
    else:
        assert priority_register is not None
        priority = expr.RegisterRef(priority_register.name, priority_register.type)
        candidates = tuple(
            _packet_priority_candidate(
                valid_tuple,
                tuple((start + offset) % len(sources) for offset in range(len(sources))),
                owner_type,
            )
            for start in range(len(sources))
        )
        candidate = _packet_select(
            priority,
            tuple(item[0] for item in candidates),
        )
        candidate_valid = _packet_select(
            priority,
            tuple(item[1] for item in candidates),
        )

    active = expr.RegisterRef(active_register.name, active_register.type)
    owner = expr.RegisterRef(owner_register.name, owner_register.type)
    selected = expr.Mux(active, owner, candidate, owner_type)
    grant_available = _bit_binary(
        expr.BinaryOperator.BIT_OR,
        active,
        candidate_valid,
    )
    grant_valid = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        reset_deasserted,
        grant_available,
    )
    selected_payload = _packet_select(selected, tuple(source_payloads))
    selected_valid = _packet_select(selected, valid_tuple)
    selected_last = _packet_select(selected, tuple(source_last))
    destination_valid = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        reset_deasserted,
        _bit_binary(
            expr.BinaryOperator.BIT_AND,
            grant_valid,
            selected_valid,
        ),
    )
    destination_payload = expr.Mux(
        grant_valid,
        selected_payload,
        expr.Constant(0, destination.type),
        destination.type,
    )
    destination_last = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        grant_valid,
        selected_last,
    )
    transfer = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        destination_valid,
        destination_ready,
    )
    grant_complete = (
        transfer
        if arbiter.grant_scope is GrantScope.BEAT
        else _bit_binary(
            expr.BinaryOperator.BIT_AND,
            transfer,
            selected_last,
        )
    )
    values[(destination.name, PacketSignal.PAYLOAD)] = destination_payload
    values[(destination.name, PacketSignal.VALID)] = destination_valid
    values[(destination.name, PacketSignal.LAST)] = destination_last
    source_ready: list[expr.Expression] = []
    for index, source in enumerate(sources):
        selected_here = expr.Binary(
            expr.BinaryOperator.EQUAL,
            selected,
            expr.Constant(index, owner_type),
            owner_type,
            BitType(),
        )
        ready = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            transfer,
            selected_here,
        )
        source_ready.append(ready)
        values[(source.name, PacketSignal.READY)] = ready

    scalar_ports: list[Port] = []
    scalar_by_name: dict[str, Port] = {}
    for port in module.ports:
        for signal in (
            PacketSignal.PAYLOAD,
            PacketSignal.VALID,
            PacketSignal.READY,
            PacketSignal.LAST,
        ):
            external = (
                signal in {PacketSignal.PAYLOAD, PacketSignal.VALID, PacketSignal.LAST}
                if port.direction is PortDirection.INPUT
                else signal is PacketSignal.READY
            )
            name = packet_field_name(port.name, signal)
            scalar = Port(
                PortDirection.INPUT if external else PortDirection.OUTPUT,
                name,
                port.type if signal is PacketSignal.PAYLOAD else BitType(),
                domain=port.domain,
            )
            scalar_ports.append(scalar)
            scalar_by_name[name] = scalar
    for field, type_ in (("grant", owner_type), ("grant_valid", BitType())):
        name = packet_field_name(destination.name, field)
        scalar = Port(PortDirection.OUTPUT, name, type_, domain=destination.domain)
        scalar_ports.append(scalar)
        scalar_by_name[name] = scalar

    assignments = [
        Assignment(
            scalar_by_name[packet_field_name(endpoint, signal)],
            value,
        )
        for (endpoint, signal), value in values.items()
        if scalar_by_name[packet_field_name(endpoint, signal)].direction
        is PortDirection.OUTPUT
    ]
    assignments.extend((
        Assignment(
            scalar_by_name[packet_field_name(destination.name, "grant")],
            selected,
        ),
        Assignment(
            scalar_by_name[packet_field_name(destination.name, "grant_valid")],
            grant_valid,
        ),
    ))

    next_assignments: list[NextAssignment] = []
    acquire = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        _bit_not(active),
        candidate_valid,
    )
    next_active = expr.Mux(
        grant_complete,
        expr.Constant(0, BitType()),
        expr.Mux(
            acquire,
            expr.Constant(1, BitType()),
            active,
            BitType(),
        ),
        BitType(),
    )
    next_assignments.extend((
        NextAssignment(active_register, next_active),
        NextAssignment(
            owner_register,
            expr.Mux(acquire, selected, owner, owner_type),
        ),
    ))
    if priority_register is not None:
        priority = expr.RegisterRef(priority_register.name, priority_register.type)
        incremented = expr.Add(
            selected,
            expr.Constant(1, owner_type),
            owner_type,
        )
        wrapped = expr.Mux(
            expr.Binary(
                expr.BinaryOperator.EQUAL,
                selected,
                expr.Constant(len(sources) - 1, owner_type),
                owner_type,
                BitType(),
            ),
            expr.Constant(0, owner_type),
            incremented,
            owner_type,
        )
        next_assignments.append(
            NextAssignment(
                priority_register,
                expr.Mux(grant_complete, wrapped, priority, owner_type),
            )
        )

    conditions: list[tuple[str, expr.Expression]] = []
    for index, source in enumerate(sources):
        stalled = expr.RegisterRef(
            stalled_registers[index].name,
            stalled_registers[index].type,
        )
        previous_payload = expr.RegisterRef(
            payload_registers[index].name,
            payload_registers[index].type,
        )
        previous_last = expr.RegisterRef(
            last_registers[index].name,
            last_registers[index].type,
        )
        payload_stable = expr.Binary(
            expr.BinaryOperator.EQUAL,
            source_payloads[index],
            previous_payload,
            source.type,
            BitType(),
        )
        last_stable = expr.Binary(
            expr.BinaryOperator.EQUAL,
            source_last[index],
            previous_last,
            BitType(),
            BitType(),
        )
        stable = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            source_valid[index],
            _bit_binary(expr.BinaryOperator.BIT_AND, payload_stable, last_stable),
        )
        conditions.append((
            f"packet source '{source.name}' changed while stalled",
            _bit_binary(expr.BinaryOperator.BIT_OR, _bit_not(stalled), stable),
        ))
        next_assignments.extend((
            NextAssignment(
                stalled_registers[index],
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    source_valid[index],
                    _bit_not(source_ready[index]),
                ),
            ),
            NextAssignment(payload_registers[index], source_payloads[index]),
            NextAssignment(last_registers[index], source_last[index]),
        ))

    lowerer = _PacketExpressionLowerer(values)
    runtime_scope = _runtime_protocol_scope(
        module=module,
        port=destination,
        conditions=tuple(conditions),
    )
    registers = (
        active_register,
        owner_register,
        *((priority_register,) if priority_register is not None else ()),
        *stalled_registers,
        *payload_registers,
        *last_registers,
    )
    rewritten = {
        "functions": lowerer.value(module.functions),
        "registers": registers,
        "next_assignments": tuple(next_assignments),
        "rules": (),
        "fifos": (),
        "memories": (),
        "roms": (),
        "locals": lowerer.value(module.locals),
        "instance_bindings": (),
        "resolved_transition": None,
        "callable_definitions": lowerer.value(module.callable_definitions),
        "verification_scopes": (
            *lowerer.value(module.verification_scopes),
            runtime_scope,
        ),
    }
    return replace(
        module,
        ports=tuple(scalar_ports),
        assignments=tuple(assignments),
        arbiters=(),
        protocol_endpoints=(),
        module_signature=None,
        semantic_expression_arena_statistics=None,
        semantic_expression_provenance=None,
        **rewritten,
    )


class _RequestResponseExpressionLowerer(SimulationExpressionRewriter):
    def __init__(
        self,
        values: dict[
            tuple[str, RequestResponseChannel, ReadyValidSignal],
            expr.Expression,
        ],
    ) -> None:
        super().__init__()
        self._values = values

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if not isinstance(value, expr.RequestResponseRef):
            return None
        if value.signal is ReadyValidSignal.TRANSFER:
            try:
                valid = self._values[
                    (value.interface, value.channel, ReadyValidSignal.VALID)
                ]
                ready = self._values[
                    (value.interface, value.channel, ReadyValidSignal.READY)
                ]
            except KeyError as error:
                raise ProtocolSimulationLoweringError(
                    f"request/response transfer names incomplete channel "
                    f"'{value.interface}.{value.channel.value}'"
                ) from error
            return _bit_binary(expr.BinaryOperator.BIT_AND, valid, ready)
        try:
            return self._values[(value.interface, value.channel, value.signal)]
        except KeyError as error:
            raise ProtocolSimulationLoweringError(
                f"request/response reference names unknown field "
                f"'{value.interface}.{value.channel.value}.{value.signal.value}'"
            ) from error


def _request_response_field_type(
    interface: RequestResponseInterface,
    channel: RequestResponseChannel,
    signal: ReadyValidSignal,
):
    if signal is ReadyValidSignal.PAYLOAD:
        return (
            interface.request_type
            if channel is RequestResponseChannel.REQUEST
            else interface.response_type
        )
    return BitType()


def _request_response_external(
    interface: RequestResponseInterface,
    channel: RequestResponseChannel,
    signal: ReadyValidSignal,
) -> bool:
    requester = interface.role is RequestResponseRole.REQUESTER
    if requester:
        return (
            channel is RequestResponseChannel.REQUEST
            and signal is ReadyValidSignal.READY
        ) or (
            channel is RequestResponseChannel.RESPONSE
            and signal in {ReadyValidSignal.PAYLOAD, ReadyValidSignal.VALID}
        )
    return (
        channel is RequestResponseChannel.REQUEST
        and signal in {ReadyValidSignal.PAYLOAD, ReadyValidSignal.VALID}
    ) or (
        channel is RequestResponseChannel.RESPONSE
        and signal is ReadyValidSignal.READY
    )


def lower_request_response_module(module: Module) -> Module:
    """Erase standalone request/response ledgers into primitive state."""

    interfaces = module.request_responses
    if not interfaces:
        return module
    if any(
        interface.ordering is RequestResponseOrdering.OUT_OF_ORDER
        and (
            interface.role is not RequestResponseRole.REQUESTER
            or interface.match_by is None
            or interface.id_type is None
        )
        for interface in interfaces
    ):
        raise ProtocolSimulationLoweringError(
            "out-of-order request/response lowering requires a requester with "
            "an exact match field"
        )
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        raise ProtocolSimulationLoweringError(
            "request/response lowering cannot mix unrelated protocol ports"
        )
    if (
        module.connections
        or module.hierarchical_connections
        or module.aggregate_protocol_connections
        or module.request_response_connections
    ):
        raise ProtocolSimulationLoweringError(
            "standalone request/response lowering cannot consume hierarchy links"
        )

    producers: dict[
        tuple[str, RequestResponseChannel, ReadyValidSignal],
        expr.Expression,
    ] = {}
    retained_assignments: list[Assignment] = []
    for assignment in module.assignments:
        if not isinstance(assignment.target, RequestResponseInterface):
            retained_assignments.append(assignment)
            continue
        if assignment.channel is None or not isinstance(
            assignment.signal,
            ReadyValidSignal,
        ):
            raise ProtocolSimulationLoweringError(
                f"request/response assignment '{assignment.target.name}' has "
                "no exact channel signal"
            )
        if assignment.signal is ReadyValidSignal.TRANSFER:
            raise ProtocolSimulationLoweringError(
                "derived request/response transfer cannot be assigned"
            )
        key = (
            assignment.target.name,
            assignment.channel,
            assignment.signal,
        )
        if _request_response_external(
            assignment.target,
            assignment.channel,
            assignment.signal,
        ):
            raise ProtocolSimulationLoweringError(
                f"request/response field '{assignment.target.name}."
                f"{assignment.channel.value}.{assignment.signal.value}' is "
                "environment-owned"
            )
        if key in producers:
            raise ProtocolSimulationLoweringError(
                f"request/response field '{assignment.target.name}."
                f"{assignment.channel.value}.{assignment.signal.value}' has "
                "multiple drivers"
            )
        producers[key] = assignment.expression

    values: dict[
        tuple[str, RequestResponseChannel, ReadyValidSignal],
        expr.Expression,
    ] = {}
    scalar_ports = list(module.ports)
    scalar_by_name = {port.name: port for port in scalar_ports}
    count_registers: dict[str, Register] = {}
    id_registers: dict[str, tuple[Register, ...]] = {}
    id_valid_registers: dict[str, tuple[Register, ...]] = {}
    for interface in interfaces:
        if interface.max_outstanding < 1:
            raise ProtocolSimulationLoweringError(
                f"request/response interface '{interface.name}' has no capacity"
            )
        for channel in RequestResponseChannel:
            for signal in (
                ReadyValidSignal.PAYLOAD,
                ReadyValidSignal.VALID,
                ReadyValidSignal.READY,
            ):
                external = _request_response_external(interface, channel, signal)
                name = request_response_field_name(interface.name, channel, signal)
                type_ = _request_response_field_type(interface, channel, signal)
                scalar = Port(
                    PortDirection.INPUT if external else PortDirection.OUTPUT,
                    name,
                    type_,
                    domain=module.clock,
                )
                scalar_ports.append(scalar)
                scalar_by_name[name] = scalar
                if external:
                    values[(interface.name, channel, signal)] = expr.InputRef(
                        name,
                        type_,
                    )
        count_type = UIntType(max(1, interface.max_outstanding.bit_length()))
        count_registers[interface.name] = Register(
            request_response_state_name(interface.name, "outstanding"),
            count_type,
            expr.Constant(0, count_type),
            module.clock,
        )
        if interface.ordering is RequestResponseOrdering.OUT_OF_ORDER:
            assert interface.id_type is not None
            id_registers[interface.name] = tuple(
                Register(
                    request_response_state_name(interface.name, f"id:{index}"),
                    interface.id_type,
                    expr.Constant(0, interface.id_type),
                    module.clock,
                )
                for index in range(interface.max_outstanding)
            )
            id_valid_registers[interface.name] = tuple(
                Register(
                    request_response_state_name(
                        interface.name,
                        f"id_valid:{index}",
                    ),
                    BitType(),
                    expr.Constant(0, BitType()),
                    module.clock,
                )
                for index in range(interface.max_outstanding)
            )
        count_name = request_response_field_name(
            interface.name,
            "ledger",
            "outstanding",
        )
        count_port = Port(
            PortDirection.OUTPUT,
            count_name,
            count_type,
            domain=module.clock,
        )
        scalar_ports.append(count_port)
        scalar_by_name[count_name] = count_port

    lowerer = _RequestResponseExpressionLowerer(values)
    next_assignments = list(lowerer.value(module.next_assignments))
    generated_assignments: list[Assignment] = []
    runtime_scopes: list[VerificationScope] = []
    for interface in interfaces:
        count_register = count_registers[interface.name]
        count = expr.RegisterRef(count_register.name, count_register.type)
        has_capacity = expr.Binary(
            expr.BinaryOperator.LESS,
            count,
            expr.Constant(interface.max_outstanding, count_register.type),
            count_register.type,
            BitType(),
        )
        has_outstanding = expr.Binary(
            expr.BinaryOperator.NOT_EQUAL,
            count,
            expr.Constant(0, count_register.type),
            count_register.type,
            BitType(),
        )

        def raw(
            channel: RequestResponseChannel,
            signal: ReadyValidSignal,
        ) -> expr.Expression:
            key = (interface.name, channel, signal)
            try:
                source = producers[key]
            except KeyError as error:
                raise ProtocolSimulationLoweringError(
                    f"request/response field '{interface.name}."
                    f"{channel.value}.{signal.value}' has no exact driver"
                ) from error
            return lowerer.expression(source)

        reset_deasserted = _bit_not(
            expr.InputRef(f"$reset:{module.reset}", BitType())
        )
        if interface.role is RequestResponseRole.REQUESTER:
            request_payload = raw(
                RequestResponseChannel.REQUEST,
                ReadyValidSignal.PAYLOAD,
            )
            values[(interface.name, RequestResponseChannel.REQUEST, ReadyValidSignal.PAYLOAD)] = request_payload
            request_valid = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                reset_deasserted,
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    has_capacity,
                    raw(RequestResponseChannel.REQUEST, ReadyValidSignal.VALID),
                ),
            )
            values[(interface.name, RequestResponseChannel.REQUEST, ReadyValidSignal.VALID)] = request_valid
            request_ready = values[(interface.name, RequestResponseChannel.REQUEST, ReadyValidSignal.READY)]
            request_transfer = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                request_valid,
                request_ready,
            )
            response_available = (
                has_outstanding
                if interface.ordering is RequestResponseOrdering.OUT_OF_ORDER
                else _bit_binary(
                    expr.BinaryOperator.BIT_OR,
                    has_outstanding,
                    request_transfer,
                )
            )
            response_ready = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                reset_deasserted,
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    response_available,
                    raw(RequestResponseChannel.RESPONSE, ReadyValidSignal.READY),
                ),
            )
            values[(interface.name, RequestResponseChannel.RESPONSE, ReadyValidSignal.READY)] = response_ready
            response_valid = values[(interface.name, RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID)]
            response_transfer = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                response_valid,
                response_ready,
            )
            if interface.ordering is RequestResponseOrdering.OUT_OF_ORDER:
                assert interface.match_by is not None
                assert interface.id_type is not None
                response_payload = values[
                    (
                        interface.name,
                        RequestResponseChannel.RESPONSE,
                        ReadyValidSignal.PAYLOAD,
                    )
                ]
                request_id = expr.FieldAccess(
                    request_payload,
                    interface.match_by,
                    interface.id_type,
                )
                response_id = expr.FieldAccess(
                    response_payload,
                    interface.match_by,
                    interface.id_type,
                )
                request_present: expr.Expression = expr.Constant(0, BitType())
                response_present: expr.Expression = expr.Constant(0, BitType())
                valid_after_remove: list[expr.Expression] = []
                for id_register, valid_register in zip(
                    id_registers[interface.name],
                    id_valid_registers[interface.name],
                    strict=True,
                ):
                    stored_id = expr.RegisterRef(
                        id_register.name,
                        id_register.type,
                    )
                    stored_valid = expr.RegisterRef(
                        valid_register.name,
                        valid_register.type,
                    )
                    request_match = _bit_binary(
                        expr.BinaryOperator.BIT_AND,
                        stored_valid,
                        expr.Binary(
                            expr.BinaryOperator.EQUAL,
                            stored_id,
                            request_id,
                            interface.id_type,
                            BitType(),
                        ),
                    )
                    response_match = _bit_binary(
                        expr.BinaryOperator.BIT_AND,
                        stored_valid,
                        expr.Binary(
                            expr.BinaryOperator.EQUAL,
                            stored_id,
                            response_id,
                            interface.id_type,
                            BitType(),
                        ),
                    )
                    request_present = _bit_binary(
                        expr.BinaryOperator.BIT_OR,
                        request_present,
                        request_match,
                    )
                    response_present = _bit_binary(
                        expr.BinaryOperator.BIT_OR,
                        response_present,
                        response_match,
                    )
                    remove_here = _bit_binary(
                        expr.BinaryOperator.BIT_AND,
                        response_transfer,
                        response_match,
                    )
                    valid_after_remove.append(
                        _bit_binary(
                            expr.BinaryOperator.BIT_AND,
                            stored_valid,
                            _bit_not(remove_here),
                        )
                    )
                duplicate = _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    request_transfer,
                    request_present,
                )
                missing = _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    response_transfer,
                    _bit_not(response_present),
                )
                runtime_scopes.append(
                    _runtime_protocol_scope(
                        module=module,
                        port=Port(
                            PortDirection.OUTPUT,
                            interface.name,
                            interface.request_type,
                            domain=module.clock,
                        ),
                        conditions=(
                            (
                                f"request/response interface '{interface.name}' "
                                "issued duplicate outstanding ID",
                                _bit_not(duplicate),
                            ),
                            (
                                f"request/response interface '{interface.name}' "
                                "received response for non-outstanding ID",
                                _bit_not(missing),
                            ),
                        ),
                    )
                )
                inserted: expr.Expression = expr.Constant(0, BitType())
                for index, (id_register, valid_register) in enumerate(zip(
                    id_registers[interface.name],
                    id_valid_registers[interface.name],
                    strict=True,
                )):
                    insert_here = _bit_binary(
                        expr.BinaryOperator.BIT_AND,
                        request_transfer,
                        _bit_binary(
                            expr.BinaryOperator.BIT_AND,
                            _bit_not(inserted),
                            _bit_not(valid_after_remove[index]),
                        ),
                    )
                    next_assignments.extend((
                        NextAssignment(
                            valid_register,
                            _bit_binary(
                                expr.BinaryOperator.BIT_OR,
                                valid_after_remove[index],
                                insert_here,
                            ),
                        ),
                        NextAssignment(
                            id_register,
                            expr.Mux(
                                insert_here,
                                request_id,
                                expr.RegisterRef(id_register.name, id_register.type),
                                id_register.type,
                            ),
                        ),
                    ))
                    inserted = _bit_binary(
                        expr.BinaryOperator.BIT_OR,
                        inserted,
                        insert_here,
                    )
        else:
            request_ready = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                reset_deasserted,
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    has_capacity,
                    raw(RequestResponseChannel.REQUEST, ReadyValidSignal.READY),
                ),
            )
            values[(interface.name, RequestResponseChannel.REQUEST, ReadyValidSignal.READY)] = request_ready
            request_valid = values[(interface.name, RequestResponseChannel.REQUEST, ReadyValidSignal.VALID)]
            request_transfer = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                request_valid,
                request_ready,
            )
            values[(interface.name, RequestResponseChannel.RESPONSE, ReadyValidSignal.PAYLOAD)] = raw(
                RequestResponseChannel.RESPONSE,
                ReadyValidSignal.PAYLOAD,
            )
            response_valid = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                reset_deasserted,
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    _bit_binary(
                        expr.BinaryOperator.BIT_OR,
                        has_outstanding,
                        request_transfer,
                    ),
                    raw(RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID),
                ),
            )
            values[(interface.name, RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID)] = response_valid
            response_ready = values[(interface.name, RequestResponseChannel.RESPONSE, ReadyValidSignal.READY)]
            response_transfer = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                response_valid,
                response_ready,
            )

        widened_request = expr.Extend(request_transfer, count_register.type)
        widened_response = expr.Extend(response_transfer, count_register.type)
        next_count = expr.Binary(
            expr.BinaryOperator.SUBTRACT,
            expr.Add(count, widened_request, count_register.type),
            widened_response,
            count_register.type,
            count_register.type,
        )
        next_assignments.append(NextAssignment(count_register, next_count))
        for channel in RequestResponseChannel:
            for signal in (
                ReadyValidSignal.PAYLOAD,
                ReadyValidSignal.VALID,
                ReadyValidSignal.READY,
            ):
                name = request_response_field_name(interface.name, channel, signal)
                port = scalar_by_name[name]
                if port.direction is PortDirection.OUTPUT:
                    generated_assignments.append(
                        Assignment(port, values[(interface.name, channel, signal)])
                    )
        count_name = request_response_field_name(
            interface.name,
            "ledger",
            "outstanding",
        )
        generated_assignments.append(
            Assignment(scalar_by_name[count_name], count)
        )

    lowerer = _RequestResponseExpressionLowerer(values)
    assignments = [
        replace(assignment, expression=lowerer.expression(assignment.expression))
        for assignment in retained_assignments
    ]
    assignments.extend(generated_assignments)
    rewritten = {
        "functions": lowerer.value(module.functions),
        "registers": (
            *lowerer.value(module.registers),
            *count_registers.values(),
            *(register for group in id_registers.values() for register in group),
            *(
                register
                for group in id_valid_registers.values()
                for register in group
            ),
        ),
        "next_assignments": tuple(next_assignments),
        "rules": lowerer.value(module.rules),
        "fifos": lowerer.value(module.fifos),
        "memories": lowerer.value(module.memories),
        "roms": lowerer.value(module.roms),
        "locals": lowerer.value(module.locals),
        "instance_bindings": lowerer.value(module.instance_bindings),
        "resolved_transition": lowerer.value(module.resolved_transition),
        "callable_definitions": lowerer.value(module.callable_definitions),
        "verification_scopes": (
            *lowerer.value(module.verification_scopes),
            *runtime_scopes,
        ),
    }
    return replace(
        module,
        ports=tuple(scalar_ports),
        assignments=tuple(assignments),
        request_responses=(),
        protocol_endpoints=(),
        module_signature=None,
        semantic_expression_arena_statistics=None,
        semantic_expression_provenance=None,
        **rewritten,
    )


class _ProtocolFieldExpressionLowerer(SimulationExpressionRewriter):
    def __init__(
        self,
        values: dict[tuple[str, str, str], expr.Expression],
    ) -> None:
        super().__init__()
        self._values = values

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if isinstance(value, expr.ReadyValidRef):
            signal = (
                ReadyValidSignal.VALID
                if value.signal is ReadyValidSignal.TRANSFER
                else value.signal
            )
            result = self._values.get(("ready_valid", value.interface, signal.value))
            if result is None:
                raise ProtocolSimulationLoweringError(
                    f"adapter reference names unknown ready/valid field "
                    f"'{value.interface}.{signal.value}'"
                )
            if value.signal is ReadyValidSignal.TRANSFER:
                ready = self._values.get(
                    ("ready_valid", value.interface, ReadyValidSignal.READY.value)
                )
                if ready is None:
                    raise ProtocolSimulationLoweringError(
                        f"adapter transfer has no ready field for '{value.interface}'"
                    )
                return _bit_binary(expr.BinaryOperator.BIT_AND, result, ready)
            return result
        if isinstance(value, expr.CreditRef):
            signal = (
                CreditSignal.SEND
                if value.signal is CreditSignal.TRANSFER
                else value.signal
            )
            result = self._values.get(("credit", value.interface, signal.value))
            if result is None:
                raise ProtocolSimulationLoweringError(
                    f"adapter reference names unknown credit field "
                    f"'{value.interface}.{signal.value}'"
                )
            return result
        return None


def _adapter_scalar_ports(module: Module) -> tuple[tuple[Port, ...], dict[str, Port]]:
    scalar_ports = [
        port for port in module.ports if port.protocol is InterfaceProtocol.WIRE
    ]
    by_name = {port.name: port for port in scalar_ports}
    for port in module.ports:
        if port.protocol is InterfaceProtocol.WIRE:
            continue
        if port.protocol is InterfaceProtocol.READY_VALID:
            fields = (
                (ReadyValidSignal.PAYLOAD.value, port.type),
                (ReadyValidSignal.VALID.value, BitType()),
                (ReadyValidSignal.READY.value, BitType()),
            )
            forward = {ReadyValidSignal.PAYLOAD.value, ReadyValidSignal.VALID.value}
            name_for = ready_valid_field_name
        elif port.protocol is InterfaceProtocol.CREDIT:
            fields = (
                (CreditSignal.PAYLOAD.value, port.type),
                (CreditSignal.SEND.value, BitType()),
                (CreditSignal.RETURN.value, BitType()),
            )
            forward = {CreditSignal.PAYLOAD.value, CreditSignal.SEND.value}
            name_for = credit_field_name
        else:
            raise ProtocolSimulationLoweringError(
                f"adapter does not support protocol '{port.protocol.value}'"
            )
        for field, type_ in fields:
            module_owned = (
                field not in forward
                if port.direction is PortDirection.INPUT
                else field in forward
            )
            name = name_for(port.name, field)
            scalar = Port(
                PortDirection.OUTPUT if module_owned else PortDirection.INPUT,
                name,
                type_,
                domain=port.domain,
            )
            scalar_ports.append(scalar)
            by_name[name] = scalar
    return tuple(scalar_ports), by_name


def lower_adapter_module(module: Module) -> Module:
    """Erase one exact ready/valid-credit adapter to counters or FIFO state."""

    if len(module.connections) != 1:
        raise ProtocolSimulationLoweringError(
            "primitive protocol adapter lowering requires exactly one connection"
        )
    if (
        module.registers
        or module.next_assignments
        or module.rules
        or module.fifos
        or module.memories
        or module.roms
        or module.csr_blocks
        or module.elaborated_instances
        or module.instances
        or module.children
    ):
        raise ProtocolSimulationLoweringError(
            "primitive protocol adapter lowering requires one closed adapter"
        )
    connection = module.connections[0]
    source = connection.source
    destination = connection.destination
    if connection.crossing is not None or connection.adapter is None:
        raise ProtocolSimulationLoweringError(
            "protocol adapter lowering requires one same-domain typed adapter"
        )
    if source.domain != destination.domain:
        raise ProtocolSimulationLoweringError(
            "protocol adapter endpoints must share one clock domain"
        )
    domain = source.domain or module.clock
    reset = next(
        (
            candidate.reset
            for candidate in module.clock_domains
            if candidate.clock == domain
        ),
        module.reset if module.clock == domain else None,
    )
    if domain is None or reset is None:
        raise ProtocolSimulationLoweringError(
            "protocol adapter requires one exact clock/reset domain"
        )
    scalar_ports, scalar_by_name = _adapter_scalar_ports(module)
    reset_deasserted = _bit_not(expr.InputRef(f"$reset:{reset}", BitType()))
    values: dict[tuple[str, str, str], expr.Expression] = {}

    def scalar_input(port: Port, field: str, type_) -> expr.InputRef:
        name = (
            ready_valid_field_name(port.name, field)
            if port.protocol is InterfaceProtocol.READY_VALID
            else credit_field_name(port.name, field)
        )
        return expr.InputRef(name, type_)

    registers = list(module.registers)
    next_assignments = list(module.next_assignments)
    fifos = list(module.fifos)
    runtime_scopes: list[VerificationScope] = []
    internal_outputs: list[tuple[Port, expr.Expression]] = []

    if connection.adapter is ConnectionAdapter.READY_VALID_TO_CREDIT:
        if (
            source.protocol is not InterfaceProtocol.READY_VALID
            or source.direction is not PortDirection.INPUT
            or destination.protocol is not InterfaceProtocol.CREDIT
            or destination.direction is not PortDirection.OUTPUT
            or destination.capacity is None
            or connection.buffer_depth
        ):
            raise ProtocolSimulationLoweringError(
                "invalid typed ready/valid-to-credit adapter"
            )
        count_type = UIntType(max(1, destination.capacity.bit_length()))
        count_register = Register(
            credit_state_name(destination.name, "credits"),
            count_type,
            expr.Constant(destination.capacity, count_type),
            domain,
        )
        registers.append(count_register)
        count = expr.RegisterRef(count_register.name, count_type)
        payload = scalar_input(source, "payload", source.type)
        valid = scalar_input(source, "valid", BitType())
        returned = scalar_input(destination, "return", BitType())
        available = expr.Binary(
            expr.BinaryOperator.NOT_EQUAL,
            count,
            expr.Constant(0, count_type),
            count_type,
            BitType(),
        )
        ready = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            reset_deasserted,
            available,
        )
        sent = _bit_binary(expr.BinaryOperator.BIT_AND, valid, ready)
        values.update({
            ("ready_valid", source.name, "payload"): payload,
            ("ready_valid", source.name, "valid"): valid,
            ("ready_valid", source.name, "ready"): ready,
            ("credit", destination.name, "payload"): payload,
            ("credit", destination.name, "send"): sent,
            ("credit", destination.name, "return"): returned,
            ("credit", destination.name, "credits"): count,
        })
        updated = expr.Binary(
            expr.BinaryOperator.SUBTRACT,
            expr.Add(count, expr.Extend(returned, count_type), count_type),
            expr.Extend(sent, count_type),
            count_type,
            count_type,
        )
        next_assignments.append(NextAssignment(count_register, updated))
        violation = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            _bit_binary(
                expr.BinaryOperator.BIT_AND,
                returned,
                _bit_not(sent),
            ),
            expr.Binary(
                expr.BinaryOperator.EQUAL,
                count,
                expr.Constant(destination.capacity, count_type),
                count_type,
                BitType(),
            ),
        )
        runtime_scopes.append(
            _runtime_protocol_scope(
                module=module,
                port=destination,
                conditions=((
                    f"rv_to_credit adapter '{source.name}->{destination.name}' "
                    "received a return at maximum credits",
                    _bit_not(violation),
                ),),
            )
        )
    elif connection.adapter is ConnectionAdapter.CREDIT_TO_READY_VALID:
        if (
            source.protocol is not InterfaceProtocol.CREDIT
            or source.direction is not PortDirection.INPUT
            or destination.protocol is not InterfaceProtocol.READY_VALID
            or destination.direction is not PortDirection.OUTPUT
            or source.capacity is None
            or connection.buffer_depth != source.capacity
        ):
            raise ProtocolSimulationLoweringError(
                "invalid typed credit-to-ready/valid adapter"
            )
        identity = hashlib.sha256(
            (
                f"{module.name}|{source.name}|{destination.name}|"
                f"{connection.buffer_depth}|credit-to-rv"
            ).encode("utf-8")
        ).hexdigest()[:20]
        fifo_name = f"$zlang_protocol_adapter_{identity}"
        payload = scalar_input(source, "payload", source.type)
        sent = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            reset_deasserted,
            scalar_input(source, "send", BitType()),
        )
        ready = scalar_input(destination, "ready", BitType())
        accepted = expr.FifoRef(fifo_name, FifoSignal.READY, BitType())
        push = _bit_binary(expr.BinaryOperator.BIT_AND, sent, accepted)
        fifo = Fifo(
            fifo_name,
            source.type,
            connection.buffer_depth,
            payload,
            push,
            ready,
            domain=domain,
        )
        fifos.append(fifo)
        valid = expr.FifoRef(fifo_name, FifoSignal.VALID, BitType())
        returned = _bit_binary(expr.BinaryOperator.BIT_AND, valid, ready)
        values.update({
            ("credit", source.name, "payload"): payload,
            ("credit", source.name, "send"): sent,
            ("credit", source.name, "return"): returned,
            ("ready_valid", destination.name, "payload"): expr.FifoRef(
                fifo_name, FifoSignal.FRONT, source.type
            ),
            ("ready_valid", destination.name, "valid"): valid,
            ("ready_valid", destination.name, "ready"): ready,
        })
        overflow_name = f"{fifo_name}:overflow"
        overflow_port = Port(
            PortDirection.OUTPUT,
            overflow_name,
            BitType(),
            domain=domain,
        )
        scalar_ports = (*scalar_ports, overflow_port)
        overflow = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            sent,
            _bit_not(accepted),
        )
        internal_outputs.append((overflow_port, overflow))
        runtime_scopes.append(
            _runtime_protocol_scope(
                module=module,
                port=source,
                conditions=((
                    f"credit_to_rv adapter '{source.name}->{destination.name}' "
                    "received a transfer without credit",
                    _bit_not(expr.InputRef(overflow_name, BitType())),
                ),),
            )
        )
    else:
        raise ProtocolSimulationLoweringError(
            f"unsupported protocol adapter '{connection.adapter.value}'"
        )

    lowerer = _ProtocolFieldExpressionLowerer(values)
    assignments: list[Assignment] = []
    for assignment in module.assignments:
        if (
            isinstance(assignment.target, Port)
            and assignment.target.protocol is not InterfaceProtocol.WIRE
        ):
            continue
        assignments.append(
            replace(assignment, expression=lowerer.expression(assignment.expression))
        )
    for (protocol, endpoint, field), value in values.items():
        name = (
            ready_valid_field_name(endpoint, field)
            if protocol == "ready_valid"
            else credit_field_name(endpoint, field)
        )
        target = scalar_by_name.get(name)
        if target is not None and target.direction is PortDirection.OUTPUT:
            assignments.append(Assignment(target, value))
    assignments.extend(
        Assignment(target, value) for target, value in internal_outputs
    )
    if connection.adapter is ConnectionAdapter.READY_VALID_TO_CREDIT:
        credits_name = credit_field_name(destination.name, CreditSignal.CREDITS)
        credits_port = Port(
            PortDirection.OUTPUT,
            credits_name,
            registers[-1].type,
            domain=destination.domain,
        )
        scalar_ports = (*scalar_ports, credits_port)
        assignments.append(
            Assignment(
                credits_port,
                values[("credit", destination.name, "credits")],
            )
        )

    return replace(
        module,
        ports=scalar_ports,
        assignments=tuple(assignments),
        registers=tuple(lowerer.value(tuple(registers))),
        next_assignments=tuple(lowerer.value(tuple(next_assignments))),
        rules=lowerer.value(module.rules),
        fifos=tuple(lowerer.value(tuple(fifos))),
        memories=lowerer.value(module.memories),
        roms=lowerer.value(module.roms),
        locals=lowerer.value(module.locals),
        functions=lowerer.value(module.functions),
        callable_definitions=lowerer.value(module.callable_definitions),
        verification_scopes=(
            *lowerer.value(module.verification_scopes),
            *runtime_scopes,
        ),
        connections=(),
        protocol_endpoints=(),
        module_signature=None,
        semantic_expression_arena_statistics=None,
        semantic_expression_provenance=None,
    )


def lower_protocol_module(module: Module) -> Module:
    """Erase the supported leaf protocol family before primitive planning."""

    protocols = {
        port.protocol
        for port in module.ports
        if port.protocol is not InterfaceProtocol.WIRE
    }
    if not protocols:
        return module
    if protocols == {InterfaceProtocol.READY_VALID}:
        return lower_ready_valid_module(module)
    if protocols == {InterfaceProtocol.CREDIT}:
        return lower_credit_module(module)
    if protocols == {InterfaceProtocol.VC_CREDIT}:
        return lower_vc_credit_module(module)
    if protocols == {InterfaceProtocol.PACKET}:
        return lower_packet_arbiter_module(module)
    if module.connections and any(
        connection.adapter is not None for connection in module.connections
    ):
        return lower_adapter_module(module)
    rendered = ", ".join(sorted(item.value for item in protocols))
    raise ProtocolSimulationLoweringError(
        f"primitive simulation protocol lowering does not support: {rendered}"
    )


def lower_ready_valid_hierarchy(module: Module) -> Module:
    """Erase direct/buffered ready/valid hierarchy into primitive state."""

    if not (module.elaborated_instances or module.instances or module.children):
        return lower_ready_valid_module(module)
    if len(module.elaborated_instances) != len(module.children):
        raise ProtocolSimulationLoweringError(
            f"module '{module.name}' has incomplete elaborated hierarchy"
        )
    lowered_children = tuple(
        lower_ready_valid_hierarchy(child) for child in module.children
    )
    child_names = {item.instance.name for item in module.elaborated_instances}
    top_ports = {port.name: port for port in module.ports}
    assignments = list(module.assignments)
    bindings = list(module.instance_bindings)
    fifos = list(module.fifos)

    def source_value(owner: str, name: str, signal: ReadyValidSignal, type_):
        if owner == module.name:
            return expr.ReadyValidRef(name, signal, type_)
        if owner not in child_names:
            raise ProtocolSimulationLoweringError(
                f"unknown protocol source owner '{owner}'"
            )
        return expr.InstanceOutputRef(
            owner,
            ready_valid_field_name(name, signal),
            type_,
        )

    def drive(
        owner: str,
        name: str,
        signal: ReadyValidSignal,
        value: expr.Expression,
    ) -> None:
        if owner == module.name:
            try:
                target = top_ports[name]
            except KeyError as error:
                raise ProtocolSimulationLoweringError(
                    f"unknown top protocol endpoint '{name}'"
                ) from error
            assignments.append(Assignment(target, value, signal))
            return
        if owner not in child_names:
            raise ProtocolSimulationLoweringError(
                f"unknown protocol destination owner '{owner}'"
            )
        bindings.append(
            InstancePortBinding(
                owner,
                ready_valid_field_name(name, signal),
                value,
            )
        )

    for connection in module.hierarchical_connections:
        if (
            connection.source.protocol is not InterfaceProtocol.READY_VALID
            or connection.destination.protocol is not InterfaceProtocol.READY_VALID
            or connection.request_buffer_depth
            or connection.response_buffer_depth
            or connection.adapter is not None
            or connection.crossing is not None
        ):
            raise ProtocolSimulationLoweringError(
                "primitive hierarchy supports only direct or buffered "
                "ready/valid connections"
            )
        source = connection.source
        destination = connection.destination
        if connection.buffer_depth:
            identity = hashlib.sha256(
                (
                    f"{module.name}|{source.owner}.{source.name}|"
                    f"{destination.owner}.{destination.name}|"
                    f"{connection.buffer_depth}|{source.payload_type}"
                ).encode("utf-8")
            ).hexdigest()[:20]
            fifo_name = f"$zlang_protocol_buffer_{identity}"
            if any(fifo.name == fifo_name for fifo in fifos):
                raise ProtocolSimulationLoweringError(
                    "hierarchical buffered connection has a duplicate physical "
                    "FIFO identity"
                )
            destination_ready = source_value(
                destination.owner,
                destination.name,
                ReadyValidSignal.READY,
                BitType(),
            )
            pop = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                destination_ready,
                expr.FifoRef(fifo_name, FifoSignal.VALID, BitType()),
            )
            fifos.append(
                Fifo(
                    fifo_name,
                    source.payload_type,
                    connection.buffer_depth,
                    source_value(
                        source.owner,
                        source.name,
                        ReadyValidSignal.PAYLOAD,
                        source.payload_type,
                    ),
                    source_value(
                        source.owner,
                        source.name,
                        ReadyValidSignal.VALID,
                        BitType(),
                    ),
                    pop,
                    domain=source.domain,
                )
            )
            drive(
                destination.owner,
                destination.name,
                ReadyValidSignal.PAYLOAD,
                expr.FifoRef(fifo_name, FifoSignal.FRONT, source.payload_type),
            )
            drive(
                destination.owner,
                destination.name,
                ReadyValidSignal.VALID,
                expr.FifoRef(fifo_name, FifoSignal.VALID, BitType()),
            )
            drive(
                source.owner,
                source.name,
                ReadyValidSignal.READY,
                expr.FifoRef(fifo_name, FifoSignal.READY, BitType()),
            )
            continue
        for signal, type_ in (
            (ReadyValidSignal.PAYLOAD, source.payload_type),
            (ReadyValidSignal.VALID, BitType()),
        ):
            drive(
                destination.owner,
                destination.name,
                signal,
                source_value(source.owner, source.name, signal, type_),
            )
        drive(
            source.owner,
            source.name,
            ReadyValidSignal.READY,
            source_value(
                destination.owner,
                destination.name,
                ReadyValidSignal.READY,
                BitType(),
            ),
        )

    prepared = replace(
        module,
        assignments=tuple(assignments),
        instance_bindings=tuple(bindings),
        fifos=tuple(fifos),
        children=lowered_children,
        hierarchical_connections=(),
        protocol_endpoints=(),
    )
    return lower_ready_valid_module(prepared)


def lower_request_response_hierarchy(module: Module) -> Module:
    """Erase hierarchical request/response links before primitive planning.

    A request/response connection is already represented by semantic analysis
    as two exact ready/valid channel edges.  This lowering keeps that ownership
    in the compiler: child interfaces become scalar ports, the two channel
    directions become ordinary instance bindings, and optional directional
    buffers become compiler-owned FIFOs.  Neither hierarchy nor transaction
    protocol concepts cross the SimulationPlan boundary.
    """

    if not (module.elaborated_instances or module.instances or module.children):
        return lower_request_response_module(module)
    if len(module.elaborated_instances) != len(module.children):
        raise ProtocolSimulationLoweringError(
            f"module '{module.name}' has incomplete elaborated hierarchy"
        )

    lowered_children = tuple(
        lower_request_response_hierarchy(child) for child in module.children
    )
    if not module.request_response_connections:
        return replace(module, children=lowered_children)

    child_names = {item.instance.name for item in module.elaborated_instances}
    child_interfaces = {
        (owner.instance.name, interface.name): interface
        for owner, child in zip(
            module.elaborated_instances,
            module.children,
            strict=True,
        )
        for interface in child.request_responses
    }
    assignments = list(module.assignments)
    bindings = list(module.instance_bindings)
    fifos = list(module.fifos)

    def interface_for(owner: str, name: str) -> RequestResponseInterface:
        try:
            return child_interfaces[(owner, name)]
        except KeyError as error:
            raise ProtocolSimulationLoweringError(
                f"unknown request/response endpoint '{owner}.{name}'"
            ) from error

    def source_value(
        owner: str,
        name: str,
        channel: RequestResponseChannel,
        signal: ReadyValidSignal,
        type_,
    ) -> expr.Expression:
        if owner not in child_names:
            raise ProtocolSimulationLoweringError(
                "hierarchical request/response endpoints must name physical "
                f"children, found '{owner}.{name}'"
            )
        interface_for(owner, name)
        return expr.InstanceOutputRef(
            owner,
            request_response_field_name(name, channel, signal),
            type_,
        )

    def drive(
        owner: str,
        name: str,
        channel: RequestResponseChannel,
        signal: ReadyValidSignal,
        value: expr.Expression,
    ) -> None:
        if owner not in child_names:
            raise ProtocolSimulationLoweringError(
                "hierarchical request/response endpoints must name physical "
                f"children, found '{owner}.{name}'"
            )
        interface_for(owner, name)
        bindings.append(
            InstancePortBinding(
                owner,
                request_response_field_name(name, channel, signal),
                value,
            )
        )

    consumed_edges = []
    for connection in module.request_response_connections:
        if connection.ordering is not RequestResponseOrdering.IN_ORDER:
            raise ProtocolSimulationLoweringError(
                "hierarchical request/response lowering currently requires "
                "ordering in_order"
            )
        for channel, edge, payload_type, depth in (
            (
                RequestResponseChannel.REQUEST,
                connection.request,
                connection.request_type,
                connection.request.request_buffer_depth,
            ),
            (
                RequestResponseChannel.RESPONSE,
                connection.response,
                connection.response_type,
                connection.response.response_buffer_depth,
            ),
        ):
            if (
                edge.source.channel is not channel
                or edge.destination.channel is not channel
                or edge.source.protocol is not InterfaceProtocol.READY_VALID
                or edge.destination.protocol is not InterfaceProtocol.READY_VALID
                or edge.source.payload_type != payload_type
                or edge.destination.payload_type != payload_type
                or edge.buffer_depth
                or edge.adapter is not None
                or edge.crossing is not None
            ):
                raise ProtocolSimulationLoweringError(
                    "request/response hierarchy contains a malformed channel edge"
                )
            consumed_edges.append(edge)
            source = edge.source
            destination = edge.destination
            if depth:
                identity = hashlib.sha256(
                    (
                        f"{connection.semantic_id}|{channel.value}|{depth}|"
                        f"{payload_type}"
                    ).encode("utf-8")
                ).hexdigest()[:20]
                fifo_name = f"$zlang_request_response_buffer_{identity}"
                if any(fifo.name == fifo_name for fifo in fifos):
                    raise ProtocolSimulationLoweringError(
                        "hierarchical request/response buffer has a duplicate "
                        "physical FIFO identity"
                    )
                destination_ready = source_value(
                    destination.owner,
                    destination.name,
                    channel,
                    ReadyValidSignal.READY,
                    BitType(),
                )
                pop = _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    destination_ready,
                    expr.FifoRef(fifo_name, FifoSignal.VALID, BitType()),
                )
                fifos.append(
                    Fifo(
                        fifo_name,
                        payload_type,
                        depth,
                        source_value(
                            source.owner,
                            source.name,
                            channel,
                            ReadyValidSignal.PAYLOAD,
                            payload_type,
                        ),
                        source_value(
                            source.owner,
                            source.name,
                            channel,
                            ReadyValidSignal.VALID,
                            BitType(),
                        ),
                        pop,
                        domain=connection.clock_domain or source.domain,
                    )
                )
                drive(
                    destination.owner,
                    destination.name,
                    channel,
                    ReadyValidSignal.PAYLOAD,
                    expr.FifoRef(fifo_name, FifoSignal.FRONT, payload_type),
                )
                drive(
                    destination.owner,
                    destination.name,
                    channel,
                    ReadyValidSignal.VALID,
                    expr.FifoRef(fifo_name, FifoSignal.VALID, BitType()),
                )
                drive(
                    source.owner,
                    source.name,
                    channel,
                    ReadyValidSignal.READY,
                    expr.FifoRef(fifo_name, FifoSignal.READY, BitType()),
                )
                continue

            for signal, type_ in (
                (ReadyValidSignal.PAYLOAD, payload_type),
                (ReadyValidSignal.VALID, BitType()),
            ):
                drive(
                    destination.owner,
                    destination.name,
                    channel,
                    signal,
                    source_value(
                        source.owner,
                        source.name,
                        channel,
                        signal,
                        type_,
                    ),
                )
            drive(
                source.owner,
                source.name,
                channel,
                ReadyValidSignal.READY,
                source_value(
                    destination.owner,
                    destination.name,
                    channel,
                    ReadyValidSignal.READY,
                    BitType(),
                ),
            )

    missing = tuple(edge for edge in consumed_edges if edge not in module.hierarchical_connections)
    if missing:
        raise ProtocolSimulationLoweringError(
            "request/response descriptor is not backed by exact hierarchy edges"
        )
    remaining_edges = tuple(
        edge
        for edge in module.hierarchical_connections
        if edge not in consumed_edges
    )
    if any(
        edge.source.channel is not None or edge.destination.channel is not None
        for edge in remaining_edges
    ):
        raise ProtocolSimulationLoweringError(
            "orphan request/response channel edge remains after lowering"
        )

    prepared = replace(
        module,
        assignments=tuple(assignments),
        instance_bindings=tuple(bindings),
        fifos=tuple(fifos),
        children=lowered_children,
        hierarchical_connections=remaining_edges,
        request_response_connections=(),
        protocol_endpoints=tuple(
            endpoint
            for endpoint in module.protocol_endpoints
            if endpoint.channel is None
        ),
    )
    if prepared.request_responses:
        return lower_request_response_module(prepared)
    return prepared


def lower_credit_hierarchy(module: Module) -> Module:
    """Erase direct hierarchical credit links into scalar child bindings."""

    if not (module.elaborated_instances or module.instances or module.children):
        return lower_credit_module(module)
    if len(module.elaborated_instances) != len(module.children):
        raise ProtocolSimulationLoweringError(
            f"module '{module.name}' has incomplete elaborated hierarchy"
        )

    lowered_children = tuple(
        lower_credit_hierarchy(child) for child in module.children
    )
    credit_edges = tuple(
        edge
        for edge in module.hierarchical_connections
        if (
            edge.source.protocol is InterfaceProtocol.CREDIT
            or edge.destination.protocol is InterfaceProtocol.CREDIT
        )
    )
    if not credit_edges:
        return replace(module, children=lowered_children)

    child_names = {item.instance.name for item in module.elaborated_instances}
    top_ports = {port.name: port for port in module.ports}
    child_ports = {
        (owner.instance.name, port.name): port
        for owner, child in zip(
            module.elaborated_instances,
            module.children,
            strict=True,
        )
        for port in child.ports
    }
    assignments = list(module.assignments)
    bindings = list(module.instance_bindings)

    def endpoint_port(owner: str, name: str) -> Port:
        if owner == module.name:
            try:
                port = top_ports[name]
            except KeyError as error:
                raise ProtocolSimulationLoweringError(
                    f"unknown top credit endpoint '{name}'"
                ) from error
        else:
            if owner not in child_names:
                raise ProtocolSimulationLoweringError(
                    f"unknown credit endpoint owner '{owner}'"
                )
            try:
                port = child_ports[(owner, name)]
            except KeyError as error:
                raise ProtocolSimulationLoweringError(
                    f"unknown child credit endpoint '{owner}.{name}'"
                ) from error
        if port.protocol is not InterfaceProtocol.CREDIT:
            raise ProtocolSimulationLoweringError(
                f"hierarchical endpoint '{owner}.{name}' is not credit"
            )
        return port

    def source_value(
        owner: str,
        name: str,
        signal: CreditSignal,
        type_,
    ) -> expr.Expression:
        endpoint_port(owner, name)
        if owner == module.name:
            return expr.CreditRef(name, signal, type_)
        return expr.InstanceOutputRef(
            owner,
            credit_field_name(name, signal),
            type_,
        )

    def drive(
        owner: str,
        name: str,
        signal: CreditSignal,
        value: expr.Expression,
    ) -> None:
        port = endpoint_port(owner, name)
        if owner == module.name:
            assignments.append(Assignment(port, value, signal))
            return
        bindings.append(
            InstancePortBinding(
                owner,
                credit_field_name(name, signal),
                value,
            )
        )

    for edge in credit_edges:
        if (
            edge.source.protocol is not InterfaceProtocol.CREDIT
            or edge.destination.protocol is not InterfaceProtocol.CREDIT
            or edge.source.payload_type != edge.destination.payload_type
            or edge.source.capacity != edge.destination.capacity
            or edge.buffer_depth
            or edge.request_buffer_depth
            or edge.response_buffer_depth
            or edge.adapter is not None
            or edge.crossing is not None
            or edge.source.channel is not None
            or edge.destination.channel is not None
        ):
            raise ProtocolSimulationLoweringError(
                "hierarchical credit connection is not an exact direct link"
            )
        source = edge.source
        destination = edge.destination
        for signal, type_ in (
            (CreditSignal.PAYLOAD, source.payload_type),
            (CreditSignal.SEND, BitType()),
        ):
            drive(
                destination.owner,
                destination.name,
                signal,
                source_value(source.owner, source.name, signal, type_),
            )
        drive(
            source.owner,
            source.name,
            CreditSignal.RETURN,
            source_value(
                destination.owner,
                destination.name,
                CreditSignal.RETURN,
                BitType(),
            ),
        )

    prepared = replace(
        module,
        assignments=tuple(assignments),
        instance_bindings=tuple(bindings),
        children=lowered_children,
        hierarchical_connections=tuple(
            edge
            for edge in module.hierarchical_connections
            if edge not in credit_edges
        ),
        protocol_endpoints=tuple(
            endpoint
            for endpoint in module.protocol_endpoints
            if endpoint.protocol is not InterfaceProtocol.CREDIT
        ),
    )
    if any(
        port.protocol is InterfaceProtocol.CREDIT for port in prepared.ports
    ):
        return lower_credit_module(prepared)
    return prepared


def lower_aggregate_protocol_hierarchy(
    module: Module,
    *,
    _physical_child: bool = False,
) -> Module:
    """Expand aggregate protocol composition into ordinary member edges.

    Semantic analysis owns the aggregate schema and exact role direction.  A
    non-delegating connection already carries one typed physical edge per
    member.  Same-role top delegation is expanded here from the validated
    schema.  Scalar wire members become ordinary instance bindings; protocol
    members continue through their existing family-specific lowerers.
    """

    if not (module.elaborated_instances or module.instances or module.children):
        if module.aggregate_protocol_connections:
            raise ProtocolSimulationLoweringError(
                "aggregate protocol connection has no physical hierarchy"
            )
        if _physical_child and module.aggregate_protocol_endpoints:
            return replace(module, aggregate_protocol_endpoints=())
        return module
    if len(module.elaborated_instances) != len(module.children):
        raise ProtocolSimulationLoweringError(
            f"module '{module.name}' has incomplete elaborated hierarchy"
        )

    lowered_children = tuple(
        lower_aggregate_protocol_hierarchy(child, _physical_child=True)
        for child in module.children
    )
    if not module.aggregate_protocol_connections:
        return replace(
            module,
            children=lowered_children,
            aggregate_protocol_endpoints=(
                () if _physical_child else module.aggregate_protocol_endpoints
            ),
        )

    children = {
        item.instance.name: child
        for item, child in zip(
            module.elaborated_instances,
            module.children,
            strict=True,
        )
    }
    top_aggregates = {
        endpoint.name: endpoint
        for endpoint in module.aggregate_protocol_endpoints
    }
    child_aggregates = {
        (owner, endpoint.name): endpoint
        for owner, child in children.items()
        for endpoint in child.aggregate_protocol_endpoints
    }
    top_ports = {port.name: port for port in module.ports}
    child_ports = {
        (owner, port.name): port
        for owner, child in children.items()
        for port in child.ports
    }
    edges = list(module.hierarchical_connections)

    def endpoint(
        owner: str,
        port: Port,
    ) -> ProtocolEndpoint:
        return ProtocolEndpoint(
            owner,
            port.name,
            port.direction,
            port.protocol,
            port.type,
            port.capacity,
            port.domain,
        )

    def aggregate_members(
        aggregate: AggregateProtocolEndpoint,
    ) -> dict[str, object]:
        result = {member.name: member for member in aggregate.members}
        if len(result) != len(aggregate.members):
            raise ProtocolSimulationLoweringError(
                f"aggregate endpoint '{aggregate.name}' has duplicate members"
            )
        return result

    for connection in module.aggregate_protocol_connections:
        if not connection.delegation:
            if connection.crossing is not None:
                raise ProtocolSimulationLoweringError(
                    "aggregate protocol crossing must be lowered by CDC before "
                    "primitive simulation"
                )
            if "." not in connection.source or "." not in connection.destination:
                raise ProtocolSimulationLoweringError(
                    "aggregate composition must select two child endpoints"
                )
            source_owner, source_name = connection.source.split(".", 1)
            destination_owner, destination_name = connection.destination.split(
                ".", 1
            )
            try:
                source_aggregate = child_aggregates[(source_owner, source_name)]
                destination_aggregate = child_aggregates[
                    (destination_owner, destination_name)
                ]
            except KeyError as error:
                raise ProtocolSimulationLoweringError(
                    "aggregate composition names an unknown child endpoint"
                ) from error
            if (
                source_aggregate.protocol != connection.protocol
                or destination_aggregate.protocol != connection.protocol
                or source_aggregate.specialization_identity
                != connection.specialization_identity
                or destination_aggregate.specialization_identity
                != connection.specialization_identity
            ):
                raise ProtocolSimulationLoweringError(
                    "aggregate composition metadata disagrees with its endpoints"
                )
            source_members = aggregate_members(source_aggregate)
            destination_members = aggregate_members(destination_aggregate)
            if source_members != destination_members:
                raise ProtocolSimulationLoweringError(
                    "aggregate composition member schemas do not match"
                )
            for member_name, member in source_members.items():
                if (
                    member.source_role == source_aggregate.role
                    and member.sink_role == destination_aggregate.role
                ):
                    physical_source = (source_owner, source_name)
                    physical_destination = (destination_owner, destination_name)
                elif (
                    member.source_role == destination_aggregate.role
                    and member.sink_role == source_aggregate.role
                ):
                    physical_source = (destination_owner, destination_name)
                    physical_destination = (source_owner, source_name)
                else:
                    raise ProtocolSimulationLoweringError(
                        f"aggregate member '{member_name}' has incompatible roles"
                    )
                expected_source = (
                    physical_source[0],
                    f"{physical_source[1]}__{member_name}",
                )
                expected_destination = (
                    physical_destination[0],
                    f"{physical_destination[1]}__{member_name}",
                )
                matches = tuple(
                    edge
                    for edge in edges
                    if (
                        (edge.source.owner, edge.source.name) == expected_source
                        and (
                            edge.destination.owner,
                            edge.destination.name,
                        )
                        == expected_destination
                        and edge.source.protocol is member.protocol
                        and edge.destination.protocol is member.protocol
                        and edge.source.payload_type == member.payload_type
                        and edge.destination.payload_type == member.payload_type
                    )
                )
                if len(matches) != 1:
                    raise ProtocolSimulationLoweringError(
                        f"aggregate member '{member_name}' does not have one "
                        "exact physical edge"
                    )
            continue
        if connection.crossing is not None:
            raise ProtocolSimulationLoweringError(
                "aggregate protocol delegation cannot carry a crossing"
            )
        if "." not in connection.destination or "." in connection.source:
            raise ProtocolSimulationLoweringError(
                "aggregate delegation must connect one top endpoint to one child"
            )
        child_owner, child_name = connection.destination.split(".", 1)
        try:
            top = top_aggregates[connection.source]
            child = child_aggregates[(child_owner, child_name)]
        except KeyError as error:
            raise ProtocolSimulationLoweringError(
                "aggregate delegation names an unknown endpoint"
            ) from error
        if (
            top.protocol != connection.protocol
            or child.protocol != connection.protocol
            or top.specialization_identity != connection.specialization_identity
            or child.specialization_identity != connection.specialization_identity
            or top.role != child.role
        ):
            raise ProtocolSimulationLoweringError(
                "aggregate delegation metadata disagrees with its endpoints"
            )
        top_members = aggregate_members(top)
        child_members = aggregate_members(child)
        if top_members != child_members:
            raise ProtocolSimulationLoweringError(
                "aggregate delegation member schemas do not match"
            )
        for member_name in top_members:
            top_port_name = f"{top.name}__{member_name}"
            child_port_name = f"{child.name}__{member_name}"
            try:
                top_port = top_ports[top_port_name]
                child_port = child_ports[(child_owner, child_port_name)]
            except KeyError as error:
                raise ProtocolSimulationLoweringError(
                    "aggregate delegation has an incomplete physical member"
                ) from error
            if (
                top_port.protocol is not child_port.protocol
                or top_port.type != child_port.type
                or top_port.capacity != child_port.capacity
                or top_port.direction is not child_port.direction
            ):
                raise ProtocolSimulationLoweringError(
                    f"aggregate member '{member_name}' physical ports disagree"
                )
            if top_port.direction is PortDirection.INPUT:
                source = endpoint(module.name, top_port)
                destination = endpoint(child_owner, child_port)
            else:
                source = endpoint(child_owner, child_port)
                destination = endpoint(module.name, top_port)
            edges.append(HierarchicalConnection(source, destination))

    assignments = list(module.assignments)
    bindings = list(module.instance_bindings)
    wire_edges = tuple(
        edge
        for edge in edges
        if (
            edge.source.protocol is InterfaceProtocol.WIRE
            or edge.destination.protocol is InterfaceProtocol.WIRE
        )
    )
    for edge in wire_edges:
        if (
            edge.source.protocol is not InterfaceProtocol.WIRE
            or edge.destination.protocol is not InterfaceProtocol.WIRE
            or edge.source.payload_type != edge.destination.payload_type
            or edge.buffer_depth
            or edge.request_buffer_depth
            or edge.response_buffer_depth
            or edge.adapter is not None
            or edge.crossing is not None
            or edge.source.channel is not None
            or edge.destination.channel is not None
        ):
            raise ProtocolSimulationLoweringError(
                "aggregate scalar member is not an exact direct wire"
            )
        if edge.source.owner == module.name:
            try:
                source_value: expr.Expression = expr.InputRef(
                    edge.source.name,
                    top_ports[edge.source.name].type,
                )
            except KeyError as error:
                raise ProtocolSimulationLoweringError(
                    f"unknown aggregate top input '{edge.source.name}'"
                ) from error
        else:
            source_value = expr.InstanceOutputRef(
                edge.source.owner,
                edge.source.name,
                edge.source.payload_type,
            )
        if edge.destination.owner == module.name:
            try:
                target = top_ports[edge.destination.name]
            except KeyError as error:
                raise ProtocolSimulationLoweringError(
                    f"unknown aggregate top output '{edge.destination.name}'"
                ) from error
            assignments.append(Assignment(target, source_value))
        else:
            bindings.append(
                InstancePortBinding(
                    edge.destination.owner,
                    edge.destination.name,
                    source_value,
                )
            )

    return replace(
        module,
        assignments=tuple(assignments),
        instance_bindings=tuple(bindings),
        children=lowered_children,
        hierarchical_connections=tuple(
            edge for edge in edges if edge not in wire_edges
        ),
        aggregate_protocol_connections=(),
        aggregate_protocol_endpoints=(),
        protocol_endpoints=tuple(
            endpoint
            for endpoint in module.protocol_endpoints
            if endpoint.protocol is not InterfaceProtocol.WIRE
        ),
    )


__all__ = [
    "ProtocolSimulationLoweringError",
    "RUNTIME_PROTOCOL_SCOPE_PREFIX",
    "credit_field_name",
    "credit_state_name",
    "lower_adapter_module",
    "lower_credit_module",
    "lower_credit_hierarchy",
    "lower_aggregate_protocol_hierarchy",
    "lower_request_response_module",
    "lower_request_response_hierarchy",
    "lower_packet_arbiter_module",
    "lower_protocol_module",
    "lower_ready_valid_module",
    "lower_ready_valid_hierarchy",
    "lower_vc_credit_module",
    "packet_field_name",
    "packet_state_name",
    "request_response_field_name",
    "request_response_state_name",
    "ready_valid_field_name",
    "vc_credit_field_name",
    "vc_credit_state_name",
]
