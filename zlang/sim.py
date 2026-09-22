"""Public persistent simulation API.

The native engine is the default for the supported Community simulation
surface.  The independent reference engine remains explicit.  Neither engine
falls back to SystemVerilog or another executor for an unsupported plan.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import threading
from zlang.incremental_workspace import IncrementalWorkspaceSession
from zlang.source_identity import validate_source_path
from zlang.persistent_simulation_plan import load_or_build as _load_simulation_plan
from zlang.ir import packing
from zlang.ir import csr as ir_csr
from zlang.ir.interfaces import (
    CreditSignal,
    InterfaceProtocol,
    PacketSignal,
    ReadyValidSignal,
    RequestResponseChannel,
    RequestResponseRole,
    VirtualChannelCreditSignal,
)
from zlang.ir.module import PortDirection
from zlang.ir.runtime_values import TaggedUnionValue
from zlang.ir.types import (
    BitType,
    EnumType,
    HardwareType,
    TaggedUnionType,
    UIntType,
    VecType,
)
from zlang.simulation_protocols import (
    credit_field_name,
    packet_field_name,
    ready_valid_field_name,
    request_response_field_name,
    vc_credit_field_name,
)
from zlang.simulation_primitives import (
    int_from_limbs as _limbs_to_int,
    int_to_limbs as _int_to_limbs,
)
from zlang.simulate import (
    ProtocolViolation,
    VerificationAssertionError,
    VerificationCoverWitness,
    VerificationRequirementViolation,
)
from zlang.simulation_plan import (
    JitUnsupportedFeatureError,
    SIMULATION_RUNTIME_ABI,
    SimulationPlan,
    SimulationPlanError,
)


class SimulationRuntimeError(RuntimeError):
    """The persistent simulation instance rejected an operation."""


@dataclass(frozen=True)
class SimulationInstrumentationEvent:
    """One source-restored generic event emitted by the primitive machine."""

    event_id: int
    category: str
    event_index: int
    clock: str
    hierarchy_path: tuple[str, ...]
    scope_id: str
    scope_name: str
    clause_id: str
    clause_name: str


_PROGRAM_CACHE_ENTRIES = 8
_PROGRAM_CACHE: OrderedDict[str, object] = OrderedDict()
_PROGRAM_CACHE_LOCK = threading.RLock()
_COMPILATION_WORKSPACE = IncrementalWorkspaceSession()


def _pack_value(type_: HardwareType, value: object) -> int:
    if isinstance(type_, EnumType):
        if isinstance(value, str):
            try:
                return type_.member_code(value)
            except ValueError as error:
                raise SimulationRuntimeError(str(error)) from error
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not type_.is_valid_code(value)
        ):
            raise SimulationRuntimeError(
                f"value {value!r} is not a valid member/code of '{type_.name}'"
            )
        return value
    if isinstance(type_, TaggedUnionType):
        if not isinstance(value, TaggedUnionValue):
            raise SimulationRuntimeError(
                f"value for tagged union '{type_.name}' must be TaggedUnionValue"
            )
        return packing.pack_tagged_union_runtime(value)
    try:
        return packing.pack_runtime(type_, value)
    except packing.PackingError as error:
        raise SimulationRuntimeError(str(error)) from error


def _unpack_value(type_: HardwareType, value: int) -> object:
    if isinstance(type_, EnumType):
        if not type_.is_valid_code(value):
            raise SimulationRuntimeError(
                f"native value {value} is not a valid code of '{type_.name}'"
            )
        return value
    if isinstance(type_, TaggedUnionType):
        # Public tagged-union reconstruction requires an active-variant proof.
        # Keep the exact packed value rather than guessing a variant.
        return value
    try:
        return packing.unpack_runtime(type_, value)
    except packing.PackingError as error:
        raise SimulationRuntimeError(str(error)) from error


def _native_runtime():
    try:
        import _zlang_native_sim
    except ImportError as error:
        raise JitUnsupportedFeatureError(
            "the compatible ZLang native simulation runtime is not installed; "
            "install 'zlang-hdl[native]' for a supported platform or select "
            "engine='reference'"
        ) from error
    actual_abi = _zlang_native_sim.runtime_abi()
    if actual_abi != SIMULATION_RUNTIME_ABI:
        raise JitUnsupportedFeatureError(
            "the installed native simulation runtime ABI is incompatible: "
            f"expected {SIMULATION_RUNTIME_ABI!r}, found {actual_abi!r}"
        )
    return _zlang_native_sim


@dataclass(frozen=True)
class Program:
    """One compiled immutable simulation program, reusable across instances."""

    plan: SimulationPlan
    module: object
    _native: object

    @property
    def identity(self) -> str:
        return self.plan.identity

    def create(self) -> "Simulator":
        return Simulator(self, self._native.create())


class Simulator:
    """A mutable native instance; distinct instances may execute in parallel."""

    def __init__(self, program: Program, native: object) -> None:
        self.program = program
        self._native = native
        self._closed = False
        hidden_csr_ports = {
            name
            for block in program.module.csr_blocks
            for name in (
                *(
                    item
                    for binding in block.state_bindings
                    for item in (
                        ir_csr.csr_state_port_name(binding),
                        ir_csr.csr_write_hit_port_name(binding),
                        ir_csr.csr_write_value_port_name(binding),
                    )
                ),
                *(
                    item
                    for binding in block.access_observations
                    for item in (
                        ir_csr.csr_read_hit_port_name(binding),
                        ir_csr.csr_observation_write_hit_port_name(binding),
                        ir_csr.csr_observation_write_value_port_name(binding),
                        ir_csr.csr_observation_value_port_name(binding),
                    )
                ),
                *(
                    ir_csr.csr_event_port_name(binding)
                    for register in block.registers for binding in register.events
                ),
                *(
                    ir_csr.csr_split_port_name(block, view)
                    for view in block.split_views
                ),
            )
        }
        self._hidden_csr_ports = frozenset(hidden_csr_ports)
        self._ports = {
            port.name: port
            for port in program.module.ports
            if port.name not in hidden_csr_ports
        }
        self._request_responses = {
            interface.name: interface
            for interface in program.module.request_responses
        }
        self._registers = {
            register.name: register for register in program.module.registers
        }
        self._trace_names: dict[str, str] = {}
        self._event_metadata = {
            int(item["id"]): item["metadata"]
            for item in program.plan.payload["events"]
        }
        self._verification_scopes: dict[tuple[tuple[str, ...], str], object] = {}
        self._verification_clauses: dict[tuple[tuple[str, ...], str], object] = {}
        self._index_verification(program.module, (program.module.name,))
        self._instrumentation_events: list[SimulationInstrumentationEvent] = []
        self._requirement_violations: list[VerificationRequirementViolation] = []
        self._cover_witnesses: dict[
            tuple[tuple[str, ...], str], VerificationCoverWitness
        ] = {}

    def _index_verification(self, module: object, path: tuple[str, ...]) -> None:
        for scope in module.verification_scopes:
            self._verification_scopes[(path, scope.semantic_id)] = scope
            for clause in (*scope.requirements, *scope.goals):
                self._verification_clauses[(path, clause.semantic_id)] = clause
        for elaborated, child in zip(
            module.elaborated_instances, module.children, strict=True
        ):
            self._index_verification(
                child, (*path, elaborated.instance.name)
            )

    def _consume_instrumentation(self) -> BaseException | None:
        try:
            raw_events = self._native.drain_events()
        except (ValueError, RuntimeError) as error:
            raise SimulationRuntimeError(str(error)) from error
        failure: BaseException | None = None
        for raw_id, raw_index, raw_clock in raw_events:
            event_id = int(raw_id)
            try:
                metadata = self._event_metadata[event_id]
            except KeyError as error:
                raise SimulationRuntimeError(
                    f"native simulator emitted unknown event {event_id}"
                ) from error
            path = tuple(str(item) for item in metadata["hierarchy_path"])
            event = SimulationInstrumentationEvent(
                event_id=event_id,
                category=str(metadata["category"]),
                event_index=int(raw_index),
                clock=str(raw_clock),
                hierarchy_path=path,
                scope_id=str(metadata["scope_id"]),
                scope_name=str(metadata["scope_name"]),
                clause_id=str(metadata["clause_id"]),
                clause_name=str(metadata["clause_name"]),
            )
            self._instrumentation_events.append(event)
            if event.category == "runtime_violation":
                if failure is None:
                    failure = ProtocolViolation(event.clause_name)
                continue
            scope_key = (path, event.scope_id)
            clause_key = (path, event.clause_id)
            scope = self._verification_scopes.get(scope_key)
            clause = self._verification_clauses.get(clause_key)
            if scope is None or clause is None:
                raise SimulationRuntimeError(
                    "simulation event cannot be restored to its compiler-owned "
                    "verification declaration"
                )
            if event.category == "requirement_violation":
                self._requirement_violations.append(
                    VerificationRequirementViolation(
                        scope_id=event.scope_id,
                        scope_name=event.scope_name,
                        requirement_id=event.clause_id,
                        requirement_name=event.clause_name,
                        cycle=event.event_index,
                        source_origin=clause.source_origin,
                    )
                )
            elif event.category == "cover_witness":
                self._cover_witnesses.setdefault(
                    (event.hierarchy_path, event.clause_id),
                    VerificationCoverWitness(
                        scope_id=event.scope_id,
                        scope_name=event.scope_name,
                        goal_id=event.clause_id,
                        goal_name=event.clause_name,
                        cycle=event.event_index,
                        source_origin=clause.source_origin,
                    ),
                )
            elif event.category == "assertion_failure" and failure is None:
                failure = VerificationAssertionError(scope, clause, event.event_index)
                failure.clock = event.clock
                failure.hierarchy_path = event.hierarchy_path
        return failure

    def _raise_instrumentation_failure(
        self, error: BaseException | None = None
    ) -> None:
        failure = self._consume_instrumentation()
        if failure is not None:
            if error is None:
                raise failure
            raise failure from error

    @property
    def requirement_violations(self) -> tuple[VerificationRequirementViolation, ...]:
        return tuple(self._requirement_violations)

    @property
    def cover_witnesses(self) -> tuple[VerificationCoverWitness, ...]:
        return tuple(self._cover_witnesses.values())

    def _set_scalar(self, name: str, type_: HardwareType, value: object) -> None:
        packed = _pack_value(type_, value)
        try:
            self._native.set_limbs(name, _int_to_limbs(packed, type_.width))
        except (ValueError, RuntimeError) as error:
            raise SimulationRuntimeError(str(error)) from error

    def _get_scalar(self, name: str, type_: HardwareType) -> object:
        try:
            raw = _limbs_to_int(self._native.get_limbs(name))
        except (ValueError, RuntimeError) as error:
            raise SimulationRuntimeError(str(error)) from error
        return _unpack_value(type_, raw)

    @staticmethod
    def _protocol_input_fields(port: object) -> tuple[tuple[str, HardwareType], ...]:
        if port.protocol is InterfaceProtocol.READY_VALID:
            if port.direction is PortDirection.INPUT:
                return (("payload", port.type), ("valid", BitType()))
            return (("ready", BitType()),)
        if port.protocol is InterfaceProtocol.CREDIT:
            if port.direction is PortDirection.INPUT:
                return (("payload", port.type), ("send", BitType()))
            return (("return", BitType()),)
        if port.protocol is InterfaceProtocol.VC_CREDIT:
            if port.virtual_channels is None or port.virtual_channels < 1:
                raise SimulationRuntimeError(
                    f"VC-credit endpoint '{port.name}' has no channel count"
                )
            vc_type = UIntType(
                max(1, (port.virtual_channels - 1).bit_length())
            )
            if port.direction is PortDirection.INPUT:
                return (
                    ("payload", port.type),
                    ("vc", vc_type),
                    ("send", BitType()),
                )
            return (("return", BitType()), ("return_vc", vc_type))
        if port.protocol is InterfaceProtocol.PACKET:
            if port.direction is PortDirection.INPUT:
                return (
                    ("payload", port.type),
                    ("valid", BitType()),
                    ("last", BitType()),
                )
            return (("ready", BitType()),)
        raise SimulationRuntimeError(
            f"public protocol '{port.protocol.value}' is not supported"
        )

    @staticmethod
    def _protocol_field_name(port: object, field: str) -> str:
        if port.protocol is InterfaceProtocol.READY_VALID:
            return ready_valid_field_name(port.name, field)
        if port.protocol is InterfaceProtocol.CREDIT:
            return credit_field_name(port.name, field)
        if port.protocol is InterfaceProtocol.VC_CREDIT:
            return vc_credit_field_name(port.name, field)
        if port.protocol is InterfaceProtocol.PACKET:
            return packet_field_name(port.name, field)
        raise SimulationRuntimeError(
            f"public protocol '{port.protocol.value}' is not supported"
        )

    def _set_ready_valid(self, port: object, value: object) -> None:
        if not isinstance(value, Mapping):
            raise SimulationRuntimeError(
                f"ready/valid input '{port.name}' must be a field mapping"
            )
        fields = self._protocol_input_fields(port)
        expected = {name for name, _ in fields}
        if set(value) != expected:
            rendered = ", ".join(sorted(expected))
            raise SimulationRuntimeError(
                f"ready/valid input '{port.name}' requires fields: {rendered}"
            )
        for field, type_ in fields:
            self._set_scalar(
                ready_valid_field_name(port.name, field), type_, value[field]
            )

    def _set_protocol(self, port: object, value: object) -> None:
        if port.protocol is InterfaceProtocol.READY_VALID:
            self._set_ready_valid(port, value)
            return
        if not isinstance(value, Mapping):
            raise SimulationRuntimeError(
                f"{port.protocol.value} input '{port.name}' must be a field mapping"
            )
        fields = self._protocol_input_fields(port)
        expected = {name for name, _ in fields}
        if set(value) != expected:
            rendered = ", ".join(sorted(expected))
            raise SimulationRuntimeError(
                f"{port.protocol.value} input '{port.name}' requires fields: "
                f"{rendered}"
            )
        for field, type_ in fields:
            self._set_scalar(
                self._protocol_field_name(port, field), type_, value[field]
            )

    def _get_ready_valid(self, port: object) -> dict[str, object]:
        valid = int(
            self._get_scalar(
                ready_valid_field_name(port.name, ReadyValidSignal.VALID),
                BitType(),
            )
        )
        ready = int(
            self._get_scalar(
                ready_valid_field_name(port.name, ReadyValidSignal.READY),
                BitType(),
            )
        )
        transfer = int(bool(valid) and bool(ready))
        if port.direction is PortDirection.INPUT:
            return {"ready": ready, "transfer": transfer}
        return {
            "payload": self._get_scalar(
                ready_valid_field_name(port.name, ReadyValidSignal.PAYLOAD),
                port.type,
            ),
            "valid": valid,
            "transfer": transfer,
        }

    def _get_credit(self, port: object) -> dict[str, object]:
        sent = int(
            self._get_scalar(
                credit_field_name(port.name, CreditSignal.SEND),
                BitType(),
            )
        )
        if port.direction is PortDirection.INPUT:
            returned = int(
                self._get_scalar(
                    credit_field_name(port.name, CreditSignal.RETURN),
                    BitType(),
                )
            )
            return {"return": returned, "transfer": sent}
        if port.capacity is None:
            raise SimulationRuntimeError(
                f"credit endpoint '{port.name}' has no capacity"
            )
        count_type = UIntType(max(1, port.capacity.bit_length()))
        credits = int(
            self._get_scalar(
                credit_field_name(port.name, CreditSignal.CREDITS),
                count_type,
            )
        )
        return {
            "payload": self._get_scalar(
                credit_field_name(port.name, CreditSignal.PAYLOAD),
                port.type,
            ),
            "send": sent,
            "transfer": sent,
            "credits": credits,
        }

    def _get_vc_credit(self, port: object) -> dict[str, object]:
        if (
            port.capacity is None
            or port.virtual_channels is None
            or port.virtual_channels < 1
        ):
            raise SimulationRuntimeError(
                f"VC-credit endpoint '{port.name}' has incomplete bounds"
            )
        vc_type = UIntType(max(1, (port.virtual_channels - 1).bit_length()))
        count_type = UIntType(max(1, port.capacity.bit_length()))
        counts_type = VecType(port.virtual_channels, count_type)
        sent = int(
            self._get_scalar(
                vc_credit_field_name(
                    port.name,
                    VirtualChannelCreditSignal.SEND,
                ),
                BitType(),
            )
        )
        if port.direction is PortDirection.INPUT:
            returned = int(
                self._get_scalar(
                    vc_credit_field_name(
                        port.name,
                        VirtualChannelCreditSignal.RETURN,
                    ),
                    BitType(),
                )
            )
            occupancy = self._get_scalar(
                vc_credit_field_name(port.name, "occupancy"),
                counts_type,
            )
            return {
                "return": returned,
                "return_vc": self._get_scalar(
                    vc_credit_field_name(
                        port.name,
                        VirtualChannelCreditSignal.RETURN_VC,
                    ),
                    vc_type,
                ),
                "transfer": sent,
                "occupancy": tuple(occupancy),
            }
        credits = self._get_scalar(
            vc_credit_field_name(
                port.name,
                VirtualChannelCreditSignal.CREDITS,
            ),
            counts_type,
        )
        return {
            "payload": self._get_scalar(
                vc_credit_field_name(
                    port.name,
                    VirtualChannelCreditSignal.PAYLOAD,
                ),
                port.type,
            ),
            "vc": self._get_scalar(
                vc_credit_field_name(
                    port.name,
                    VirtualChannelCreditSignal.VC,
                ),
                vc_type,
            ),
            "send": sent,
            "transfer": sent,
            "credits": tuple(credits),
        }

    def _get_packet(self, port: object) -> dict[str, object]:
        valid = int(
            self._get_scalar(
                packet_field_name(port.name, PacketSignal.VALID),
                BitType(),
            )
        )
        ready = int(
            self._get_scalar(
                packet_field_name(port.name, PacketSignal.READY),
                BitType(),
            )
        )
        transfer = int(bool(valid) and bool(ready))
        if port.direction is PortDirection.INPUT:
            return {"ready": ready, "transfer": transfer}
        grant_valid = int(
            self._get_scalar(
                packet_field_name(port.name, "grant_valid"),
                BitType(),
            )
        )
        owner_count = sum(
            candidate.protocol is InterfaceProtocol.PACKET
            and candidate.direction is PortDirection.INPUT
            for candidate in self.program.module.ports
        )
        owner_type = UIntType(max(1, (owner_count - 1).bit_length()))
        grant = self._get_scalar(
            packet_field_name(port.name, "grant"),
            owner_type,
        )
        return {
            "payload": self._get_scalar(
                packet_field_name(port.name, PacketSignal.PAYLOAD),
                port.type,
            ),
            "valid": valid,
            "last": int(
                self._get_scalar(
                    packet_field_name(port.name, PacketSignal.LAST),
                    BitType(),
                )
            ),
            "transfer": transfer,
            "grant": grant if grant_valid else None,
        }

    def _get_protocol(self, port: object) -> dict[str, object]:
        if port.protocol is InterfaceProtocol.READY_VALID:
            return self._get_ready_valid(port)
        if port.protocol is InterfaceProtocol.CREDIT:
            return self._get_credit(port)
        if port.protocol is InterfaceProtocol.VC_CREDIT:
            return self._get_vc_credit(port)
        if port.protocol is InterfaceProtocol.PACKET:
            return self._get_packet(port)
        raise SimulationRuntimeError(
            f"public protocol '{port.protocol.value}' is not supported"
        )

    @staticmethod
    def _protocol_trace_fields(port: object) -> tuple[str, ...]:
        if port.protocol is InterfaceProtocol.READY_VALID:
            return ("payload", "valid", "ready")
        if port.protocol is InterfaceProtocol.CREDIT:
            return (
                ("payload", "send", "return", "credits")
                if port.direction is PortDirection.OUTPUT
                else ("payload", "send", "return")
            )
        if port.protocol is InterfaceProtocol.VC_CREDIT:
            return (
                ("payload", "vc", "send", "return", "return_vc", "credits")
                if port.direction is PortDirection.OUTPUT
                else (
                    "payload",
                    "vc",
                    "send",
                    "return",
                    "return_vc",
                    "occupancy",
                )
            )
        if port.protocol is InterfaceProtocol.PACKET:
            return (
                ("payload", "valid", "ready", "last")
                if port.direction is PortDirection.INPUT
                else (
                    "payload",
                    "valid",
                    "ready",
                    "last",
                    "grant",
                    "grant_valid",
                )
            )
        raise SimulationRuntimeError(
            f"public protocol '{port.protocol.value}' is not supported"
        )

    @staticmethod
    def _request_response_input_fields(
        interface: object,
    ) -> tuple[tuple[RequestResponseChannel, str, HardwareType], ...]:
        if interface.role is RequestResponseRole.REQUESTER:
            return (
                (RequestResponseChannel.REQUEST, "ready", BitType()),
                (
                    RequestResponseChannel.RESPONSE,
                    "payload",
                    interface.response_type,
                ),
                (RequestResponseChannel.RESPONSE, "valid", BitType()),
            )
        return (
            (
                RequestResponseChannel.REQUEST,
                "payload",
                interface.request_type,
            ),
            (RequestResponseChannel.REQUEST, "valid", BitType()),
            (RequestResponseChannel.RESPONSE, "ready", BitType()),
        )

    def _set_request_response(self, interface: object, value: object) -> None:
        if not isinstance(value, Mapping) or set(value) != {"request", "response"}:
            raise SimulationRuntimeError(
                f"request/response input '{interface.name}' requires request "
                "and response mappings"
            )
        expected: dict[str, dict[str, HardwareType]] = {
            "request": {},
            "response": {},
        }
        for channel, field, type_ in self._request_response_input_fields(interface):
            expected[channel.value][field] = type_
        for channel_name, fields in expected.items():
            supplied = value[channel_name]
            if not isinstance(supplied, Mapping) or set(supplied) != set(fields):
                rendered = ", ".join(sorted(fields))
                raise SimulationRuntimeError(
                    f"request/response input '{interface.name}.{channel_name}' "
                    f"requires fields: {rendered}"
                )
            for field, type_ in fields.items():
                self._set_scalar(
                    request_response_field_name(
                        interface.name,
                        channel_name,
                        field,
                    ),
                    type_,
                    supplied[field],
                )

    def _get_request_response(self, interface: object) -> dict[str, object]:
        def scalar(
            channel: RequestResponseChannel,
            field: str,
            type_: HardwareType,
        ) -> object:
            return self._get_scalar(
                request_response_field_name(interface.name, channel, field),
                type_,
            )

        request_valid = int(
            scalar(RequestResponseChannel.REQUEST, "valid", BitType())
        )
        request_ready = int(
            scalar(RequestResponseChannel.REQUEST, "ready", BitType())
        )
        response_valid = int(
            scalar(RequestResponseChannel.RESPONSE, "valid", BitType())
        )
        response_ready = int(
            scalar(RequestResponseChannel.RESPONSE, "ready", BitType())
        )
        request_transfer = int(bool(request_valid) and bool(request_ready))
        response_transfer = int(bool(response_valid) and bool(response_ready))
        count_type = UIntType(max(1, interface.max_outstanding.bit_length()))
        outstanding = int(
            self._get_scalar(
                request_response_field_name(
                    interface.name,
                    "ledger",
                    "outstanding",
                ),
                count_type,
            )
        )
        if interface.role is RequestResponseRole.REQUESTER:
            return {
                "request": {
                    "payload": scalar(
                        RequestResponseChannel.REQUEST,
                        "payload",
                        interface.request_type,
                    ),
                    "valid": request_valid,
                    "transfer": request_transfer,
                },
                "response": {
                    "ready": response_ready,
                    "transfer": response_transfer,
                },
                "outstanding": outstanding,
            }
        return {
            "request": {
                "ready": request_ready,
                "transfer": request_transfer,
            },
            "response": {
                "payload": scalar(
                    RequestResponseChannel.RESPONSE,
                    "payload",
                    interface.response_type,
                ),
                "valid": response_valid,
                "transfer": response_transfer,
            },
            "outstanding": outstanding,
        }

    @staticmethod
    def _request_response_trace_names(
        interface: object,
    ) -> tuple[tuple[str, str], ...]:
        fields = tuple(
            (
                request_response_field_name(
                    interface.name,
                    channel,
                    field,
                ),
                f"{interface.name}.{channel.value}.{field}",
            )
            for channel in RequestResponseChannel
            for field in ("payload", "valid", "ready")
        )
        return (
            *fields,
            (
                request_response_field_name(
                    interface.name,
                    "ledger",
                    "outstanding",
                ),
                f"{interface.name}.outstanding",
            ),
        )

    def _require_open(self) -> None:
        if self._closed:
            raise SimulationRuntimeError("simulation instance is closed")

    def set(self, name: str, value: object) -> None:
        self._require_open()
        interface = self._request_responses.get(name)
        if interface is not None:
            self._set_request_response(interface, value)
            return
        try:
            port = self._ports[name]
        except KeyError as error:
            raise SimulationRuntimeError(f"unknown public port '{name}'") from error
        if port.protocol is not InterfaceProtocol.WIRE:
            self._set_protocol(port, value)
            return
        self.set_packed(name, _pack_value(port.type, value))

    def set_packed(self, name: str, value: int) -> None:
        self._require_open()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SimulationRuntimeError(
                "packed simulation values must be unsigned integers"
            )
        try:
            port = self._ports[name]
        except KeyError as error:
            raise SimulationRuntimeError(f"unknown public port '{name}'") from error
        if port.protocol is not InterfaceProtocol.WIRE:
            raise SimulationRuntimeError(
                "set_packed requires a scalar wire port; use set() with a "
                "protocol field mapping"
            )
        if value.bit_length() > port.type.width:
            raise SimulationRuntimeError(
                f"value does not fit {port.type.width}-bit input '{name}'"
            )
        try:
            self._native.set_limbs(name, _int_to_limbs(value, port.type.width))
        except (ValueError, RuntimeError) as error:
            raise SimulationRuntimeError(str(error)) from error

    def get(self, name: str) -> object:
        self._require_open()
        interface = self._request_responses.get(name)
        if interface is not None:
            return self._get_request_response(interface)
        if name in self._ports:
            port = self._ports[name]
            if port.protocol is not InterfaceProtocol.WIRE:
                return self._get_protocol(port)
        raw = self.get_packed(name)
        if name in self._ports:
            return _unpack_value(self._ports[name].type, raw)
        if name in self._registers:
            return _unpack_value(self._registers[name].type, raw)
        raise SimulationRuntimeError(f"unknown signal '{name}'")

    def get_packed(self, name: str) -> int:
        self._require_open()
        port = self._ports.get(name)
        if port is not None and port.protocol is not InterfaceProtocol.WIRE:
            raise SimulationRuntimeError(
                "get_packed requires a scalar wire port; use get() for a "
                "protocol endpoint"
            )
        try:
            return _limbs_to_int(self._native.get_limbs(name))
        except (ValueError, RuntimeError) as error:
            raise SimulationRuntimeError(str(error)) from error

    def eval(self) -> dict[str, object]:
        self._require_open()
        try:
            self._native.eval()
        except RuntimeError as error:
            raise SimulationRuntimeError(str(error)) from error
        return self.outputs()

    def outputs(self) -> dict[str, object]:
        outputs = {
            port.name: self.get(port.name)
            for port in self._ports.values()
            if port.direction is PortDirection.OUTPUT
            or port.protocol is not InterfaceProtocol.WIRE
        }
        outputs.update(
            (interface.name, self._get_request_response(interface))
            for interface in self.program.module.request_responses
        )
        return outputs

    def edge(self, clock: str) -> dict[str, object]:
        self._require_open()
        try:
            self._native.edge(clock)
        except (ValueError, RuntimeError) as error:
            self._raise_instrumentation_failure(error)
            raise SimulationRuntimeError(str(error)) from error
        self._raise_instrumentation_failure()
        return self.outputs()

    def edge_many(self, clocks: Iterable[str]) -> dict[str, object]:
        self._require_open()
        selected = tuple(clocks)
        if len(selected) != len(set(selected)):
            raise SimulationRuntimeError("one event cannot contain a clock twice")
        try:
            self._native.edge_many(list(selected))
        except (ValueError, RuntimeError) as error:
            self._raise_instrumentation_failure(error)
            raise SimulationRuntimeError(str(error)) from error
        self._raise_instrumentation_failure()
        return self.outputs()

    def reset(self, name: str, *, asserted: bool) -> dict[str, object]:
        self._require_open()
        try:
            self._native.reset(name, asserted)
        except (ValueError, RuntimeError) as error:
            self._raise_instrumentation_failure(error)
            raise SimulationRuntimeError(str(error)) from error
        self._raise_instrumentation_failure()
        return self.outputs()

    def tick(self, clock: str) -> dict[str, object]:
        return self.edge(clock)

    def run_cycles(self, clock: str, count: int) -> dict[str, object]:
        self._require_open()
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SimulationRuntimeError("cycle count must be a non-negative integer")
        try:
            self._native.run_cycles(clock, count)
        except (ValueError, RuntimeError) as error:
            self._raise_instrumentation_failure(error)
            raise SimulationRuntimeError(str(error)) from error
        self._raise_instrumentation_failure()
        return self.outputs()

    def run_events(
        self, events: Iterable[Mapping[str, object]]
    ) -> list[dict[str, object]]:
        self._require_open()
        encoded_events = []
        protocol_ports = tuple(
            port
            for port in self.program.module.ports
            if port.protocol is not InterfaceProtocol.WIRE
        )
        request_response_interfaces = tuple(
            self.program.module.request_responses
        )
        external_protocol_values: dict[str, object] = {}
        for port in protocol_ports:
            for field, type_ in self._protocol_input_fields(port):
                internal = self._protocol_field_name(port, field)
                external_protocol_values[internal] = self._get_scalar(internal, type_)
        for interface in request_response_interfaces:
            for channel, field, type_ in self._request_response_input_fields(
                interface
            ):
                internal = request_response_field_name(
                    interface.name,
                    channel,
                    field,
                )
                external_protocol_values[internal] = self._get_scalar(
                    internal,
                    type_,
                )
        protocol_event_values: list[dict[str, object]] = []
        for event in events:
            unknown = set(event) - {"set", "reset", "edges"}
            if unknown:
                raise SimulationRuntimeError(
                    f"simulation event has unknown field '{sorted(unknown)[0]}'"
                )
            updates = event.get("set", {})
            resets = event.get("reset", {})
            edges = event.get("edges", ())
            if not isinstance(updates, Mapping) or not isinstance(resets, Mapping):
                raise SimulationRuntimeError("event set/reset fields must be mappings")
            if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)):
                raise SimulationRuntimeError("event edges field must be a sequence")
            packed_updates = []
            for raw_name, value in updates.items():
                name = str(raw_name)
                port = self._ports.get(name)
                interface = self._request_responses.get(name)
                if port is None and interface is None:
                    raise SimulationRuntimeError(f"unknown public port '{name}'")
                if interface is not None:
                    if not isinstance(value, Mapping) or set(value) != {
                        "request",
                        "response",
                    }:
                        raise SimulationRuntimeError(
                            f"request/response input '{name}' requires request "
                            "and response mappings"
                        )
                    expected: dict[str, dict[str, HardwareType]] = {
                        "request": {},
                        "response": {},
                    }
                    for channel, field, type_ in self._request_response_input_fields(
                        interface
                    ):
                        expected[channel.value][field] = type_
                    for channel_name, fields in expected.items():
                        supplied = value[channel_name]
                        if not isinstance(supplied, Mapping) or set(supplied) != set(
                            fields
                        ):
                            rendered = ", ".join(sorted(fields))
                            raise SimulationRuntimeError(
                                f"request/response input '{name}.{channel_name}' "
                                f"requires fields: {rendered}"
                            )
                        for field, type_ in fields.items():
                            internal = request_response_field_name(
                                name,
                                channel_name,
                                field,
                            )
                            packed = _pack_value(type_, supplied[field])
                            packed_updates.append((
                                internal,
                                _int_to_limbs(packed, type_.width),
                            ))
                            external_protocol_values[internal] = supplied[field]
                    continue
                assert port is not None
                if port.protocol is not InterfaceProtocol.WIRE:
                    if not isinstance(value, Mapping):
                        raise SimulationRuntimeError(
                            f"{port.protocol.value} input '{name}' must be a "
                            "field mapping"
                        )
                    fields = self._protocol_input_fields(port)
                    expected = {field for field, _ in fields}
                    if set(value) != expected:
                        rendered = ", ".join(sorted(expected))
                        raise SimulationRuntimeError(
                            f"{port.protocol.value} input '{name}' requires "
                            f"fields: {rendered}"
                        )
                    for field, type_ in fields:
                        packed = _pack_value(type_, value[field])
                        internal = self._protocol_field_name(port, field)
                        packed_updates.append(
                            (
                                internal,
                                _int_to_limbs(packed, type_.width),
                            )
                        )
                        external_protocol_values[internal] = value[field]
                else:
                    packed = _pack_value(port.type, value)
                    packed_updates.append(
                        (name, _int_to_limbs(packed, port.type.width))
                    )
            packed_resets = []
            for raw_name, value in resets.items():
                if not isinstance(value, bool):
                    raise SimulationRuntimeError("reset event values must be boolean")
                packed_resets.append((str(raw_name), value))
            selected_edges = [str(item) for item in edges]
            if len(selected_edges) != len(set(selected_edges)):
                raise SimulationRuntimeError("one event cannot contain a clock twice")
            encoded_events.append((packed_updates, packed_resets, selected_edges))
            protocol_event_values.append(dict(external_protocol_values))
        try:
            raw_results = self._native.run_events(encoded_events)
        except (ValueError, RuntimeError) as error:
            self._raise_instrumentation_failure(error)
            raise SimulationRuntimeError(str(error)) from error
        self._raise_instrumentation_failure()
        if protocol_ports or request_response_interfaces:
            results: list[dict[str, object]] = []
            for raw, external in zip(raw_results, protocol_event_values, strict=True):
                decoded: dict[str, object] = {}
                for port in self._ports.values():
                    if port.protocol is InterfaceProtocol.WIRE:
                        if port.direction is PortDirection.OUTPUT:
                            decoded[port.name] = _unpack_value(
                                port.type, _limbs_to_int(raw[port.name])
                            )
                        continue
                    if port.protocol is InterfaceProtocol.READY_VALID:
                        payload_name = ready_valid_field_name(port.name, "payload")
                        valid_name = ready_valid_field_name(port.name, "valid")
                        ready_name = ready_valid_field_name(port.name, "ready")
                        if port.direction is PortDirection.INPUT:
                            valid = int(external[valid_name])
                            ready = int(_limbs_to_int(raw[ready_name]))
                            decoded[port.name] = {
                                "ready": ready,
                                "transfer": int(bool(valid) and bool(ready)),
                            }
                        else:
                            valid = int(_limbs_to_int(raw[valid_name]))
                            ready = int(external[ready_name])
                            decoded[port.name] = {
                                "payload": _unpack_value(
                                    port.type, _limbs_to_int(raw[payload_name])
                                ),
                                "valid": valid,
                                "transfer": int(bool(valid) and bool(ready)),
                            }
                    elif port.protocol is InterfaceProtocol.CREDIT:
                        send_name = credit_field_name(port.name, "send")
                        sent = (
                            int(external[send_name])
                            if port.direction is PortDirection.INPUT
                            else int(_limbs_to_int(raw[send_name]))
                        )
                        if port.direction is PortDirection.INPUT:
                            return_name = credit_field_name(port.name, "return")
                            decoded[port.name] = {
                                "return": int(_limbs_to_int(raw[return_name])),
                                "transfer": sent,
                            }
                        else:
                            payload_name = credit_field_name(port.name, "payload")
                            if port.capacity is None:
                                raise SimulationRuntimeError(
                                    f"credit endpoint '{port.name}' has no capacity"
                                )
                            count_name = credit_field_name(port.name, "credits")
                            decoded[port.name] = {
                                "payload": _unpack_value(
                                    port.type, _limbs_to_int(raw[payload_name])
                                ),
                                "send": sent,
                                "transfer": sent,
                                "credits": int(
                                    _limbs_to_int(raw[count_name])
                                ),
                            }
                    elif port.protocol is InterfaceProtocol.VC_CREDIT:
                        if (
                            port.capacity is None
                            or port.virtual_channels is None
                            or port.virtual_channels < 1
                        ):
                            raise SimulationRuntimeError(
                                f"VC-credit endpoint '{port.name}' has "
                                "incomplete bounds"
                            )
                        vc_type = UIntType(
                            max(1, (port.virtual_channels - 1).bit_length())
                        )
                        count_type = UIntType(max(1, port.capacity.bit_length()))
                        counts_type = VecType(port.virtual_channels, count_type)
                        send_name = vc_credit_field_name(port.name, "send")
                        sent = (
                            int(external[send_name])
                            if port.direction is PortDirection.INPUT
                            else int(_limbs_to_int(raw[send_name]))
                        )
                        if port.direction is PortDirection.INPUT:
                            return_name = vc_credit_field_name(port.name, "return")
                            return_vc_name = vc_credit_field_name(
                                port.name,
                                "return_vc",
                            )
                            occupancy_name = vc_credit_field_name(
                                port.name,
                                "occupancy",
                            )
                            decoded[port.name] = {
                                "return": int(_limbs_to_int(raw[return_name])),
                                "return_vc": _unpack_value(
                                    vc_type,
                                    _limbs_to_int(raw[return_vc_name]),
                                ),
                                "transfer": sent,
                                "occupancy": tuple(
                                    _unpack_value(
                                        counts_type,
                                        _limbs_to_int(raw[occupancy_name]),
                                    )
                                ),
                            }
                        else:
                            payload_name = vc_credit_field_name(
                                port.name,
                                "payload",
                            )
                            vc_name = vc_credit_field_name(port.name, "vc")
                            credits_name = vc_credit_field_name(
                                port.name,
                                "credits",
                            )
                            decoded[port.name] = {
                                "payload": _unpack_value(
                                    port.type,
                                    _limbs_to_int(raw[payload_name]),
                                ),
                                "vc": _unpack_value(
                                    vc_type,
                                    _limbs_to_int(raw[vc_name]),
                                ),
                                "send": sent,
                                "transfer": sent,
                                "credits": tuple(
                                    _unpack_value(
                                        counts_type,
                                        _limbs_to_int(raw[credits_name]),
                                    )
                                ),
                            }
                    elif port.protocol is InterfaceProtocol.PACKET:
                        valid_name = packet_field_name(port.name, "valid")
                        ready_name = packet_field_name(port.name, "ready")
                        valid = (
                            int(external[valid_name])
                            if port.direction is PortDirection.INPUT
                            else int(_limbs_to_int(raw[valid_name]))
                        )
                        ready = (
                            int(_limbs_to_int(raw[ready_name]))
                            if port.direction is PortDirection.INPUT
                            else int(external[ready_name])
                        )
                        transfer = int(bool(valid) and bool(ready))
                        if port.direction is PortDirection.INPUT:
                            decoded[port.name] = {
                                "ready": ready,
                                "transfer": transfer,
                            }
                        else:
                            payload_name = packet_field_name(port.name, "payload")
                            last_name = packet_field_name(port.name, "last")
                            grant_name = packet_field_name(port.name, "grant")
                            grant_valid_name = packet_field_name(
                                port.name,
                                "grant_valid",
                            )
                            owner_count = sum(
                                candidate.protocol is InterfaceProtocol.PACKET
                                and candidate.direction is PortDirection.INPUT
                                for candidate in self.program.module.ports
                            )
                            owner_type = UIntType(
                                max(1, (owner_count - 1).bit_length())
                            )
                            grant_valid = int(
                                _limbs_to_int(raw[grant_valid_name])
                            )
                            grant = _unpack_value(
                                owner_type,
                                _limbs_to_int(raw[grant_name]),
                            )
                            decoded[port.name] = {
                                "payload": _unpack_value(
                                    port.type,
                                    _limbs_to_int(raw[payload_name]),
                                ),
                                "valid": valid,
                                "last": int(_limbs_to_int(raw[last_name])),
                                "transfer": transfer,
                                "grant": grant if grant_valid else None,
                            }
                    else:
                        raise SimulationRuntimeError(
                            f"public protocol '{port.protocol.value}' is not supported"
                        )
                for interface in request_response_interfaces:
                    external_names = {
                        request_response_field_name(
                            interface.name,
                            channel,
                            field,
                        )
                        for channel, field, _ in self._request_response_input_fields(
                            interface
                        )
                    }

                    def rr_value(
                        channel: RequestResponseChannel,
                        field: str,
                        type_: HardwareType,
                    ) -> object:
                        internal = request_response_field_name(
                            interface.name,
                            channel,
                            field,
                        )
                        if internal in external_names:
                            return external[internal]
                        return _unpack_value(type_, _limbs_to_int(raw[internal]))

                    request_valid = int(
                        rr_value(
                            RequestResponseChannel.REQUEST,
                            "valid",
                            BitType(),
                        )
                    )
                    request_ready = int(
                        rr_value(
                            RequestResponseChannel.REQUEST,
                            "ready",
                            BitType(),
                        )
                    )
                    response_valid = int(
                        rr_value(
                            RequestResponseChannel.RESPONSE,
                            "valid",
                            BitType(),
                        )
                    )
                    response_ready = int(
                        rr_value(
                            RequestResponseChannel.RESPONSE,
                            "ready",
                            BitType(),
                        )
                    )
                    request_transfer = int(
                        bool(request_valid) and bool(request_ready)
                    )
                    response_transfer = int(
                        bool(response_valid) and bool(response_ready)
                    )
                    count_name = request_response_field_name(
                        interface.name,
                        "ledger",
                        "outstanding",
                    )
                    outstanding = int(_limbs_to_int(raw[count_name]))
                    if interface.role is RequestResponseRole.REQUESTER:
                        decoded[interface.name] = {
                            "request": {
                                "payload": rr_value(
                                    RequestResponseChannel.REQUEST,
                                    "payload",
                                    interface.request_type,
                                ),
                                "valid": request_valid,
                                "transfer": request_transfer,
                            },
                            "response": {
                                "ready": response_ready,
                                "transfer": response_transfer,
                            },
                            "outstanding": outstanding,
                        }
                    else:
                        decoded[interface.name] = {
                            "request": {
                                "ready": request_ready,
                                "transfer": request_transfer,
                            },
                            "response": {
                                "payload": rr_value(
                                    RequestResponseChannel.RESPONSE,
                                    "payload",
                                    interface.response_type,
                                ),
                                "valid": response_valid,
                                "transfer": response_transfer,
                            },
                            "outstanding": outstanding,
                        }
                results.append(decoded)
            return results
        return [
            {
                name: _unpack_value(self._ports[name].type, _limbs_to_int(value))
                for name, value in raw.items()
                if name in self._ports
            }
            for raw in raw_results
        ]

    def enable_trace(self, signals: Iterable[str] | None = None) -> None:
        self._require_open()
        self._trace_names = {}
        if signals is None:
            selected = None
            chosen = (*self._ports, *self._request_responses)
        else:
            requested = tuple(signals)
            chosen = requested
            selected_items: list[str] = []
            for name in requested:
                interface = self._request_responses.get(name)
                if interface is not None:
                    for internal, public in self._request_response_trace_names(
                        interface
                    ):
                        selected_items.append(internal)
                        self._trace_names[internal] = public
                    continue
                port = self._ports.get(name)
                if port is None or port.protocol is InterfaceProtocol.WIRE:
                    selected_items.append(name)
                    self._trace_names[name] = name
                    continue
                selected_items.extend(
                    self._protocol_field_name(port, field)
                    for field in self._protocol_trace_fields(port)
                )
            selected = selected_items
        for name in chosen:
            interface = self._request_responses.get(name)
            if interface is not None:
                for internal, public in self._request_response_trace_names(interface):
                    self._trace_names[internal] = public
                continue
            port = self._ports.get(name)
            if port is None or port.protocol is InterfaceProtocol.WIRE:
                self._trace_names.setdefault(name, name)
                continue
            for field in self._protocol_trace_fields(port):
                self._trace_names[self._protocol_field_name(port, field)] = (
                    f"{name}.{field}"
                )
        if selected is None and self._hidden_csr_ports:
            selected = list(self._trace_names)
        try:
            self._native.enable_trace(selected)
        except (ValueError, RuntimeError) as error:
            raise SimulationRuntimeError(str(error)) from error

    def drain_trace(self) -> list[dict[str, int]]:
        self._require_open()
        try:
            return [
                {
                    self._trace_names.get(name, name): _limbs_to_int(value)
                    for name, value in item.items()
                }
                for item in self._native.drain_trace()
            ]
        except RuntimeError as error:
            raise SimulationRuntimeError(str(error)) from error

    @property
    def trace_widths(self) -> dict[str, int]:
        """Return exact packed widths for the currently named trace surface."""

        raw = {
            str(item["name"]): int(item["width"])
            for group in ("ports", "registers")
            for item in self.program.plan.payload[group]
        }
        return {
            self._trace_names.get(name, name): width
            for name, width in raw.items()
        }

    def drain_events(self) -> tuple[SimulationInstrumentationEvent, ...]:
        """Return and clear source-restored instrumentation events."""

        self._require_open()
        self._raise_instrumentation_failure()
        result = tuple(self._instrumentation_events)
        self._instrumentation_events.clear()
        return result

    def close(self) -> None:
        if not self._closed:
            self._native.close()
            self._closed = True

    def __enter__(self) -> "Simulator":
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def compile(
    source: Path | str,
    *,
    top: str | None = None,
    project: Path | str | None = None,
    profile: str | None = None,
    engine: str = "native",
) -> Program:
    """Compile one file into a reusable persistent simulation program."""

    if engine not in {"jit", "native", "reference", "python"}:
        raise ValueError(
            "simulation engine must be 'native'/'jit' or 'reference'/'python'"
        )
    source_path = validate_source_path(source)
    source_text = source_path.read_text(encoding="utf-8")
    session = _COMPILATION_WORKSPACE.file_snapshot(
        source_path, source_text, project=project, profile=profile, top=top,
    )
    plan = _load_simulation_plan(session)
    _COMPILATION_WORKSPACE.refresh(session)
    runtime = None
    if engine in {"jit", "native"}:
        runtime = _native_runtime()
        cache_key = f"native:{plan.execution_identity}"
    else:
        from zlang.simulation_reference import compile_reference_plan

        cache_key = f"reference:{plan.identity}"
    with _PROGRAM_CACHE_LOCK:
        native = _PROGRAM_CACHE.get(cache_key)
        if native is None:
            if runtime is None:
                native = compile_reference_plan(plan)
            else:
                try:
                    native = runtime.compile_plan_bytes(plan.to_bytes())
                except (ValueError, RuntimeError) as error:
                    raise JitUnsupportedFeatureError(str(error)) from error
            _PROGRAM_CACHE[cache_key] = native
            while len(_PROGRAM_CACHE) > _PROGRAM_CACHE_ENTRIES:
                _PROGRAM_CACHE.popitem(last=False)
        else:
            _PROGRAM_CACHE.move_to_end(cache_key)
    return Program(plan=plan, module=session.planning.module, _native=native)


def load(
    source: Path | str,
    *,
    top: str | None = None,
    project: Path | str | None = None,
    profile: str | None = None,
    engine: str = "native",
) -> Simulator:
    """Compile *source* and create one persistent simulation instance."""

    return compile(
        source,
        top=top,
        project=project,
        profile=profile,
        engine=engine,
    ).create()


__all__ = [
    "JitUnsupportedFeatureError",
    "Program",
    "SimulationPlan",
    "SimulationPlanError",
    "SimulationRuntimeError",
    "SimulationInstrumentationEvent",
    "Simulator",
    "compile",
    "load",
]
