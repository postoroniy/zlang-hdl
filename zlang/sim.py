"""Public persistent native simulation API.

Generated RTL comparison is an explicit validation mode; it is never an
execution fallback for an unsupported native plan.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
import threading
from zlang.incremental_workspace import IncrementalWorkspaceSession
from zlang.source_identity import validate_source_path
from zlang.persistent_simulation_plan import load_or_build as _load_simulation_plan
from zlang.ir import csr as ir_csr
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module, PortDirection
from zlang.ir.types import HardwareType
from zlang.opt.identity import canonical_ir_identity
from zlang.simulation_event_runner import SimulationEventRunner
from zlang.simulation_initial_state import resolve_initial_registers
from zlang.simulation_logic import LogicBit, LogicVector
from zlang.simulation_logic_machine import LogicMachineSnapshot, LogicStateMachine
from zlang.simulation_protocol_access import SimulationProtocolAccess
from zlang.simulation_primitives import (
    int_from_limbs as _limbs_to_int,
    int_to_limbs as _int_to_limbs,
)
from zlang.simulation_errors import (
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
from zlang.simulation_values import (
    SimulationRuntimeError,
    pack_value as _pack_value,
    unpack_value as _unpack_value,
)


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


def _native_runtime():
    try:
        import _zlang_native_sim
    except ImportError as error:
        raise JitUnsupportedFeatureError(
            "the compatible ZLang native simulation runtime is not installed; "
            "install 'zlang-hdl[native]' for a supported platform"
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
    module: Module
    _native: object
    selected_ir_identity: str | None = None
    strict_uninitialized: bool = False
    logic_state: bool = False

    @property
    def identity(self) -> str:
        return self.plan.identity

    def create(
        self,
        *,
        initial_registers: Mapping[str, str | LogicVector] | None = None,
    ) -> "Simulator":
        if initial_registers and not self.logic_state:
            raise SimulationRuntimeError(
                "initial_registers requires logic_state=True so U/X provenance "
                "cannot be discarded"
            )
        if initial_registers and self.selected_ir_identity is None:
            raise SimulationRuntimeError(
                "initial_registers requires compiler-owned selected IR identity"
            )
        overrides = resolve_initial_registers(
            self.module,
            self.selected_ir_identity or "",
            initial_registers,
        )
        native = self._native.create()
        if overrides:
            try:
                native.write_state_batch([
                    (
                        "register",
                        name,
                        None,
                        _int_to_limbs(value.value, value.width),
                    )
                    for name, value in overrides.items()
                ])
            except (ValueError, RuntimeError) as error:
                raise SimulationRuntimeError(str(error)) from error
        return Simulator(self, native, initial_registers=overrides)


class Simulator:
    """A mutable native instance; distinct instances may execute in parallel."""

    def __init__(
        self,
        program: Program,
        native: object,
        *,
        initial_registers: Mapping[str, LogicVector] | None = None,
    ) -> None:
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
        self._logic = (
            LogicStateMachine(
                program.plan.payload,
                initial_registers=initial_registers,
            )
            if program.logic_state
            else None
        )
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
        self._protocols = SimulationProtocolAccess(self)

    def _index_verification(self, module: Module, path: tuple[str, ...]) -> None:
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
        if self._logic is not None:
            self._logic.set_input(name, LogicVector.known(packed, type_.width))
        try:
            self._native.set_limbs(name, _int_to_limbs(packed, type_.width))
        except (ValueError, RuntimeError) as error:
            raise SimulationRuntimeError(str(error)) from error

    def _get_scalar(self, name: str, type_: HardwareType) -> object:
        if self._logic is not None:
            raw = self._logic.get(name)
            return _unpack_value(type_, self._require_binary(name, raw))
        try:
            raw = _limbs_to_int(self._native.get_limbs(name))
        except (ValueError, RuntimeError) as error:
            raise SimulationRuntimeError(str(error)) from error
        return _unpack_value(type_, raw)

    def _require_open(self) -> None:
        if self._closed:
            raise SimulationRuntimeError("simulation instance is closed")

    def _require_binary(self, name: str, value: LogicVector) -> int:
        try:
            return value.require_binary(name="value")
        except ValueError as error:
            if self.program.strict_uninitialized:
                raise SimulationRuntimeError(
                    f"strict-uninitialized simulation observed '{name}': {error}"
                ) from error
            raise SimulationRuntimeError(
                f"typed read of '{name}' requires a binary value; {error}; "
                "use get_logic()"
            ) from error

    def set(self, name: str, value: object) -> None:
        self._require_open()
        interface = self._request_responses.get(name)
        if interface is not None:
            self._protocols.set_request_response(interface, value)
            return
        try:
            port = self._ports[name]
        except KeyError as error:
            raise SimulationRuntimeError(f"unknown public port '{name}'") from error
        if port.protocol is not InterfaceProtocol.WIRE:
            self._protocols.set_port(port, value)
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
        previous_logic = self._logic.get(name) if self._logic is not None else None
        if self._logic is not None:
            self._logic.set_input(name, LogicVector.known(value, port.type.width))
        try:
            self._native.set_limbs(name, _int_to_limbs(value, port.type.width))
        except (ValueError, RuntimeError) as error:
            if self._logic is not None:
                assert previous_logic is not None
                self._logic.set_input(name, previous_logic)
            raise SimulationRuntimeError(str(error)) from error

    def get(self, name: str) -> object:
        self._require_open()
        interface = self._request_responses.get(name)
        if interface is not None:
            return self._protocols.get_request_response(interface)
        if name in self._ports:
            port = self._ports[name]
            if port.protocol is not InterfaceProtocol.WIRE:
                return self._protocols.get_port(port)
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
        if self._logic is not None:
            return self._require_binary(name, self._logic.get(name))
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
        if self._logic is not None:
            self._logic.evaluate()
        return self._step_outputs()

    def _step_outputs(self) -> dict[str, object]:
        if self._logic is not None and not self.program.strict_uninitialized:
            return self.outputs_logic()
        return self.outputs()

    def set_logic(self, name: str, value: str | LogicVector) -> None:
        """Set one scalar input with an exact 0/1/U/X packed value."""

        self._require_open()
        if self._logic is None:
            raise SimulationRuntimeError("set_logic requires logic_state=True")
        port = self._ports.get(name)
        if port is None:
            raise SimulationRuntimeError(f"unknown public port '{name}'")
        if port.protocol is not InterfaceProtocol.WIRE:
            raise SimulationRuntimeError("set_logic currently requires a scalar wire input")
        try:
            parsed = value if isinstance(value, LogicVector) else LogicVector.parse(value, port.type.width)
        except ValueError as error:
            raise SimulationRuntimeError(str(error)) from error
        previous = self._logic.get(name)
        self._logic.set_input(name, parsed)
        try:
            self._native.set_limbs(name, _int_to_limbs(parsed.value, parsed.width))
        except (ValueError, RuntimeError) as error:
            self._logic.set_input(name, previous)
            raise SimulationRuntimeError(str(error)) from error

    def get_logic(self, name: str) -> LogicVector:
        """Read a scalar signal without collapsing U/X provenance."""

        self._require_open()
        if self._logic is None:
            port = self._ports.get(name)
            register = self._registers.get(name)
            if port is None and register is None:
                raise SimulationRuntimeError(f"unknown signal '{name}'")
            width = port.type.width if port is not None else register.type.width
            return LogicVector.known(self.get_packed(name), width)
        return self._logic.get(name)

    def outputs_logic(self) -> dict[str, LogicVector]:
        if self._logic is None:
            return {
                port.name: self.get_logic(port.name)
                for port in self._ports.values()
                if port.direction is PortDirection.OUTPUT
                and port.protocol is InterfaceProtocol.WIRE
            }
        return {
            port.name: self._logic.get(port.name)
            for port in self._ports.values()
            if port.direction is PortDirection.OUTPUT
            and port.protocol is InterfaceProtocol.WIRE
        }

    def _sync_logic_state_to_native(
        self,
        previous: LogicMachineSnapshot | None = None,
    ) -> None:
        if self._logic is None:
            return
        edits = [
            ("register", name, None, _int_to_limbs(value.value, value.width))
            for name, value in self._logic.states.items()
            if previous is None
            or previous.states.get(name) != value
            or name in self._logic.refreshed_registers
        ]
        edits.extend(
            (
                "memory",
                name,
                index,
                _int_to_limbs(value.value, value.width),
            )
            for name, cells in self._logic.memories.items()
            for index, value in enumerate(cells)
            if previous is None
            or previous.memories.get(name, ())[index] != value
            or (name, index) in self._logic.written_memory_cells
        )
        if not edits:
            return
        try:
            self._native.write_state_batch(edits)
        except (ValueError, RuntimeError) as error:
            raise SimulationRuntimeError(str(error)) from error

    def outputs(self) -> dict[str, object]:
        outputs = {
            port.name: self.get(port.name)
            for port in self._ports.values()
            if port.direction is PortDirection.OUTPUT
            or port.protocol is not InterfaceProtocol.WIRE
        }
        outputs.update(
            (interface.name, self._protocols.get_request_response(interface))
            for interface in self.program.module.request_responses
        )
        return outputs

    def edge(self, clock: str) -> dict[str, object]:
        self._require_open()
        logic_snapshot = self._logic.snapshot() if self._logic is not None else None
        try:
            if self._logic is not None:
                self._logic.edge([clock])
            self._native.edge(clock)
        except SimulationRuntimeError:
            if self._logic is not None:
                assert logic_snapshot is not None
                self._logic.restore(logic_snapshot)
            raise
        except (ValueError, RuntimeError) as error:
            if self._logic is not None:
                assert logic_snapshot is not None
                self._logic.restore(logic_snapshot)
            self._raise_instrumentation_failure(error)
            raise SimulationRuntimeError(str(error)) from error
        if self._logic is not None:
            self._sync_logic_state_to_native(logic_snapshot)
        self._raise_instrumentation_failure()
        return self._step_outputs()

    def edge_many(self, clocks: Iterable[str]) -> dict[str, object]:
        self._require_open()
        selected = tuple(clocks)
        if len(selected) != len(set(selected)):
            raise SimulationRuntimeError("one event cannot contain a clock twice")
        logic_snapshot = self._logic.snapshot() if self._logic is not None else None
        try:
            if self._logic is not None:
                self._logic.edge(list(selected))
            self._native.edge_many(list(selected))
        except SimulationRuntimeError:
            if self._logic is not None:
                assert logic_snapshot is not None
                self._logic.restore(logic_snapshot)
            raise
        except (ValueError, RuntimeError) as error:
            if self._logic is not None:
                assert logic_snapshot is not None
                self._logic.restore(logic_snapshot)
            self._raise_instrumentation_failure(error)
            raise SimulationRuntimeError(str(error)) from error
        if self._logic is not None:
            self._sync_logic_state_to_native(logic_snapshot)
        self._raise_instrumentation_failure()
        return self._step_outputs()

    def reset(self, name: str, *, asserted: bool) -> dict[str, object]:
        self._require_open()
        logic_snapshot = self._logic.snapshot() if self._logic is not None else None
        try:
            if self._logic is not None:
                self._logic.reset(name, asserted)
            self._native.reset(name, asserted)
        except SimulationRuntimeError:
            if self._logic is not None:
                assert logic_snapshot is not None
                self._logic.restore(logic_snapshot)
            raise
        except (ValueError, RuntimeError) as error:
            if self._logic is not None:
                assert logic_snapshot is not None
                self._logic.restore(logic_snapshot)
            self._raise_instrumentation_failure(error)
            raise SimulationRuntimeError(str(error)) from error
        if self._logic is not None:
            self._sync_logic_state_to_native(logic_snapshot)
        self._raise_instrumentation_failure()
        return self._step_outputs()

    def tick(self, clock: str) -> dict[str, object]:
        return self.edge(clock)

    def run_cycles(self, clock: str, count: int) -> dict[str, object]:
        self._require_open()
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SimulationRuntimeError("cycle count must be a non-negative integer")
        if self._logic is not None:
            for _ in range(count):
                self.edge(clock)
            return self._step_outputs()
        try:
            self._native.run_cycles(clock, count)
        except (ValueError, RuntimeError) as error:
            self._raise_instrumentation_failure(error)
            raise SimulationRuntimeError(str(error)) from error
        self._raise_instrumentation_failure()
        return self._step_outputs()

    def run_events(
        self, events: Iterable[Mapping[str, object]]
    ) -> list[dict[str, object]]:
        return SimulationEventRunner(self).run(events)

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
                    for internal, public in self._protocols.request_response_trace_names(
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
                    self._protocols.field_name(port, field)
                    for field in self._protocols.trace_fields(port)
                )
            selected = selected_items
        for name in chosen:
            interface = self._request_responses.get(name)
            if interface is not None:
                for internal, public in self._protocols.request_response_trace_names(
                    interface
                ):
                    self._trace_names[internal] = public
                continue
            port = self._ports.get(name)
            if port is None or port.protocol is InterfaceProtocol.WIRE:
                self._trace_names.setdefault(name, name)
                continue
            for field in self._protocols.trace_fields(port):
                self._trace_names[self._protocols.field_name(port, field)] = (
                    f"{name}.{field}"
                )
        if selected is None and self._hidden_csr_ports:
            selected = list(self._trace_names)
        if self._logic is not None:
            self._logic.enable_trace(tuple(selected or self._trace_names))
            return
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

    def drain_logic_trace(self) -> list[dict[str, object]]:
        self._require_open()
        if self._logic is None:
            return self.drain_trace()
        return [
            {
                self._trace_names.get(name, name): value
                for name, value in item.items()
            }
            for item in self._logic.drain_trace()
        ]

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
    strict_uninitialized: bool = False,
    logic_state: bool = False,
) -> Program:
    """Compile one file into a reusable persistent simulation program."""

    if engine != "native":
        raise ValueError(
            f"simulation engine '{engine}' was removed; use engine='native' "
            "and --compare-with iverilog|verilator for an external RTL oracle"
        )
    source_path = validate_source_path(source)
    source_text = source_path.read_text(encoding="utf-8")
    session = _COMPILATION_WORKSPACE.file_snapshot(
        source_path, source_text, project=project, profile=profile, top=top,
    )
    logic_state = logic_state or strict_uninitialized
    if logic_state:
        from zlang.simulation_plan_build import build_simulation_plan

        plan = build_simulation_plan(session.planning.module)
    else:
        plan = _load_simulation_plan(session)
    _COMPILATION_WORKSPACE.refresh(session)
    runtime = _native_runtime()
    cache_key = f"native:{plan.execution_identity}"
    with _PROGRAM_CACHE_LOCK:
        native = _PROGRAM_CACHE.get(cache_key)
        if native is None:
            try:
                native = runtime.compile_plan_bytes(plan.to_bytes())
            except (ValueError, RuntimeError) as error:
                raise JitUnsupportedFeatureError(str(error)) from error
            _PROGRAM_CACHE[cache_key] = native
            while len(_PROGRAM_CACHE) > _PROGRAM_CACHE_ENTRIES:
                _PROGRAM_CACHE.popitem(last=False)
        else:
            _PROGRAM_CACHE.move_to_end(cache_key)
    return Program(
        plan=plan,
        module=session.planning.module,
        _native=native,
        selected_ir_identity=canonical_ir_identity(session.selection.optimization_ir),
        strict_uninitialized=strict_uninitialized,
        logic_state=logic_state,
    )


def load(
    source: Path | str,
    *,
    top: str | None = None,
    project: Path | str | None = None,
    profile: str | None = None,
    engine: str = "native",
    strict_uninitialized: bool = False,
    logic_state: bool = False,
    initial_registers: Mapping[str, str | LogicVector] | None = None,
) -> Simulator:
    """Compile *source* and create one persistent simulation instance."""

    return compile(
        source,
        top=top,
        project=project,
        profile=profile,
        engine=engine,
        strict_uninitialized=strict_uninitialized,
        logic_state=logic_state,
    ).create(initial_registers=initial_registers)


__all__ = [
    "JitUnsupportedFeatureError",
    "LogicVector",
    "LogicBit",
    "Program",
    "SimulationPlan",
    "SimulationPlanError",
    "SimulationRuntimeError",
    "SimulationInstrumentationEvent",
    "Simulator",
    "compile",
    "load",
]
