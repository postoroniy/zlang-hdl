# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Shared protocol lowering identities and primitive helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
from typing import Callable, Generic, Iterable, Mapping, TypeVar

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.ir import verification as ir_verification
from zlang.simulation_primitives import bit_binary, bit_not
from zlang.simulation_rewrite import SimulationExpressionRewriter


class ProtocolSimulationLoweringError(ValueError):
    """A protocol module cannot be erased to primitive simulation fields."""


ProtocolScalarSignal = (
    ir_interfaces.ReadyValidSignal
    | ir_interfaces.CreditSignal
    | ir_interfaces.VirtualChannelCreditSignal
    | ir_interfaces.PacketSignal
)


@dataclass(frozen=True)
class ProtocolScalarField:
    """One compiler-owned scalar field of a public protocol endpoint."""

    signal: ProtocolScalarSignal
    type: ir_types.HardwareType
    external: bool


@dataclass(frozen=True)
class RequestResponseScalarField:
    """One compiler-owned scalar field of a request/response endpoint."""

    channel: ir_interfaces.RequestResponseChannel
    signal: ir_interfaces.ReadyValidSignal
    type: ir_types.HardwareType
    external: bool


@dataclass(frozen=True)
class CreditCounterTransition:
    """Exact shared predicates for one bounded credit/occupancy counter."""

    updated: expr.Expression
    at_zero: expr.Expression
    at_capacity: expr.Expression
    returned_without_send: expr.Expression
    sent_without_return: expr.Expression


def credit_counter_transition(
    count: expr.Expression,
    sent: expr.Expression,
    returned: expr.Expression,
    capacity: int,
    *,
    increase: expr.Expression | None = None,
    decrease: expr.Expression | None = None,
) -> CreditCounterTransition:
    """Build the one credit-counter transition used by all protocol lowerers."""

    type_ = count.type
    if not isinstance(type_, ir_types.UIntType):
        raise ProtocolSimulationLoweringError(
            "credit accounting state must have an unsigned type"
        )
    increase = returned if increase is None else increase
    decrease = sent if decrease is None else decrease
    updated = expr.Binary(
        expr.BinaryOperator.SUBTRACT,
        expr.Add(count, expr.Extend(increase, type_), type_),
        expr.Extend(decrease, type_),
        type_,
        type_,
    )
    at_zero = expr.Binary(
        expr.BinaryOperator.EQUAL,
        count,
        expr.Constant(0, type_),
        type_,
        ir_types.BitType(),
    )
    at_capacity = expr.Binary(
        expr.BinaryOperator.EQUAL,
        count,
        expr.Constant(capacity, type_),
        type_,
        ir_types.BitType(),
    )
    return CreditCounterTransition(
        updated,
        at_zero,
        at_capacity,
        bit_binary(expr.BinaryOperator.BIT_AND, returned, bit_not(sent)),
        bit_binary(expr.BinaryOperator.BIT_AND, sent, bit_not(returned)),
    )


def vc_credit_channel_type(port: ir_module.Port) -> ir_types.UIntType:
    if port.virtual_channels is None or port.virtual_channels < 1:
        raise ProtocolSimulationLoweringError(
            f"VC-credit endpoint '{port.name}' has no channel count"
        )
    return ir_types.UIntType(max(1, (port.virtual_channels - 1).bit_length()))


def credit_counter_spec(
    module: ir_module.Module,
    port: ir_module.Port,
    label: str,
) -> tuple[ir_types.UIntType, str, str, int]:
    """Return the shared scalar/VC-credit counter contract."""

    if port.capacity is None or port.capacity < 1:
        raise ProtocolSimulationLoweringError(
            f"{label} endpoint '{port.name}' has no positive capacity"
        )
    domain = port.domain or module.clock
    if domain is None:
        raise ProtocolSimulationLoweringError(
            f"{label} endpoint '{port.name}' has no owning clock domain"
        )
    kind = "credits" if port.direction is ir_module.PortDirection.OUTPUT else "occupancy"
    return (
        ir_types.UIntType(max(1, port.capacity.bit_length())),
        domain,
        kind,
        port.capacity if kind == "credits" else 0,
    )


def credit_counter_update(
    register: ir_module.Register,
    sent: expr.Expression,
    returned: expr.Expression,
    capacity: int,
    direction: ir_module.PortDirection,
    messages: tuple[str, str, str],
    *,
    increase: expr.Expression | None = None,
    decrease: expr.Expression | None = None,
) -> tuple[ir_module.NextAssignment, tuple[tuple[str, expr.Expression], ...]]:
    """Build one bounded counter update and its exact runtime contracts."""

    transition = credit_counter_transition(
        expr.RegisterRef(register.name, register.type),
        sent,
        returned,
        capacity,
        increase=increase,
        decrease=decrease,
    )
    if direction is ir_module.PortDirection.OUTPUT:
        violation = bit_binary(
            expr.BinaryOperator.BIT_AND,
            transition.returned_without_send,
            transition.at_capacity,
        )
        conditions = ((messages[0], bit_not(violation)),)
    else:
        underflow = bit_binary(
            expr.BinaryOperator.BIT_AND,
            transition.returned_without_send,
            transition.at_zero,
        )
        overflow = bit_binary(
            expr.BinaryOperator.BIT_AND,
            transition.sent_without_return,
            transition.at_capacity,
        )
        conditions = (
            (messages[1], bit_not(underflow)),
            (messages[2], bit_not(overflow)),
        )
    return ir_module.NextAssignment(register, transition.updated), conditions


def request_response_scalar_fields(
    interface: ir_module.RequestResponseInterface,
) -> tuple[RequestResponseScalarField, ...]:
    """Return the exact ordered scalar boundary of one request/response endpoint."""

    outbound = (
        ir_interfaces.RequestResponseChannel.REQUEST
        if interface.role is ir_interfaces.RequestResponseRole.REQUESTER
        else ir_interfaces.RequestResponseChannel.RESPONSE
    )
    return tuple(
        RequestResponseScalarField(
            channel,
            signal,
            (
                interface.request_type
                if channel is ir_interfaces.RequestResponseChannel.REQUEST
                else interface.response_type
            )
            if signal is ir_interfaces.ReadyValidSignal.PAYLOAD
            else ir_types.BitType(),
            (
                signal is ir_interfaces.ReadyValidSignal.READY
                if channel is outbound
                else signal
                in {
                    ir_interfaces.ReadyValidSignal.PAYLOAD,
                    ir_interfaces.ReadyValidSignal.VALID,
                }
            ),
        )
        for channel in ir_interfaces.RequestResponseChannel
        for signal in (
            ir_interfaces.ReadyValidSignal.PAYLOAD,
            ir_interfaces.ReadyValidSignal.VALID,
            ir_interfaces.ReadyValidSignal.READY,
        )
    )


def request_response_scalar_field(
    interface: ir_module.RequestResponseInterface,
    channel: ir_interfaces.RequestResponseChannel,
    signal: ir_interfaces.ReadyValidSignal,
) -> RequestResponseScalarField:
    return next(
        field
        for field in request_response_scalar_fields(interface)
        if field.channel is channel and field.signal is signal
    )


_PROTOCOL_FIELD_SPECS: dict[
    ir_interfaces.InterfaceProtocol,
    tuple[tuple[ProtocolScalarSignal, str, bool], ...],
] = {
    ir_interfaces.InterfaceProtocol.READY_VALID: (
        (ir_interfaces.ReadyValidSignal.PAYLOAD, "payload", True),
        (ir_interfaces.ReadyValidSignal.VALID, "bit", True),
        (ir_interfaces.ReadyValidSignal.READY, "bit", False),
    ),
    ir_interfaces.InterfaceProtocol.CREDIT: (
        (ir_interfaces.CreditSignal.PAYLOAD, "payload", True),
        (ir_interfaces.CreditSignal.SEND, "bit", True),
        (ir_interfaces.CreditSignal.RETURN, "bit", False),
    ),
    ir_interfaces.InterfaceProtocol.VC_CREDIT: (
        (ir_interfaces.VirtualChannelCreditSignal.PAYLOAD, "payload", True),
        (ir_interfaces.VirtualChannelCreditSignal.VC, "channel", True),
        (ir_interfaces.VirtualChannelCreditSignal.SEND, "bit", True),
        (ir_interfaces.VirtualChannelCreditSignal.RETURN, "bit", False),
        (ir_interfaces.VirtualChannelCreditSignal.RETURN_VC, "channel", False),
    ),
    ir_interfaces.InterfaceProtocol.PACKET: (
        (ir_interfaces.PacketSignal.PAYLOAD, "payload", True),
        (ir_interfaces.PacketSignal.VALID, "bit", True),
        (ir_interfaces.PacketSignal.READY, "bit", False),
        (ir_interfaces.PacketSignal.LAST, "bit", True),
    ),
}


def protocol_scalar_fields(port: ir_module.Port) -> tuple[ProtocolScalarField, ...]:
    """Return the exact scalar boundary of one supported protocol endpoint."""

    try:
        specs = _PROTOCOL_FIELD_SPECS[port.protocol]
    except KeyError as error:
        raise ProtocolSimulationLoweringError(
            f"protocol '{port.protocol.value}' has no scalar field inventory"
        ) from error
    channel_type = (
        vc_credit_channel_type(port)
        if port.protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT
        else ir_types.BitType()
    )

    return tuple(
        ProtocolScalarField(
            signal,
            (
                port.type
                if kind == "payload"
                else channel_type
                if kind == "channel"
                else ir_types.BitType()
            ),
            (
                forward
                if port.direction is ir_module.PortDirection.INPUT
                else not forward
            ),
        )
        for signal, kind, forward in specs
    )


def protocol_scalar_field(
    port: ir_module.Port,
    signal: ProtocolScalarSignal,
) -> ProtocolScalarField:
    return next(
        field for field in protocol_scalar_fields(port) if field.signal is signal
    )


RUNTIME_PROTOCOL_SCOPE_PREFIX = "$zlang_runtime_protocol:"
_ProtocolKey = TypeVar("_ProtocolKey")
_ProtocolSignal = TypeVar(
    "_ProtocolSignal",
    ir_interfaces.CreditSignal,
    ir_interfaces.VirtualChannelCreditSignal,
    ir_interfaces.ReadyValidSignal,
)


class _MappedProtocolExpressionLowerer(
    SimulationExpressionRewriter,
    Generic[_ProtocolKey],
):
    """Own the shared immutable lookup table used by protocol erasure."""

    def __init__(
        self,
        values: dict[_ProtocolKey, expr.Expression],
    ) -> None:
        super().__init__()
        self._values = values


def _protocol_port(
    ports: Mapping[str, ir_module.Port],
    endpoint: str,
    label: str,
) -> ir_module.Port:
    try:
        return ports[endpoint]
    except KeyError as error:
        raise ProtocolSimulationLoweringError(
            f"{label} reference names unknown endpoint '{endpoint}'"
        ) from error


def _producer_expression(
    lowerer: SimulationExpressionRewriter,
    producers: Mapping[tuple[str, _ProtocolSignal], expr.Expression],
    active: set[tuple[str, _ProtocolSignal]],
    endpoint: str,
    signal: _ProtocolSignal,
    label: str,
) -> expr.Expression:
    """Resolve one driver while rejecting recursive protocol dependencies."""

    key = (endpoint, signal)
    if key in active:
        raise ProtocolSimulationLoweringError(
            f"{label} combinational dependency is cyclic at "
            f"'{endpoint}.{signal.value}'"
        )
    try:
        source = producers[key]
    except KeyError as error:
        raise ProtocolSimulationLoweringError(
            f"{label} field '{endpoint}.{signal.value}' has no exact driver"
        ) from error
    active.add(key)
    try:
        return lowerer.expression(source)
    finally:
        active.remove(key)


def _reset_deasserted(
    module: ir_module.Module,
    port: ir_module.Port,
    label: str,
) -> expr.Expression:
    _, reset = _domain_reset(module, port, label)
    return bit_not(expr.InputRef(f"$reset:{reset}", ir_types.BitType()))


def _validate_leaf_endpoint_module(
    module: ir_module.Module,
    protocol: ir_interfaces.InterfaceProtocol,
    description: str,
) -> None:
    """Enforce the common closed-leaf boundary for protocol erasure."""

    if any(
        port.protocol not in {ir_interfaces.InterfaceProtocol.WIRE, protocol}
        for port in module.ports
    ):
        raise ProtocolSimulationLoweringError(
            f"{description} lowering cannot mix unrelated protocol families"
        )
    if (
        module.connections
        or module.request_responses
        or module.hierarchical_connections
        or module.aggregate_protocol_connections
        or module.request_response_connections
    ):
        raise ProtocolSimulationLoweringError(
            f"{description} lowering requires a leaf module without protocol "
            "connections"
        )


def _endpoint_producers(
    module: ir_module.Module,
    *,
    protocol: ir_interfaces.InterfaceProtocol,
    signal_type: type,
    derived: frozenset[object],
    label: str,
    assignments: tuple[ir_module.Assignment, ...] | list[ir_module.Assignment] | None = None,
    derived_error: str | None = None,
) -> dict[tuple[str, object], expr.Expression]:
    """Collect exact compiler-owned field drivers for one protocol family."""

    producers: dict[tuple[str, object], expr.Expression] = {}
    for assignment in module.assignments if assignments is None else assignments:
        target = assignment.target
        if not isinstance(target, ir_module.Port) or target.protocol is not protocol:
            continue
        if not isinstance(assignment.signal, signal_type):
            raise ProtocolSimulationLoweringError(
                f"{label} assignment '{target.name}' has no exact signal"
            )
        signal = assignment.signal
        if signal in derived:
            if derived_error is not None:
                raise ProtocolSimulationLoweringError(derived_error)
            raise ProtocolSimulationLoweringError(
                f"derived {label} field '{target.name}.{signal.value}' cannot be assigned"
            )
        if protocol_scalar_field(target, signal).external:
            raise ProtocolSimulationLoweringError(
                f"{label} field '{target.name}.{signal.value}' is environment-owned"
            )
        key = (target.name, signal)
        if key in producers:
            raise ProtocolSimulationLoweringError(
                f"{label} field '{target.name}.{signal.value}' has multiple drivers"
            )
        producers[key] = assignment.expression
    return producers


class ScalarPortInventory:
    """Own the ordered scalar boundary synthesized by one protocol pass."""

    def __init__(self, ports: Iterable[ir_module.Port]) -> None:
        self.ports = list(ports)
        self.by_name = {port.name: port for port in self.ports}

    @classmethod
    def retained_wires(cls, module: ir_module.Module) -> "ScalarPortInventory":
        return cls(
            port
            for port in module.ports
            if port.protocol is ir_interfaces.InterfaceProtocol.WIRE
        )

    def add(
        self,
        direction: ir_module.PortDirection,
        name: str,
        type_: ir_types.HardwareType,
        domain: str | None,
    ) -> ir_module.Port:
        port = ir_module.Port(direction, name, type_, domain=domain)
        self.ports.append(port)
        self.by_name[name] = port
        return port

    def add_protocol_field(
        self,
        *,
        external: bool,
        name: str,
        type_: ir_types.HardwareType,
        domain: str | None,
    ) -> ir_module.Port:
        """Add one scalarized field using the shared ownership convention."""

        return self.add(
            ir_module.PortDirection.INPUT
            if external
            else ir_module.PortDirection.OUTPUT,
            name,
            type_,
            domain,
        )

    def add_protocol_endpoint(
        self,
        port: ir_module.Port,
        name_for: Callable[[str, object], str],
    ) -> None:
        """Scalarize one endpoint from the authoritative field inventory."""

        for field in protocol_scalar_fields(port):
            self.add_protocol_field(
                external=field.external,
                name=name_for(port.name, field.signal),
                type_=field.type,
                domain=port.domain,
            )

    def assignment(self, name: str, value: expr.Expression) -> ir_module.Assignment:
        return ir_module.Assignment(self.by_name[name], value)


def _retained_wire_assignments(
    module: ir_module.Module,
    lowerer: SimulationExpressionRewriter,
) -> list[ir_module.Assignment]:
    return [
        replace(assignment, expression=lowerer.expression(assignment.expression))
        for assignment in module.assignments
        if not isinstance(assignment.target, ir_module.Port)
        or assignment.target.protocol is ir_interfaces.InterfaceProtocol.WIRE
    ]


def _rewritten_module_fields(
    module: ir_module.Module,
    lowerer: SimulationExpressionRewriter,
    **overrides: object,
) -> dict[str, object]:
    """Rewrite the expression-bearing leaf-module fields exactly once.

    Protocol passes differ in the state they synthesize, but the retained IR
    fields must all cross the same expression-rewrite boundary.  Keeping that
    inventory here prevents a newly added expression-bearing field from being
    handled by only one protocol family.
    """

    rewritten: dict[str, object] = {
        "functions": lowerer.value(module.functions),
        "registers": lowerer.value(module.registers),
        "next_assignments": lowerer.value(module.next_assignments),
        "rules": lowerer.value(module.rules),
        "fifos": lowerer.value(module.fifos),
        "memories": lowerer.value(module.memories),
        "roms": lowerer.value(module.roms),
        "locals": lowerer.value(module.locals),
        "instance_bindings": lowerer.value(module.instance_bindings),
        "resolved_transition": lowerer.value(module.resolved_transition),
        "callable_definitions": lowerer.value(module.callable_definitions),
        "verification_scopes": lowerer.value(module.verification_scopes),
    }
    rewritten.update(overrides)
    return rewritten


def _finalize_protocol_module(
    module: ir_module.Module,
    lowerer: SimulationExpressionRewriter,
    *,
    ports: tuple[ir_module.Port, ...],
    assignments: tuple[ir_module.Assignment, ...],
    rewritten: Mapping[str, object] | None = None,
    removed: Mapping[str, object] | None = None,
) -> ir_module.Module:
    """Close one protocol-erasure pass through the shared module boundary."""

    fields = _rewritten_module_fields(module, lowerer, **dict(rewritten or ()))
    fields.update(removed or {"connections": ()})
    return replace(
        module,
        ports=ports,
        assignments=assignments,
        protocol_endpoints=(),
        module_signature=None,
        semantic_expression_arena_statistics=None,
        semantic_expression_provenance=None,
        **fields,
    )


def _domain_reset(module: ir_module.Module, port: ir_module.Port, label: str) -> tuple[str, str]:
    """Resolve the exact clock/reset pair shared by protocol state owners."""

    domain = port.domain or module.clock
    if domain is None:
        raise ProtocolSimulationLoweringError(
            f"{label} endpoint '{port.name}' has no owning clock domain"
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
            f"{label} endpoint '{port.name}' has no owning reset"
        )
    return domain, reset


def _protocol_field_name(endpoint: str, signal: object) -> str:
    # Protocol signal enums intentionally inherit from ``str`` for stable JSON
    # representation.  Test Enum before str so formatting never publishes the
    # Python ``CreditSignal.RETURN`` spelling into the native-plan ABI.
    value = signal.value if isinstance(signal, Enum) else signal
    return f"$zlang_protocol:{endpoint}:{value}"


def credit_field_name(endpoint: str, signal: ir_interfaces.CreditSignal | str) -> str:
    """Return the private scalar-plan name for one credit endpoint field."""

    return _protocol_field_name(endpoint, signal)


def credit_state_name(endpoint: str, kind: str) -> str:
    """Return the deterministic compiler-owned credit accounting state name."""

    return f"$zlang_protocol_credit:{endpoint}:{kind}"


def vc_credit_field_name(
    endpoint: str,
    signal: ir_interfaces.VirtualChannelCreditSignal | str,
) -> str:
    """Return the private scalar-plan name for one VC-credit field."""

    return _protocol_field_name(endpoint, signal)


def vc_credit_state_name(endpoint: str, kind: str, channel: int) -> str:
    """Return one deterministic per-channel accounting state name."""

    return f"$zlang_protocol_vc_credit:{endpoint}:{kind}:{channel}"


def packet_field_name(endpoint: str, signal: ir_interfaces.PacketSignal | str) -> str:
    """Return the private scalar-plan name for one packet endpoint field."""

    return _protocol_field_name(endpoint, signal)


def packet_state_name(kind: str, source: str | None = None) -> str:
    """Return a deterministic compiler-owned packet arbiter state name."""

    suffix = "" if source is None else f":{source}"
    return f"$zlang_protocol_packet:{kind}{suffix}"


def request_response_field_name(
    interface: str,
    channel: ir_interfaces.RequestResponseChannel | str,
    signal: ir_interfaces.ReadyValidSignal | str,
) -> str:
    """Return one private scalar request/response field name."""

    channel_name = channel.value if isinstance(channel, ir_interfaces.RequestResponseChannel) else channel
    signal_name = signal.value if isinstance(signal, ir_interfaces.ReadyValidSignal) else signal
    return f"$zlang_request_response:{interface}:{channel_name}:{signal_name}"


def request_response_state_name(interface: str, kind: str) -> str:
    """Return deterministic compiler-owned request/response state."""

    return f"$zlang_request_response_state:{interface}:{kind}"


def _runtime_protocol_scope(
    *,
    module: ir_module.Module,
    port: ir_module.Port,
    conditions: tuple[tuple[str, expr.Expression], ...],
) -> ir_verification.VerificationScope:
    """Encode compiler-owned safety checks as generic primitive probes."""

    domain, reset = _domain_reset(module, port, "protocol")
    digest = hashlib.sha256(
        (
            f"{module.name}|{port.name}|{port.protocol.value}|{domain}|"
            "protocol-check"
        ).encode("utf-8")
    ).hexdigest()
    scope_id = RUNTIME_PROTOCOL_SCOPE_PREFIX + digest[:24]
    goals = tuple(
        ir_verification.VerificationGoal(
            semantic_id=f"{scope_id}:{index}",
            scope_id=scope_id,
            kind=ir_verification.VerificationGoalKind.ASSERT,
            name=message,
            expression=condition,
        )
        for index, (message, condition) in enumerate(conditions)
    )
    return ir_verification.VerificationScope(
        semantic_id=scope_id,
        name=f"{port.protocol.value} endpoint {port.name}",
        clock=domain,
        reset=reset,
        goals=goals,
    )
