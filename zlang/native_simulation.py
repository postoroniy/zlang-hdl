"""Typed-module convenience helpers backed solely by the native simulator.

These helpers preserve the compact test-facing call shapes historically used
by compiler tests.  They do not contain an expression evaluator or a second
state machine: every result comes from the serialized primitive simulation
plan and the native runtime.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
import threading

from zlang.ir.module import Module
from zlang.ir import csr as ir_csr
from zlang.ir import expressions as ir_expr
from zlang.ir.cdc import CrossingKind
from zlang.ir.packing import unpack_runtime
from zlang.sim import Program, SimulationRuntimeError, Simulator, _native_runtime
from zlang.simulation_errors import (
    ProtocolViolation,
    SimulationError,
    VerificationAssertionError,
    VerificationCoverWitness,
    VerificationRequirementViolation,
)
from zlang.simulation_plan_build import build_simulation_plan
from zlang.simulation_csr import csr_field_state_register_name
from zlang.simulation_primitives import int_from_limbs
from zlang.verification_monitor import VerificationMonitor, VerificationSampleResult


_PROGRAM_CACHE_ENTRIES = 8
_PROGRAM_CACHE: OrderedDict[int, tuple[Module, Program]] = OrderedDict()
_PROGRAM_CACHE_LOCK = threading.RLock()


def _program(module: Module) -> Program:
    key = id(module)
    with _PROGRAM_CACHE_LOCK:
        cached = _PROGRAM_CACHE.get(key)
        if cached is not None and cached[0] is module:
            _PROGRAM_CACHE.move_to_end(key)
            return cached[1]
    plan = build_simulation_plan(module)
    runtime = _native_runtime()
    try:
        compiled = runtime.compile_plan_bytes(plan.to_bytes())
    except (ValueError, RuntimeError) as error:
        raise SimulationError(str(error)) from error
    program = Program(plan, module, compiled)
    with _PROGRAM_CACHE_LOCK:
        _PROGRAM_CACHE[key] = (module, program)
        _PROGRAM_CACHE.move_to_end(key)
        while len(_PROGRAM_CACHE) > _PROGRAM_CACHE_ENTRIES:
            _PROGRAM_CACHE.popitem(last=False)
    return program


def _translate_error(module: Module, error: SimulationRuntimeError) -> SimulationError:
    if module.has_protocol_interfaces:
        return ProtocolViolation(str(error))
    if module.fifos and str(error) == "native simulator returned status 2":
        return ProtocolViolation("FIFO overflow/underflow")
    return SimulationError(str(error))


def simulate(module: Module, **input_values: object) -> dict[str, object]:
    """Evaluate one combinational typed module through the native plan."""

    if module.is_sequential:
        raise SimulationError("use simulate_cycles for a sequential module")
    try:
        with _program(module).create() as instance:
            for name, value in input_values.items():
                instance.set(name, value)
            return instance.eval()
    except SimulationRuntimeError as error:
        raise _translate_error(module, error) from error


def _single_domain(module: Module) -> tuple[str, str | None]:
    if len(module.clock_domains) == 1:
        domain = module.clock_domains[0]
        return domain.clock, domain.reset
    if module.clock is not None:
        return module.clock, module.reset
    raise SimulationError("cycle simulation requires exactly one clock domain")


def _sample_single_clock_cycle(
    instance: Simulator,
    clock: str,
    *,
    reset_asserted: bool,
) -> dict[str, object]:
    """Preserve the public pre-edge sample and post-reset sample convention."""

    if reset_asserted:
        instance.edge(clock)
        return instance.eval()
    result = instance.eval()
    instance.edge(clock)
    return result


def simulate_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
    *,
    sample_verification: bool = True,  # noqa: ARG001 - native always samples overlays
) -> list[dict[str, object]]:
    """Run single-domain cycles through one persistent native instance."""

    if not module.is_sequential:
        raise SimulationError("simulate_cycles requires a sequential module")
    cycles = tuple(input_cycles)
    resets = tuple(reset) if reset is not None else (False,) * len(cycles)
    if len(resets) != len(cycles):
        raise SimulationError("reset sequence length must match input cycles")
    clock, reset_name = _single_domain(module)
    try:
        with _program(module).create() as instance:
            results = []
            reset_asserted: bool | None = None
            for inputs, asserted in zip(cycles, resets, strict=True):
                for name, value in inputs.items():
                    instance.set(name, value)
                if reset_name is not None and asserted != reset_asserted:
                    instance.reset(reset_name, asserted=asserted)
                    reset_asserted = asserted
                results.append(
                    _sample_single_clock_cycle(
                        instance,
                        clock,
                        reset_asserted=asserted,
                    )
                )
            return results
    except SimulationRuntimeError as error:
        raise _translate_error(module, error) from error


def simulate_multiclock_steps(
    module: Module,
    input_steps: Iterable[dict[str, object]],
    domain_edges: Iterable[set[str]],
    resets: Iterable[set[str]] | None = None,
) -> list[dict[str, object]]:
    """Run explicit coincident-domain edge events through the native runtime."""

    steps = tuple(input_steps)
    edges = tuple(domain_edges)
    reset_steps = tuple(resets) if resets is not None else (set(),) * len(steps)
    if len(edges) != len(steps) or len(reset_steps) != len(steps):
        raise SimulationError(
            "multi-clock input, edge, and reset schedules must have equal lengths"
        )
    reset_by_clock = {
        domain.clock: domain.reset
        for domain in module.clock_domains
        if domain.reset is not None
    }
    asserted: dict[str, bool] = {
        reset: False for reset in reset_by_clock.values()
    }
    try:
        with _program(module).create() as instance:
            results = []
            for inputs, selected_edges, selected_resets in zip(
                steps, edges, reset_steps, strict=True
            ):
                unknown = set(selected_resets) - set(reset_by_clock)
                if unknown:
                    raise SimulationError(
                        f"unknown reset domain '{sorted(unknown)[0]}'"
                    )
                for name, value in inputs.items():
                    instance.set(name, value)
                for domain_clock, reset_name in reset_by_clock.items():
                    asserted[reset_name] = domain_clock in selected_resets
                    instance.reset(
                        reset_name, asserted=asserted[reset_name]
                    )
                results.append(instance.eval())
                instance.edge_many(tuple(sorted(selected_edges)))
            return results
    except SimulationRuntimeError as error:
        raise _translate_error(module, error) from error


def simulate_cdc_steps(
    module: Module,
    input_steps: Iterable[dict[str, object]],
    domain_edges: Iterable[set[str]],
    resets: Iterable[set[str]] | None = None,
) -> list[dict[str, object]]:
    steps = tuple(input_steps)
    edges = tuple(domain_edges)
    reset_steps = tuple(resets) if resets is not None else (set(),) * len(steps)
    crossings = tuple(
        connection
        for connection in module.connections
        if connection.crossing is not None
    )
    if len(crossings) != 1 or len(module.clock_domains) < 2:
        raise SimulationError(
            "simulate_cdc_steps requires one explicit multi-clock crossing"
        )
    connection = crossings[0]
    crossing = connection.crossing
    assert crossing is not None
    source = connection.source
    destination = connection.destination
    assert source.domain is not None and destination.domain is not None
    endpoints = {source.domain, destination.domain}

    # These are compiler-declared CDC protocol preconditions, not a second
    # simulator.  The native machine still owns all value/state execution.
    source_toggle = stage_one = stage_two = previous_toggle = 0
    pending_pulses = 0
    for inputs, active_edges, active_resets in zip(
        steps, edges, reset_steps, strict=True
    ):
        relevant = set(active_resets) & endpoints
        if relevant and relevant != endpoints:
            raise ProtocolViolation("CDC endpoint resets must be asserted together")
        if relevant == endpoints:
            source_toggle = stage_one = stage_two = previous_toggle = 0
            pending_pulses = 0
            continue
        if crossing.kind is not CrossingKind.PULSE_TOGGLE:
            continue
        old_source_toggle = source_toggle
        old_stage_one = stage_one
        old_stage_two = stage_two
        if source.domain in active_edges and bool(inputs[source.name]):
            if pending_pulses:
                raise ProtocolViolation(
                    "pulse_toggle source pulse arrived before the previous pulse crossed"
                )
            source_toggle ^= 1
            pending_pulses += 1
        if destination.domain in active_edges:
            stage_one = old_source_toggle
            stage_two = old_stage_one
            previous_toggle = old_stage_two
            if stage_two != previous_toggle and pending_pulses:
                pending_pulses -= 1
    return simulate_multiclock_steps(module, steps, edges, reset_steps)


simulate_elastic_pipeline_cycles = simulate_cycles
simulate_hierarchical_scalar_cycles = simulate_cycles
simulate_hierarchical_ready_valid_cycles = simulate_cycles
simulate_storage_cycles = simulate_cycles
simulate_connection_cycles = simulate_cycles


def simulate_csr_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    """Run a CSR module natively and retain the historical test-state view."""

    if not module.is_sequential or not module.csr_blocks:
        raise SimulationError("simulate_csr_cycles requires a clocked CSR module")
    cycles = tuple(input_cycles)
    resets = tuple(reset) if reset is not None else (False,) * len(cycles)
    if len(resets) != len(cycles):
        raise SimulationError("reset sequence length must match input cycles")
    clock, reset_name = _single_domain(module)
    program = _program(module)
    stored = []
    for block_index, block in enumerate(module.csr_blocks):
        for register_index, register in enumerate(block.registers):
            for field_index, field in enumerate(register.fields):
                key = f"{block.name}.{register.name}.{field.name}"
                if ir_csr.access_owns_state(field.access):
                    stored.append(
                        (
                            key,
                            csr_field_state_register_name(
                                block_index,
                                register_index,
                                field_index,
                                field,
                            ),
                            field.type,
                            None,
                        )
                    )
                elif (
                    field.binding is not None
                    and field.binding.kind is ir_csr.CsrBindingKind.STATUS
                ):
                    stored.append((
                        key,
                        "",
                        field.type,
                        ir_csr.csr_hardware_source(
                            field.binding,
                            field.type,
                            origin=field.source_origin,
                        ),
                    ))

    try:
        with program.create() as instance:
            results = []
            reset_asserted: bool | None = None
            for inputs, asserted in zip(cycles, resets, strict=True):
                for name, value in inputs.items():
                    instance.set(name, value)
                if reset_name is not None and asserted != reset_asserted:
                    instance.reset(reset_name, asserted=asserted)
                    reset_asserted = asserted
                if asserted:
                    instance.edge(clock)
                result = instance.eval()
                state = {}
                for key, register_name, type_, status_source in stored:
                    if status_source is not None:
                        selected: object
                        chain: list[str] = []
                        cursor = status_source
                        while isinstance(cursor, ir_expr.FieldAccess):
                            chain.append(cursor.field)
                            cursor = cursor.expression
                        if not isinstance(cursor, ir_expr.InputRef):
                            raise SimulationError(
                                "CSR status state view requires an input/member source"
                            )
                        selected = inputs[cursor.name]
                        for field_name in reversed(chain):
                            if not isinstance(selected, Mapping):
                                raise SimulationError(
                                    f"CSR status input '{cursor.name}' is not structured"
                                )
                            selected = selected[field_name]
                        state[key] = selected
                    else:
                        limbs = instance._native.read_state(  # noqa: SLF001
                            "register", register_name, None
                        )
                        state[key] = unpack_runtime(
                            type_, int_from_limbs(limbs)
                        )
                results.append({**result, "state": state})
                if not asserted:
                    instance.edge(clock)
            return results
    except SimulationRuntimeError as error:
        raise _translate_error(module, error) from error


simulate_request_response_cycles = simulate_cycles
simulate_packet_arbiter_cycles = simulate_cycles
simulate_vc_credit_cycles = simulate_cycles
simulate_credit_cycles = simulate_cycles


def simulate_protocol_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    if not module.is_sequential:
        cycles = tuple(input_cycles)
        resets = tuple(reset) if reset is not None else (False,) * len(cycles)
        if len(resets) != len(cycles):
            raise SimulationError("reset sequence length must match input cycles")
        results: list[dict[str, object]] = []
        previous_inputs: Mapping[str, object] | None = None
        previous_outputs: Mapping[str, object] | None = None
        for inputs, reset_active in zip(cycles, resets, strict=True):
            if previous_inputs is not None and previous_outputs is not None:
                for port in module.inputs:
                    if port.protocol.value != "ready_valid":
                        continue
                    prior = previous_inputs.get(port.name)
                    prior_result = previous_outputs.get(port.name)
                    current = inputs.get(port.name)
                    if (
                        isinstance(prior, Mapping)
                        and isinstance(prior_result, Mapping)
                        and isinstance(current, Mapping)
                        and bool(prior.get("valid"))
                        and not bool(prior_result.get("ready"))
                        and (
                            current.get("payload") != prior.get("payload")
                            or current.get("valid") != prior.get("valid")
                        )
                    ):
                        raise ProtocolViolation(
                            f"ready/valid input '{port.name}' changed payload or "
                            "valid while stalled"
                        )
            result = simulate(module, **inputs)
            if reset_active:
                # Combinational modules have no state to reset; retain the
                # compiler-produced current-cycle value without inventing state.
                pass
            results.append(result)
            previous_inputs = inputs
            previous_outputs = result
        return results
    return simulate_cycles(module, input_cycles, reset)


class PersistentNativeSimulationState:
    """Small stateful test adapter backed by one native instance."""

    def __init__(self, module: Module) -> None:
        self.module = module
        self._instance = _program(module).create()
        self._clock, self._reset = _single_domain(module)
        self._reset_asserted: bool | None = None

    def step(self, inputs: Mapping[str, object], reset_active: bool) -> dict[str, object]:
        try:
            for name, value in inputs.items():
                self._instance.set(name, value)
            if self._reset is not None and reset_active != self._reset_asserted:
                self._instance.reset(self._reset, asserted=reset_active)
                self._reset_asserted = reset_active
            return _sample_single_clock_cycle(
                self._instance,
                self._clock,
                reset_asserted=reset_active,
            )
        except SimulationRuntimeError as error:
            raise _translate_error(self.module, error) from error

    @property
    def register_state(self) -> dict[str, object]:
        return {
            register.name: self._instance.get(register.name)
            for register in self.module.registers
        }

    def close(self) -> None:
        self._instance.close()


__all__ = [
    "PersistentNativeSimulationState",
    "ProtocolViolation",
    "SimulationError",
    "VerificationAssertionError",
    "VerificationCoverWitness",
    "VerificationMonitor",
    "VerificationRequirementViolation",
    "VerificationSampleResult",
    "simulate",
    "simulate_cdc_steps",
    "simulate_connection_cycles",
    "simulate_credit_cycles",
    "simulate_csr_cycles",
    "simulate_cycles",
    "simulate_elastic_pipeline_cycles",
    "simulate_hierarchical_ready_valid_cycles",
    "simulate_hierarchical_scalar_cycles",
    "simulate_multiclock_steps",
    "simulate_packet_arbiter_cycles",
    "simulate_protocol_cycles",
    "simulate_request_response_cycles",
    "simulate_storage_cycles",
    "simulate_vc_credit_cycles",
]
