"""Small typed-IR evaluator used for Milestone 0 behavioral checks."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from zlang.ir import expressions as expr
from zlang.ir import packing as ir_packing
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir.functional import lower_reduction
from zlang.ir.traversal import ExpressionTraversalPolicy, walk_expression
from zlang.ir.runtime_values import (
    RuntimeValueError,
    flatten_vector_value,
    normalize_scalar,
    rebuild_vector_value,
    runtime_value_fits,
    zero_runtime_value,
)
from zlang.ir.functional_regions import (
    ExactReductionOperation,
    ExactReductionPlan,
    FunctionalTable,
    evaluate_compile_time,
)
from zlang.ir import csr as ir_csr
from zlang.ir.interfaces import (
    ConnectionAdapter,
    CreditSignal,
    InterfaceProtocol,
    PacketSignal,
    ReadyValidSignal,
    RequestResponseChannel,
    RequestResponseOrdering,
    RequestResponseRole,
    VirtualChannelCreditSignal,
)
from zlang.ir.arbitration import ArbitrationPolicy, GrantScope
from zlang.ir.module import (
    Function,
    Module,
    NextAssignment,
    Port,
    Register,
    RequestResponseInterface,
    Rule,
)
from zlang.ir.module import PortDirection
from zlang.ir.cdc import CrossingKind, ResetReleaseMode
from zlang.ir.state import (
    StateActionKind,
    conditional_actions,
    select_action_groups,
)
from zlang.ir.verification import (
    VerificationGoal,
    VerificationGoalKind,
    VerificationScope,
    validate_verification_overlay,
)
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    HardwareType,
    SIntType,
    StructType,
    UFixedType,
    UIntType,
    VecType,
)
from zlang.ir.runtime_values import TaggedUnionValue
from zlang.fixed_point import apply_overflow, quantize_rational, round_ratio
from zlang.source import SourceOrigin


class SimulationError(ValueError):
    """Input values do not match a module's interface."""


def _verification_origin_text(origin: SourceOrigin | None) -> str:
    if origin is None:
        return ""
    rendered = origin.render()
    if origin.source_unit is not None:
        rendered = f"{origin.source_unit}:{rendered}"
    return f" at {rendered}"


class VerificationAssertionError(SimulationError):
    """One sampled source ``assert`` or ``ensure`` goal evaluated false."""

    def __init__(
        self,
        scope: VerificationScope,
        goal: VerificationGoal,
        cycle: int,
    ) -> None:
        self.scope_id = scope.semantic_id
        self.scope_name = scope.name
        self.goal_id = goal.semantic_id
        self.goal_name = goal.name
        self.goal_kind = goal.kind
        self.cycle = cycle
        self.source_origin = goal.source_origin
        super().__init__(
            f"verification {goal.kind.value} '{scope.name}.{goal.name}' failed "
            f"at cycle {cycle}{_verification_origin_text(goal.source_origin)}"
        )


@dataclass(frozen=True)
class VerificationRequirementViolation:
    """One environment precondition that did not hold at a sampled cycle."""

    scope_id: str
    scope_name: str
    requirement_id: str
    requirement_name: str
    cycle: int
    source_origin: SourceOrigin | None = None


@dataclass(frozen=True)
class VerificationCoverWitness:
    """The first sampled cycle at which one cover goal evaluated true."""

    scope_id: str
    scope_name: str
    goal_id: str
    goal_name: str
    cycle: int
    source_origin: SourceOrigin | None = None


@dataclass(frozen=True)
class VerificationSampleResult:
    """Events produced by one pre-edge verification sample."""

    cycle: int
    reset_suppressed: bool
    active_scope_ids: tuple[str, ...] = ()
    requirement_violations: tuple[VerificationRequirementViolation, ...] = ()
    cover_witnesses: tuple[VerificationCoverWitness, ...] = ()


class VerificationMonitor:
    """Sample typed verification scopes without mutating hardware state.

    Callers supply the values visible after combinational settling and before
    the active edge commits.  The monitor intentionally stays separate from
    the simulator entry points so every state-family simulator can use the
    same sampling semantics without changing its existing return ABI.
    """

    def __init__(
        self,
        scopes: Iterable[VerificationScope],
        functions: Iterable[Function] = (),
    ) -> None:
        self.scopes = tuple(scopes)
        validate_verification_overlay(self.scopes)

        self.functions: dict[str, Function] = {}
        for function in functions:
            previous = self.functions.get(function.name)
            if previous is not None and previous != function:
                raise ValueError(
                    f"typed function name '{function.name}' has conflicting definitions"
                )
            self.functions[function.name] = function

        self._requirement_violations: list[VerificationRequirementViolation] = []
        self._cover_witnesses: dict[str, VerificationCoverWitness] = {}

    @property
    def requirement_violations(self) -> tuple[VerificationRequirementViolation, ...]:
        return tuple(self._requirement_violations)

    @property
    def cover_witnesses(self) -> tuple[VerificationCoverWitness, ...]:
        return tuple(self._cover_witnesses.values())

    def sample(
        self,
        values: Mapping[str, object],
        cycle: int,
        reset_active: bool = False,
        *,
        clock: str | None = None,
    ) -> VerificationSampleResult:
        """Sample every scope once at ``cycle``.

        Reset suppresses requirements and goals alike.  A false requirement is
        recorded as an environment violation and gates every goal in its scope.
        False safety goals raise :class:`VerificationAssertionError`; cover
        goals record their first witness and never fail simulation.
        """

        if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 0:
            raise ValueError(
                "verification sample cycle must be a non-negative integer"
            )
        clocks = tuple(dict.fromkeys(scope.clock for scope in self.scopes))
        if clock is None:
            if len(clocks) > 1:
                raise ValueError(
                    "multi-domain verification sampling requires an explicit clock"
                )
            selected_scopes = self.scopes
        else:
            if clock not in clocks:
                raise ValueError(f"verification monitor has no clock '{clock}'")
            selected_scopes = tuple(
                scope for scope in self.scopes if scope.clock == clock
            )
        if reset_active:
            return VerificationSampleResult(cycle=cycle, reset_suppressed=True)

        sampled_values = dict(values)
        active_scope_ids: list[str] = []
        new_violations: list[VerificationRequirementViolation] = []
        new_witnesses: list[VerificationCoverWitness] = []
        for scope in selected_scopes:
            requirements_hold = True
            for requirement in scope.requirements:
                if self._evaluate_bit(
                    requirement.expression,
                    sampled_values,
                    f"requirement '{scope.name}.{requirement.name}'",
                    requirement.source_origin,
                ):
                    continue
                requirements_hold = False
                violation = VerificationRequirementViolation(
                    scope_id=scope.semantic_id,
                    scope_name=scope.name,
                    requirement_id=requirement.semantic_id,
                    requirement_name=requirement.name,
                    cycle=cycle,
                    source_origin=requirement.source_origin,
                )
                self._requirement_violations.append(violation)
                new_violations.append(violation)

            if not requirements_hold:
                continue
            active_scope_ids.append(scope.semantic_id)
            for goal in scope.goals:
                holds = self._evaluate_bit(
                    goal.expression,
                    sampled_values,
                    f"goal '{scope.name}.{goal.name}'",
                    goal.source_origin,
                )
                if goal.kind is VerificationGoalKind.COVER:
                    if holds and goal.semantic_id not in self._cover_witnesses:
                        witness = VerificationCoverWitness(
                            scope_id=scope.semantic_id,
                            scope_name=scope.name,
                            goal_id=goal.semantic_id,
                            goal_name=goal.name,
                            cycle=cycle,
                            source_origin=goal.source_origin,
                        )
                        self._cover_witnesses[goal.semantic_id] = witness
                        new_witnesses.append(witness)
                    continue
                if not holds:
                    raise VerificationAssertionError(scope, goal, cycle)

        return VerificationSampleResult(
            cycle=cycle,
            reset_suppressed=False,
            active_scope_ids=tuple(active_scope_ids),
            requirement_violations=tuple(new_violations),
            cover_witnesses=tuple(new_witnesses),
        )

    def _evaluate_bit(
        self,
        expression: expr.Expression,
        values: dict[str, object],
        label: str,
        origin: SourceOrigin | None,
    ) -> bool:
        try:
            value = _evaluate(expression, values, self.functions)
        except KeyError as error:
            missing = str(error.args[0])
            raise SimulationError(
                f"verification {label} cannot be sampled: missing value "
                f"'{missing}'{_verification_origin_text(origin)}"
            ) from error
        except SimulationError as error:
            raise SimulationError(
                f"verification {label} cannot be sampled: {error}"
                f"{_verification_origin_text(origin)}"
            ) from error
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise SimulationError(
            f"verification {label} produced non-bit value {value!r}"
            f"{_verification_origin_text(origin)}"
        )


def _effective_reset_cycles(
    module: Module,
    reset: Iterable[bool] | None,
    count: int,
) -> list[bool]:
    """Validate external reset samples and apply the typed release contract.

    The semantic simulator observes one logical reset sample per active clock
    edge.  It cannot model between-edge asynchronous assertion, but it does
    model the exact two-edge release hold of the concise ``async reset``
    contract.  Hierarchical simulators call this once at their public root and
    pass each effective sample directly to child state, avoiding duplicated
    release delays.
    """

    external = list(reset) if reset is not None else [False] * count
    if len(external) != count:
        raise SimulationError("reset sequence length must match input cycles")
    if len(module.clock_domains) != 1:
        return external
    domain = module.clock_domains[0]
    if domain.reset_release_mode is ResetReleaseMode.NATIVE:
        return external
    remaining = 0
    effective: list[bool] = []
    for asserted in external:
        if asserted:
            remaining = domain.reset_release_cycles
            effective.append(True)
        elif remaining:
            effective.append(True)
            remaining -= 1
        else:
            effective.append(False)
    return effective


_FUNCTIONAL_PREFIX = "$zlang.functional:"
_FUNCTIONAL_BINDER_PREFIX = f"{_FUNCTIONAL_PREFIX}binder:"
_FUNCTIONAL_CAPTURE_PREFIX = f"{_FUNCTIONAL_PREFIX}capture:"
_FUNCTIONAL_TABLE_PREFIX = f"{_FUNCTIONAL_PREFIX}table:"


def _function_table(module: Module) -> dict[str, Function]:
    """Return source and monomorphic callable definitions by typed IR name."""

    result: dict[str, Function] = {}
    for function in (*module.functions, *getattr(module, "callable_definitions", ())):
        previous = result.get(function.name)
        if previous is not None and previous != function:
            raise SimulationError(
                f"typed function name '{function.name}' has conflicting definitions"
            )
        result[function.name] = function
    return result


def _settle_local_values(
    module: Module,
    values: dict[str, object],
    functions: dict[str, Function],
) -> None:
    """Evaluate immutable locals against one already-built cycle snapshot."""

    pending = list(module.locals)
    while pending:
        deferred = []
        for local in pending:
            try:
                value = _evaluate(local.expression, values, functions)
            except KeyError:
                deferred.append(local)
                continue
            values[local.name] = value
        if len(deferred) == len(pending):
            raise SimulationError(
                "unresolved immutable local dependency: "
                + ", ".join(local.name for local in deferred)
            )
        pending = deferred


def _module_verification_monitor(module: Module) -> VerificationMonitor | None:
    """Build the one monitor owned by a physical module simulation."""

    if not module.verification_scopes:
        return None
    return VerificationMonitor(
        module.verification_scopes,
        (*module.functions, *module.callable_definitions),
    )


def _verification_sample_values(
    module: Module,
    settled_values: Mapping[str, object],
    cycle_outputs: Mapping[str, object],
) -> dict[str, object]:
    """Add public output leaves to an immutable pre-edge value snapshot."""

    sampled = deepcopy(dict(settled_values))
    ports = {port.name: port for port in module.ports}
    for name, value in cycle_outputs.items():
        port = ports.get(name)
        if port is None:
            continue
        if port.protocol is InterfaceProtocol.WIRE:
            sampled[name] = deepcopy(value)
            continue
        if not isinstance(value, Mapping):
            continue
        for field, field_value in value.items():
            if field in {"payload", "valid", "ready", "transfer", "send", "return"}:
                sampled[f"{name}.{field}"] = deepcopy(field_value)
    return sampled


def _sample_module_verification(
    module: Module,
    monitor: VerificationMonitor | None,
    settled_values: Mapping[str, object],
    cycle_outputs: Mapping[str, object],
    *,
    cycle: int,
    reset_active: bool,
) -> None:
    """Sample source verification after settle and before state commit."""

    if monitor is None:
        return
    monitor.sample(
        _verification_sample_values(module, settled_values, cycle_outputs),
        cycle,
        reset_active=reset_active,
        clock=module.clock if len(module.clock_domains) > 1 else None,
    )


class ProtocolViolation(SimulationError):
    """A ready/valid endpoint violated its cycle-to-cycle contract."""


def simulate(module: Module, **input_values: object) -> dict[str, object]:
    if module.is_sequential:
        raise SimulationError("use simulate_cycles for a sequential module")
    if module.has_protocol_interfaces:
        if module.request_responses:
            raise SimulationError(
                "use simulate_request_response_cycles for a request/response module"
            )
        return _simulate_protocol_module(module, input_values)
    expected = {port.name: port for port in module.inputs}
    missing = expected.keys() - input_values.keys()
    extra = input_values.keys() - expected.keys()
    if missing:
        raise SimulationError(f"missing input '{sorted(missing)[0]}'")
    if extra:
        raise SimulationError(f"unknown input '{sorted(extra)[0]}'")

    for name, port in expected.items():
        value = input_values[name]
        if not _fits(value, port.type):
            raise SimulationError(f"input '{name}' does not fit {port.type}")

    functions = _function_table(module)
    values = dict(input_values)
    pending_locals = list(module.locals)
    pending_children = {
        elaborated.instance.name: child
        for elaborated, child in zip(
            module.elaborated_instances, module.children, strict=True
        )
    }
    child_bindings = {
        (binding.instance, binding.port): binding.expression
        for binding in module.instance_bindings
    }
    while pending_locals or pending_children:
        progressed = False
        deferred_locals = []
        for local in pending_locals:
            try:
                value = _evaluate(local.expression, values, functions)
            except KeyError:
                deferred_locals.append(local)
                continue
            values[local.name] = value
            progressed = True
        pending_locals = deferred_locals

        for owner, child in tuple(pending_children.items()):
            if child.is_sequential or child.has_protocol_interfaces:
                raise SimulationError(
                    f"combinational hierarchy child '{owner}' must contain "
                    "only combinational wire ports"
                )
            child_inputs: dict[str, object] = {}
            try:
                for port in child.inputs:
                    binding = child_bindings.get((owner, port.name))
                    if binding is None:
                        raise SimulationError(
                            f"instance input '{owner}.{port.name}' is unbound"
                        )
                    child_inputs[port.name] = _evaluate(
                        binding, values, functions
                    )
            except KeyError:
                continue
            child_outputs = simulate(child, **child_inputs)
            for port in child.outputs:
                values[f"{owner}.{port.name}"] = child_outputs[port.name]
            del pending_children[owner]
            progressed = True

        if not progressed:
            unresolved = [
                *(local.name for local in pending_locals),
                *sorted(pending_children),
            ]
            raise SimulationError(
                "unresolved combinational hierarchy dependency: "
                + ", ".join(unresolved)
            )
    return {
        assignment.target.name: _evaluate(
            assignment.expression, values, functions
        )
        for assignment in module.assignments
    }


def simulate_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
    *,
    sample_verification: bool = True,
) -> list[dict[str, object]]:
    """Evaluate a sequential module cycle by cycle with simultaneous updates."""

    if not module.is_sequential:
        raise SimulationError("simulate_cycles requires a sequential module")
    if module.arbiters:
        return simulate_packet_arbiter_cycles(module, input_cycles, reset)
    if module.elastic_pipeline_regions:
        return simulate_elastic_pipeline_cycles(module, input_cycles, reset)
    if any(
        port.protocol is InterfaceProtocol.VC_CREDIT for port in module.ports
    ):
        return simulate_vc_credit_cycles(module, input_cycles, reset)
    if module.elaborated_instances and (
        module.hierarchical_connections
        or module.aggregate_protocol_connections
    ):
        return simulate_hierarchical_ready_valid_cycles(
            module, input_cycles, reset
        )
    if (
        module.elaborated_instances
        and not module.hierarchical_connections
        and not module.request_responses
        and all(
            port.protocol is InterfaceProtocol.WIRE
            for child in module.children
            for port in child.ports
        )
    ):
        return simulate_hierarchical_scalar_cycles(module, input_cycles, reset)
    if module.fifos or module.memories or module.roms:
        return simulate_storage_cycles(module, input_cycles, reset)
    if module.has_protocol_interfaces:
        if any(
            port.protocol is InterfaceProtocol.CREDIT for port in module.ports
        ):
            raise SimulationError(
                "use simulate_credit_cycles for a credit interface module"
            )
        if module.request_responses:
            raise SimulationError(
                "use simulate_request_response_cycles for a request/response module"
            )
        unsupported = tuple(
            port for port in module.ports
            if port.protocol not in {
                InterfaceProtocol.WIRE,
                InterfaceProtocol.READY_VALID,
            }
        )
        if unsupported:
            raise SimulationError(
                "simulate_cycles supports only wire and ready/valid ports for "
                "a generic sequential protocol module"
            )
        cycles = list(input_cycles)
        resets = _effective_reset_cycles(module, reset, len(cycles))
        state = _PersistentStorageSimulationState(module)
        return [
            state.step(inputs, reset_active)
            for inputs, reset_active in zip(cycles, resets, strict=True)
        ]
    cycles = list(input_cycles)
    resets = _effective_reset_cycles(module, reset, len(cycles))
    expected = {port.name: port for port in module.inputs}
    functions = _function_table(module)
    initial_state = {
        register.name: _evaluate(register.initial, {}, functions)
        for register in module.registers
    }
    state = dict(initial_state)
    delay_nodes = _module_delay_nodes(module)
    delay_stages = {
        instance: [_zero_runtime(delay.type)] * expr.sequential_stage_count(delay)
        for instance, delay in delay_nodes.items()
    }
    next_by_register = {
        assignment.target.name: assignment.expression
        for assignment in module.next_assignments
    }
    ordered_rules = _simulation_rule_schedule(module)
    results: list[dict[str, object]] = []
    verification_monitor = (
        _module_verification_monitor(module) if sample_verification else None
    )

    for cycle, (inputs, reset_active) in enumerate(
        zip(cycles, resets, strict=True)
    ):
        missing = expected.keys() - inputs.keys()
        extra = inputs.keys() - expected.keys()
        if missing:
            raise SimulationError(f"missing input '{sorted(missing)[0]}'")
        if extra:
            raise SimulationError(f"unknown input '{sorted(extra)[0]}'")
        for name, port in expected.items():
            if not _fits(inputs[name], port.type):
                raise SimulationError(f"input '{name}' does not fit {port.type}")

        if reset_active:
            state = dict(initial_state)
            delay_stages = {
                instance: [_zero_runtime(delay.type)] * expr.sequential_stage_count(delay)
                for instance, delay in delay_nodes.items()
            }
        values = {**inputs, **state}
        values.update(
            {
                f"$delay_{instance}": stages[-1]
                for instance, stages in delay_stages.items()
            }
        )
        _settle_local_values(module, values, functions)
        fired_rules: list[Rule] = []
        if not reset_active:
            if module.resolved_transition is not None:
                fired_rule_names = set(
                    _select_storage_action_groups(
                        module, values, {}, functions
                    )
                )
                fired_rules = [
                    rule for rule in ordered_rules
                    if rule.name in fired_rule_names
                ]
            else:
                # Compatibility path for hand-built legacy IR without the
                # authoritative transition graph.  Parsed ZLang modules use
                # ``ResolvedTransition`` and therefore get resource-aware
                # active-effect scheduling above.
                written_by_fired_rules: set[str] = set()
                for rule in ordered_rules:
                    active_actions = _active_rule_assignments(
                        rule, values, functions
                    )
                    targets = {
                        action.target.name for action in active_actions
                    }
                    if not bool(_evaluate(rule.guard, values, functions)):
                        continue
                    if rule.actions and not active_actions:
                        continue
                    if targets & written_by_fired_rules:
                        continue
                    fired_rules.append(rule)
                    written_by_fired_rules.update(targets)
        cycle_outputs = {
            assignment.target.name: _evaluate(
                assignment.expression, values, functions
            )
            for assignment in module.assignments
        }
        for output in module.outputs:
            if output.name in cycle_outputs:
                continue
            value: object = _zero_runtime(output.type)
            for rule in reversed(fired_rules):
                action = next(
                    (
                        item
                        for item in _active_rule_assignments(
                            rule, values, functions
                        )
                        if item.target.name == output.name
                    ),
                    None,
                )
                if action is not None:
                    value = _evaluate(action.expression, values, functions)
            cycle_outputs[output.name] = value
        _sample_module_verification(
            module,
            verification_monitor,
            values,
            cycle_outputs,
            cycle=cycle,
            reset_active=reset_active,
        )
        results.append(cycle_outputs)

        if not reset_active:
            next_state = {
                register.name: (
                    _evaluate(next_by_register[register.name], values, functions)
                    if register.name in next_by_register
                    else state[register.name]
                )
                for register in module.registers
            }
            for rule in fired_rules:
                updates = {
                    action.target.name: _evaluate(
                        action.expression, values, functions
                    )
                    for action in _active_rule_assignments(
                        rule, values, functions
                    )
                    if isinstance(action.target, Register)
                }
                next_state.update(updates)
            next_delay_stages: dict[int, list[object]] = {}
            for instance, delay in delay_nodes.items():
                source = _evaluate(delay.expression, values, functions)
                next_delay_stages[instance] = [
                    source, *delay_stages[instance][:-1]
                ]
            state = next_state
            delay_stages = next_delay_stages

    return results


def simulate_elastic_pipeline_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    """Simulate one frozen globally-stalled ready/valid transform.

    Data and valid state share one clock enable.  Consequently a downstream
    stall freezes every selected M31 register, preserving payload stability
    without introducing an independent scheduler.
    """

    if len(module.elastic_pipeline_regions) != 1:
        raise SimulationError("elastic simulation requires exactly one region")
    region = module.elastic_pipeline_regions[0]
    source = next(port for port in module.ports if port.name == region.source_endpoint)
    destination = next(
        port for port in module.ports if port.name == region.destination_endpoint
    )
    cycles = list(input_cycles)
    resets = _effective_reset_cycles(module, reset, len(cycles))
    functions = _function_table(module)
    root = region.selected_candidate.expression
    staged: dict[int, expr.Delay | expr.Pipeline] = {}
    _collect_delays(root, staged)
    if any(isinstance(node, expr.Delay) for node in staged.values()):
        raise SimulationError("elastic selected plan contains an unsupported Delay")
    if tuple(sorted((node.instance, node.stages) for node in staged.values())) != (
        region.plan.data_stage_instances
    ):
        raise SimulationError("elastic selected data stages disagree with its plan")
    delay_stages = {
        instance: [_zero_runtime(node.type)] * node.stages
        for instance, node in staged.items()
    }
    valid_stages = [0] * region.timing.capacity
    results: list[dict[str, object]] = []
    previous_input: dict[str, object] | None = None
    previous_ready = 0
    verification_monitor = _module_verification_monitor(module)

    for cycle, (inputs, reset_active) in enumerate(
        zip(cycles, resets, strict=True)
    ):
        if set(inputs) != {source.name, destination.name}:
            raise SimulationError(
                f"elastic cycle requires '{source.name}' forward and "
                f"'{destination.name}' backward values"
            )
        source_value = inputs[source.name]
        destination_value = inputs[destination.name]
        if not isinstance(source_value, dict) or set(source_value) != {"payload", "valid"}:
            raise SimulationError(
                f"ready/valid input '{source.name}' requires fields: payload, valid"
            )
        if not isinstance(destination_value, dict) or set(destination_value) != {"ready"}:
            raise SimulationError(
                f"ready/valid output '{destination.name}' requires field: ready"
            )
        if not _fits(source_value["payload"], source.type):
            raise SimulationError(f"input '{source.name}.payload' does not fit {source.type}")
        if not _fits(source_value["valid"], BitType()) or not _fits(
            destination_value["ready"], BitType()
        ):
            raise SimulationError("ready/valid control fields must be bit")
        if (
            previous_input is not None
            and bool(previous_input["valid"])
            and not bool(previous_ready)
            and (
                not bool(source_value["valid"])
                or source_value["payload"] != previous_input["payload"]
            )
        ):
            raise ProtocolViolation(
                f"ready/valid interface '{source.name}' changed payload or valid "
                "while stalled"
            )

        output_ready = int(bool(destination_value["ready"]))
        output_valid = int(not reset_active and bool(valid_stages[-1]))
        advance = int(not reset_active and (not output_valid or output_ready))
        values: dict[str, object] = {
            f"{source.name}.payload": source_value["payload"],
            f"{source.name}.valid": int(bool(source_value["valid"])),
            f"{source.name}.ready": advance,
            f"{destination.name}.ready": output_ready,
            f"{destination.name}.valid": output_valid,
        }
        values.update(
            {
                f"$delay_{instance}": stages[-1]
                for instance, stages in delay_stages.items()
            }
        )
        output_payload = _evaluate(root, values, functions)
        transfer_in = int(bool(source_value["valid"]) and bool(advance))
        transfer_out = int(bool(output_valid) and bool(output_ready))
        cycle_result = {
            source.name: {"ready": advance, "transfer": transfer_in},
            destination.name: {
                "payload": output_payload,
                "valid": output_valid,
                "transfer": transfer_out,
            },
        }
        _sample_module_verification(
            module,
            verification_monitor,
            values,
            cycle_result,
            cycle=cycle,
            reset_active=reset_active,
        )
        results.append(cycle_result)

        if reset_active:
            delay_stages = {
                instance: [_zero_runtime(node.type)] * node.stages
                for instance, node in staged.items()
            }
            valid_stages = [0] * region.timing.capacity
        elif advance:
            delay_stages = {
                instance: [
                    _evaluate(node.expression, values, functions),
                    *delay_stages[instance][:-1],
                ]
                for instance, node in staged.items()
            }
            valid_stages = [
                int(bool(source_value["valid"])),
                *valid_stages[:-1],
            ]
        previous_input = None if reset_active else dict(source_value)
        previous_ready = advance

    return results


def simulate_hierarchical_scalar_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    """Simulate one bounded scalar child hierarchy from pre-edge snapshots.

    Child outputs are evaluated before the parent transition and injected by
    their semantic ``instance.port`` identity.  Each sequential physical child
    and the parent commit exactly once per cycle; no history replay or
    cross-module atomic scheduling is introduced.
    """

    if not module.is_sequential or not module.elaborated_instances:
        raise SimulationError(
            "hierarchical scalar simulation requires a sequential parent"
        )
    if module.hierarchical_connections:
        raise SimulationError(
            "hierarchical scalar simulation does not accept protocol connections"
        )
    children = {
        item.instance.name: child
        for item, child in zip(
            module.elaborated_instances, module.children, strict=True
        )
    }
    for owner, child in children.items():
        if any(port.protocol is not InterfaceProtocol.WIRE for port in child.ports):
            raise SimulationError(
                f"hierarchical scalar child '{owner}' has a protocol port"
            )

    binding_by_input = {
        (item.instance, item.port): item.expression
        for item in module.instance_bindings
    }
    parent_functions = _function_table(module)
    parent_state = _PersistentStorageSimulationState(module)
    expected = parent_state.expected
    child_states = {
        owner: _PersistentStorageSimulationState(child)
        for owner, child in children.items()
        if child.is_sequential
    }
    cycles = list(input_cycles)
    resets = _effective_reset_cycles(module, reset, len(cycles))
    results: list[dict[str, object]] = []

    for inputs, reset_active in zip(cycles, resets, strict=True):
        missing = expected.keys() - inputs.keys()
        extra = inputs.keys() - expected.keys()
        if missing:
            raise SimulationError(f"missing input '{sorted(missing)[0]}'")
        if extra:
            raise SimulationError(f"unknown input '{sorted(extra)[0]}'")
        register_state = (
            parent_state.initial_register_state
            if reset_active
            else parent_state.register_state
        )
        values: dict[str, object] = dict(register_state)
        for name, port in expected.items():
            value = inputs[name]
            if port.protocol is InterfaceProtocol.WIRE:
                if not _fits(value, port.type):
                    raise SimulationError(f"input '{name}' does not fit {port.type}")
                values[name] = value
                continue
            if not isinstance(value, dict):
                raise SimulationError(
                    f"ready/valid input '{name}' must be a field mapping"
                )
            required = (
                {"payload", "valid"}
                if port.direction is PortDirection.INPUT
                else {"ready"}
            )
            if set(value) != required:
                raise SimulationError(
                    f"ready/valid input '{name}' requires fields: "
                    f"{', '.join(sorted(required))}"
                )
            for field, field_value in value.items():
                type_ = port.type if field == "payload" else BitType()
                if not _fits(field_value, type_):
                    raise SimulationError(
                        f"input '{name}.{field}' does not fit {type_}"
                    )
                values[f"{name}.{field}"] = field_value
        for fifo in module.fifos:
            contents = [] if reset_active else parent_state.fifo_contents[fifo.name]
            count = len(contents)
            values[f"{fifo.name}.count"] = count
            values[f"{fifo.name}.front"] = (
                contents[0] if contents else _zero_runtime(fifo.element_type)
            )
            values[f"{fifo.name}.empty"] = int(count == 0)
            values[f"{fifo.name}.full"] = int(count == fifo.depth)
            values[f"{fifo.name}.valid"] = int(not reset_active and count != 0)
            values[f"{fifo.name}.ready"] = int(
                not reset_active and count < fifo.depth
            )
        for memory in module.memories:
            if memory.read_latency == 1:
                values[f"{memory.name}.read_data"] = (
                    _zero_runtime(memory.element_type)
                    if (
                        reset_active
                        and memory.read_data_reset.value == "clear"
                    )
                    else parent_state.memory_read_data[memory.name]
                )
                continue
            # This legacy scalar-hierarchy prepass only needs to expose parent
            # storage to child input bindings.  Latency-zero global controls
            # are ordinary typed expressions, so evaluate the same pre-edge
            # view used by the persistent state engine.
            for field, expression in (
                ("read_address", memory.read_address),
                ("write_enable", memory.write_enable),
                ("write_address", memory.write_address),
                ("write_data", memory.write_data),
                ("write_mask", memory.write_mask),
            ):
                if expression is not None:
                    values[f"${memory.name}.{field}"] = _evaluate(
                        expression, values, parent_functions
                    )
            cells = (
                [
                    _zero_runtime(memory.element_type)
                    for _ in range(memory.depth)
                ]
                if (
                    reset_active
                    and memory.contents_reset.value == "clear"
                )
                else parent_state.memory_cells[memory.name]
            )
            values[f"{memory.name}.read_data"] = _async_memory_read_value(
                memory, cells, values, reset_active=reset_active
            )
        for rom in module.roms:
            values[f"{rom.name}.read_data"] = (
                _zero_runtime(rom.element_type)
                if reset_active
                else parent_state.rom_read_data[rom.name]
            )
        pending_locals = list(module.locals)
        pending = dict(children)
        child_inputs_by_owner: dict[str, dict[str, object]] = {}
        while pending_locals or pending:
            progressed = False
            deferred_locals = []
            for local in pending_locals:
                try:
                    value = _evaluate(
                        local.expression, values, parent_functions
                    )
                except KeyError:
                    deferred_locals.append(local)
                    continue
                values[local.name] = value
                progressed = True
            pending_locals = deferred_locals
            for owner, child in tuple(pending.items()):
                child_inputs: dict[str, object] = {}
                try:
                    for port in child.inputs:
                        binding = binding_by_input.get((owner, port.name))
                        if binding is None:
                            raise SimulationError(
                                f"instance input '{owner}.{port.name}' is unbound"
                            )
                        child_inputs[port.name] = _evaluate(
                            binding, values, parent_functions
                        )
                except KeyError:
                    continue
                if child.is_sequential:
                    child_result = child_states[owner].preview(
                        child_inputs, reset_active
                    )
                else:
                    child_result = simulate(child, **child_inputs)
                for port in child.outputs:
                    values[f"{owner}.{port.name}"] = deepcopy(
                        child_result[port.name]
                    )
                child_inputs_by_owner[owner] = child_inputs
                del pending[owner]
                progressed = True
            if not progressed:
                raise SimulationError(
                    "unresolved immutable-local or hierarchical scalar "
                    "dependency: "
                    + ", ".join(
                        [
                            *(local.name for local in pending_locals),
                            *sorted(pending),
                        ]
                    )
                )

        cycle_result = parent_state.step(
            inputs,
            reset_active,
            external_values={
                name: value
                for name, value in values.items()
                if "." in name
            },
        )
        for owner, state in child_states.items():
            state.step(child_inputs_by_owner[owner], reset_active)
        results.append(cycle_result)
    return results


def simulate_hierarchical_ready_valid_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    """Simulate a closed, same-domain ready/valid child hierarchy.

    This is deliberately a small composition layer over the existing child
    storage semantics.  Each physical instance owns one persistent simulation
    state.  For every parent cycle that state may be previewed repeatedly while
    solving the combinational forward/backward ready-valid connections, then is
    committed exactly once with the converged inputs.  Thus siblings are never
    advanced in source order and all child state updates remain simultaneous at
    the shared edge.

    The initial supported boundary is intentionally conservative: one clock
    and reset domain, ready/valid ports only, direct unbuffered connections,
    and no parent-owned state or combinational assignments.  Aggregate payload
    values are supported through their ordinary typed runtime representation;
    no protocol-member or RTL-name inference is involved.
    """

    if not module.is_sequential:
        raise SimulationError(
            "hierarchical ready/valid simulation requires a sequential module"
        )
    if len(module.clock_domains) != 1:
        raise SimulationError(
            "hierarchical ready/valid simulation requires one clock domain"
        )
    if (
        module.assignments
        or module.registers
        or module.next_assignments
        or module.rules
        or module.fifos
        or module.memories
        or module.roms
        or module.instance_bindings
        or module.aggregate_protocol_connections
        or any(
            port.protocol is InterfaceProtocol.WIRE
            for child in module.children
            for port in child.ports
        )
    ):
        cycles = list(input_cycles)
        resets = _effective_reset_cycles(module, reset, len(cycles))
        state = _PersistentReadyValidHierarchySimulationState(module)
        return [
            state.step(inputs, reset_active)
            for inputs, reset_active in zip(cycles, resets, strict=True)
        ]
    if (
        module.assignments
        or module.registers
        or module.next_assignments
        or module.rules
        or module.fifos
        or module.memories
        or module.roms
    ):
        raise SimulationError(
            "hierarchical ready/valid simulation does not support parent-owned "
            "logic or state"
        )
    if module.instance_bindings:
        raise SimulationError(
            "hierarchical ready/valid simulation does not support scalar child "
            "bindings"
        )
    if module.request_responses or module.request_response_connections:
        raise SimulationError(
            "hierarchical ready/valid simulation does not support "
            "request/response channels"
        )
    if module.aggregate_protocol_connections:
        raise SimulationError(
            "hierarchical ready/valid simulation does not support aggregate "
            "protocol connection descriptors"
        )
    if len(module.children) != len(module.elaborated_instances):
        raise SimulationError(
            "hierarchical ready/valid simulation requires one concrete child "
            "per elaborated instance"
        )

    top_ports = {port.name: port for port in module.ports}
    if not top_ports or any(
        port.protocol is not InterfaceProtocol.READY_VALID
        for port in module.ports
    ):
        raise SimulationError(
            "hierarchical ready/valid simulation supports ready/valid parent "
            "ports only"
        )

    children: dict[str, Module] = {}
    endpoint_ports: dict[tuple[str, str], Port] = {
        (module.name, port.name): port for port in module.ports
    }
    for index, elaborated in enumerate(module.elaborated_instances):
        child = module.children[index]
        owner = elaborated.instance.name
        if child.name != elaborated.child_module:
            raise SimulationError(
                f"elaborated child '{owner}' names module "
                f"'{elaborated.child_module}', not '{child.name}'"
            )
        if owner in children:
            raise SimulationError(
                f"duplicate physical child instance '{owner}'"
            )
        if len(child.clock_domains) != 1:
            raise SimulationError(
                f"child '{owner}' does not use exactly one clock domain"
            )
        if elaborated.clock != module.clock or elaborated.reset != module.reset:
            raise SimulationError(
                f"child '{owner}' clock/reset does not match its parent"
            )
        if child.request_responses or any(
            port.protocol is not InterfaceProtocol.READY_VALID
            for port in child.ports
        ):
            raise SimulationError(
                f"child '{owner}' is outside the ready/valid-only simulation "
                "subset"
            )
        children[owner] = child
        endpoint_ports.update(
            ((owner, port.name), port) for port in child.ports
        )

    connected_forward: set[tuple[str, str]] = set()
    connected_backward: set[tuple[str, str]] = set()
    for connection in module.hierarchical_connections:
        if (
            connection.buffer_depth
            or connection.request_buffer_depth
            or connection.response_buffer_depth
            or connection.adapter is not None
            or connection.crossing is not None
        ):
            raise SimulationError(
                "hierarchical ready/valid simulation supports direct "
                "connections only"
            )
        source_key = (connection.source.owner, connection.source.name)
        destination_key = (
            connection.destination.owner,
            connection.destination.name,
        )
        if source_key not in endpoint_ports:
            raise SimulationError(
                f"unknown hierarchical source "
                f"'{connection.source.owner}.{connection.source.name}'"
            )
        if destination_key not in endpoint_ports:
            raise SimulationError(
                f"unknown hierarchical destination "
                f"'{connection.destination.owner}."
                f"{connection.destination.name}'"
            )
        source_port = endpoint_ports[source_key]
        destination_port = endpoint_ports[destination_key]
        if (
            source_port.protocol is not InterfaceProtocol.READY_VALID
            or destination_port.protocol is not InterfaceProtocol.READY_VALID
            or source_port.type != destination_port.type
        ):
            raise SimulationError(
                "hierarchical connection is not a type-identical ready/valid "
                "connection"
            )
        if source_key in connected_forward:
            raise SimulationError(
                f"hierarchical source '{source_key[0]}.{source_key[1]}' has "
                "multiple consumers"
            )
        if destination_key in connected_backward:
            raise SimulationError(
                f"hierarchical destination '{destination_key[0]}."
                f"{destination_key[1]}' has multiple drivers"
            )
        connected_forward.add(source_key)
        connected_backward.add(destination_key)

    for key, port in endpoint_ports.items():
        owner, name = key
        is_top_input = owner == module.name and port.direction is PortDirection.INPUT
        is_top_output = owner == module.name and port.direction is PortDirection.OUTPUT
        if port.direction is PortDirection.INPUT:
            if not is_top_input and key not in connected_backward:
                raise SimulationError(
                    f"child ready/valid input '{owner}.{name}' is unconnected"
                )
            if is_top_input and key not in connected_forward:
                raise SimulationError(
                    f"parent ready/valid input '{name}' has no consumer"
                )
        else:
            if not is_top_output and key not in connected_forward:
                raise SimulationError(
                    f"child ready/valid output '{owner}.{name}' is unconnected"
                )
            if is_top_output and key not in connected_backward:
                raise SimulationError(
                    f"parent ready/valid output '{name}' has no producer"
                )

    cycles = list(input_cycles)
    resets = _effective_reset_cycles(module, reset, len(cycles))

    child_states = {
        owner: _make_persistent_ready_valid_child_state(child)
        for owner, child in children.items()
    }
    results: list[dict[str, object]] = []
    verification_monitor = _module_verification_monitor(module)

    for cycle, (inputs, reset_active) in enumerate(
        zip(cycles, resets, strict=True)
    ):
        missing = top_ports.keys() - inputs.keys()
        extra = inputs.keys() - top_ports.keys()
        if missing:
            raise SimulationError(f"missing input '{sorted(missing)[0]}'")
        if extra:
            raise SimulationError(f"unknown input '{sorted(extra)[0]}'")

        signals: dict[tuple[str, str], dict[str, object]] = {}
        for key, port in endpoint_ports.items():
            signals[key] = {
                "payload": _zero_runtime(port.type),
                "valid": 0,
                "ready": 0,
            }
        for name, port in top_ports.items():
            value = inputs[name]
            if not isinstance(value, dict):
                raise SimulationError(
                    f"ready/valid input '{name}' must be a field mapping"
                )
            key = (module.name, name)
            if port.direction is PortDirection.INPUT:
                required = {"payload", "valid"}
                if set(value) != required:
                    raise SimulationError(
                        f"ready/valid input '{name}' requires fields: "
                        "payload, valid"
                    )
                if not _fits(value["payload"], port.type):
                    raise SimulationError(
                        f"input '{name}.payload' does not fit {port.type}"
                    )
                if not _fits(value["valid"], BitType()):
                    raise SimulationError(
                        f"input '{name}.valid' does not fit bit"
                    )
                signals[key]["payload"] = deepcopy(value["payload"])
                signals[key]["valid"] = int(value["valid"])
            else:
                if set(value) != {"ready"}:
                    raise SimulationError(
                        f"ready/valid input '{name}' requires fields: ready"
                    )
                if not _fits(value["ready"], BitType()):
                    raise SimulationError(
                        f"input '{name}.ready' does not fit bit"
                    )
                signals[key]["ready"] = int(value["ready"])

        final_child_inputs: dict[str, dict[str, object]] = {}
        converged = False
        for _ in range(max(8, len(children) * 4 + 4)):
            previous = deepcopy(signals)
            for connection in module.hierarchical_connections:
                source_key = (connection.source.owner, connection.source.name)
                destination_key = (
                    connection.destination.owner,
                    connection.destination.name,
                )
                signals[destination_key]["payload"] = deepcopy(
                    signals[source_key]["payload"]
                )
                signals[destination_key]["valid"] = int(
                    signals[source_key]["valid"]
                )
                signals[source_key]["ready"] = int(
                    signals[destination_key]["ready"]
                )

            current_inputs: dict[str, dict[str, object]] = {}
            for owner, child in children.items():
                child_inputs: dict[str, object] = {}
                for port in child.ports:
                    signal = signals[(owner, port.name)]
                    if port.direction is PortDirection.INPUT:
                        child_inputs[port.name] = {
                            "payload": deepcopy(signal["payload"]),
                            "valid": int(signal["valid"]),
                        }
                    else:
                        child_inputs[port.name] = {
                            "ready": int(signal["ready"]),
                        }
                current_inputs[owner] = child_inputs
                child_result = child_states[owner].preview(
                    child_inputs, reset_active
                )
                for port in child.ports:
                    result = child_result[port.name]
                    signal = signals[(owner, port.name)]
                    if port.direction is PortDirection.INPUT:
                        signal["ready"] = int(result["ready"])
                    else:
                        signal["payload"] = deepcopy(result["payload"])
                        signal["valid"] = int(result["valid"])
            final_child_inputs = current_inputs

            for connection in module.hierarchical_connections:
                source_key = (connection.source.owner, connection.source.name)
                destination_key = (
                    connection.destination.owner,
                    connection.destination.name,
                )
                signals[destination_key]["payload"] = deepcopy(
                    signals[source_key]["payload"]
                )
                signals[destination_key]["valid"] = int(
                    signals[source_key]["valid"]
                )
                signals[source_key]["ready"] = int(
                    signals[destination_key]["ready"]
                )
            if signals == previous:
                converged = True
                break

        if not converged:
            raise SimulationError(
                "hierarchical ready/valid combinational signals did not "
                "converge"
            )

        cycle_result: dict[str, object] = {}
        for name, port in top_ports.items():
            signal = signals[(module.name, name)]
            transfer = int(bool(signal["valid"]) and bool(signal["ready"]))
            if port.direction is PortDirection.INPUT:
                cycle_result[name] = {
                    "ready": int(signal["ready"]),
                    "transfer": transfer,
                }
            else:
                cycle_result[name] = {
                    "payload": deepcopy(signal["payload"]),
                    "valid": int(signal["valid"]),
                    "transfer": transfer,
                }
        verification_values = {
            f"{name}.{field}": deepcopy(value)
            for name in top_ports
            for field, value in signals[(module.name, name)].items()
        }
        _sample_module_verification(
            module,
            verification_monitor,
            verification_values,
            cycle_result,
            cycle=cycle,
            reset_active=reset_active,
        )
        results.append(cycle_result)
        for owner in children:
            child_states[owner].step(
                final_child_inputs[owner], reset_active
            )

    return results


def _make_persistent_ready_valid_child_state(
    module: Module,
) -> (
    "_PersistentCombinationalSimulationState"
    " | _PersistentStorageSimulationState"
    " | _PersistentReadyValidHierarchySimulationState"
):
    """Create one persistent state object for a physical hierarchy child.

    A pure ready/valid hierarchy is itself a component, not a leaf storage
    module.  Keep its physical descendants alive recursively so a parent may
    preview the whole combinational handshake graph many times and still
    commit every descendant exactly once at the shared edge.
    """

    if not module.is_sequential:
        return _PersistentCombinationalSimulationState(module)
    if module.elaborated_instances and (
        module.hierarchical_connections
        or module.aggregate_protocol_connections
    ):
        return _PersistentReadyValidHierarchySimulationState(module)
    return _PersistentStorageSimulationState(module)


class _PersistentCombinationalSimulationState:
    """Uniform preview/step adapter for a stateless hierarchy child."""

    def __init__(self, module: Module) -> None:
        if module.is_sequential:
            raise SimulationError(
                "combinational child state requires a combinational module"
            )
        self.module = module

    def preview(
        self,
        inputs: dict[str, object],
        reset_active: bool = False,
        *,
        external_values: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if external_values is not None:
            raise SimulationError(
                "combinational hierarchy child does not accept external values"
            )
        return simulate(self.module, **inputs)

    def step(
        self,
        inputs: dict[str, object],
        reset_active: bool = False,
        *,
        external_values: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self.preview(
            inputs,
            reset_active,
            external_values=external_values,
        )


class _PersistentReadyValidHierarchySimulationState:
    """Persistent recursive state for one closed ready/valid hierarchy.

    The hierarchy node and every physical descendant retain one persistent
    state object.  ``preview`` solves parent locals/state observations, scalar
    child bindings, and the complete forward/backward ready-valid graph from
    one pre-edge snapshot without mutation.  ``step`` commits the parent and
    every direct child exactly once after that solve.  Recursive children apply
    the same contract to their descendants.
    """

    def __init__(self, module: Module) -> None:
        if not module.is_sequential:
            raise SimulationError(
                "persistent ready/valid hierarchy requires a sequential module"
            )
        if len(module.clock_domains) != 1:
            raise SimulationError(
                "persistent ready/valid hierarchy requires one clock domain"
            )
        if module.request_responses or module.request_response_connections:
            raise SimulationError(
                "persistent ready/valid hierarchy does not support "
                "request/response channels"
            )
        if len(module.children) != len(module.elaborated_instances):
            raise SimulationError(
                "persistent ready/valid hierarchy requires one concrete child "
                "per elaborated instance"
            )

        self.module = module
        self.local_state = _PersistentStorageSimulationState(module)
        self.scalar_bindings = {
            (item.instance, item.port): item.expression
            for item in module.instance_bindings
        }
        self.top_ports = {port.name: port for port in module.ports}
        self.top_ready_valid_ports = {
            name: port
            for name, port in self.top_ports.items()
            if port.protocol is InterfaceProtocol.READY_VALID
        }
        self.expected_top_inputs = {
            name: port
            for name, port in self.top_ports.items()
            if port.direction is PortDirection.INPUT
            or port.protocol is InterfaceProtocol.READY_VALID
        }
        if not self.top_ready_valid_ports:
            raise SimulationError(
                "persistent ready/valid hierarchy requires at least one "
                "ready/valid parent port"
            )

        self.children: dict[str, Module] = {}
        self.endpoint_ports: dict[tuple[str, str], Port] = {
            (module.name, port.name): port
            for port in module.ports
            if port.protocol is InterfaceProtocol.READY_VALID
        }
        for index, elaborated in enumerate(module.elaborated_instances):
            child = module.children[index]
            owner = elaborated.instance.name
            if child.name != elaborated.child_module:
                raise SimulationError(
                    f"elaborated child '{owner}' names module "
                    f"'{elaborated.child_module}', not '{child.name}'"
                )
            if owner in self.children:
                raise SimulationError(
                    f"duplicate physical child instance '{owner}'"
                )
            if len(child.clock_domains) != 1:
                raise SimulationError(
                    f"child '{owner}' does not use exactly one clock domain"
                )
            if elaborated.clock != module.clock or elaborated.reset != module.reset:
                raise SimulationError(
                    f"child '{owner}' clock/reset does not match its parent"
                )
            if child.request_responses or any(
                port.protocol
                not in (InterfaceProtocol.WIRE, InterfaceProtocol.READY_VALID)
                for port in child.ports
            ):
                raise SimulationError(
                    f"child '{owner}' is outside the mixed scalar/ready-valid "
                    "simulation "
                    "subset"
                )
            self.children[owner] = child
            self.endpoint_ports.update(
                ((owner, port.name), port)
                for port in child.ports
                if port.protocol is InterfaceProtocol.READY_VALID
            )

        self.connections: list[
            tuple[tuple[str, str], tuple[str, str]]
        ] = []
        for connection in module.hierarchical_connections:
            if (
                connection.buffer_depth
                or connection.request_buffer_depth
                or connection.response_buffer_depth
                or connection.adapter is not None
                or connection.crossing is not None
            ):
                raise SimulationError(
                    "persistent ready/valid hierarchy supports direct "
                    "connections only"
                )
            self.connections.append(
                (
                    (connection.source.owner, connection.source.name),
                    (
                        connection.destination.owner,
                        connection.destination.name,
                    ),
                )
            )
        self.connections.extend(self._aggregate_delegation_connections())

        connected_forward: set[tuple[str, str]] = set()
        connected_backward: set[tuple[str, str]] = set()
        for source_key, destination_key in self.connections:
            if source_key not in self.endpoint_ports:
                raise SimulationError(
                    f"unknown hierarchical source "
                    f"'{source_key[0]}.{source_key[1]}'"
                )
            if destination_key not in self.endpoint_ports:
                raise SimulationError(
                    f"unknown hierarchical destination "
                    f"'{destination_key[0]}.{destination_key[1]}'"
                )
            source_port = self.endpoint_ports[source_key]
            destination_port = self.endpoint_ports[destination_key]
            if (
                source_port.protocol is not InterfaceProtocol.READY_VALID
                or destination_port.protocol is not InterfaceProtocol.READY_VALID
                or source_port.type != destination_port.type
            ):
                raise SimulationError(
                    "hierarchical connection is not a type-identical ready/valid "
                    "connection"
                )
            if source_key in connected_forward:
                raise SimulationError(
                    f"hierarchical source '{source_key[0]}.{source_key[1]}' has "
                    "multiple consumers"
                )
            if destination_key in connected_backward:
                raise SimulationError(
                    f"hierarchical destination '{destination_key[0]}."
                    f"{destination_key[1]}' has multiple drivers"
                )
            connected_forward.add(source_key)
            connected_backward.add(destination_key)

        for key, port in self.endpoint_ports.items():
            owner, name = key
            is_top_input = (
                owner == module.name and port.direction is PortDirection.INPUT
            )
            is_top_output = (
                owner == module.name and port.direction is PortDirection.OUTPUT
            )
            if port.direction is PortDirection.INPUT:
                if not is_top_input and key not in connected_backward:
                    raise SimulationError(
                        f"child ready/valid input '{owner}.{name}' is unconnected"
                    )
                if is_top_input and key not in connected_forward:
                    raise SimulationError(
                        f"parent ready/valid input '{name}' has no consumer"
                    )
            else:
                if not is_top_output and key not in connected_forward:
                    raise SimulationError(
                        f"child ready/valid output '{owner}.{name}' is unconnected"
                    )
                if is_top_output and key not in connected_backward:
                    raise SimulationError(
                        f"parent ready/valid output '{name}' has no producer"
                    )

        self.child_states = {
            owner: _make_persistent_ready_valid_child_state(child)
            for owner, child in self.children.items()
        }
        self.initial_scalar_outputs = {
            f"{owner}.{port.name}": _zero_runtime(port.type)
            for owner, child in self.children.items()
            for port in child.outputs
            if port.protocol is InterfaceProtocol.WIRE
        }

    def _aggregate_delegation_connections(
        self,
    ) -> list[tuple[tuple[str, str], tuple[str, str]]]:
        """Expand typed aggregate delegation into physical member edges.

        Child-to-child aggregate connections already have authoritative
        member edges in ``hierarchical_connections``.  A top delegation keeps
        only a typed aggregate descriptor, so use its schema and role ownership
        to expose the same physical ready/valid edges to the simulator.
        """

        expanded: list[tuple[tuple[str, str], tuple[str, str]]] = []
        top_endpoints = {
            endpoint.name: endpoint
            for endpoint in self.module.aggregate_protocol_endpoints
        }
        for descriptor in self.module.aggregate_protocol_connections:
            if descriptor.crossing is not None:
                raise SimulationError(
                    "persistent aggregate hierarchy does not support crossings"
                )
            if not descriptor.delegation:
                continue
            destination_parts = descriptor.destination.split(".")
            if len(destination_parts) != 2:
                raise SimulationError(
                    "aggregate delegation destination must name one child endpoint"
                )
            owner, endpoint_name = destination_parts
            child = self.children.get(owner)
            top_endpoint = top_endpoints.get(descriptor.source)
            child_endpoint = (
                next(
                    (
                        endpoint
                        for endpoint in child.aggregate_protocol_endpoints
                        if endpoint.name == endpoint_name
                    ),
                    None,
                )
                if child is not None
                else None
            )
            if top_endpoint is None or child_endpoint is None:
                raise SimulationError(
                    "aggregate delegation references an unknown typed endpoint"
                )
            if (
                top_endpoint.protocol != child_endpoint.protocol
                or top_endpoint.role != child_endpoint.role
                or top_endpoint.specialization_identity
                != child_endpoint.specialization_identity
            ):
                raise SimulationError(
                    "aggregate delegation endpoint metadata does not match"
                )
            child_members = {
                member.name: member for member in child_endpoint.members
            }
            if {member.name for member in top_endpoint.members} != set(
                child_members
            ):
                raise SimulationError(
                    "aggregate delegation member sets do not match"
                )
            for member in top_endpoint.members:
                other = child_members[member.name]
                if (
                    member.protocol is not InterfaceProtocol.READY_VALID
                    or member.protocol is not other.protocol
                    or member.payload_type != other.payload_type
                    or member.source_role != other.source_role
                    or member.sink_role != other.sink_role
                ):
                    raise SimulationError(
                        f"aggregate delegation member '{member.name}' is "
                        "outside the ready/valid simulation subset"
                    )
                top_key = (
                    self.module.name,
                    f"{top_endpoint.name}__{member.name}",
                )
                child_key = (owner, f"{child_endpoint.name}__{member.name}")
                if top_endpoint.role == member.source_role:
                    expanded.append((child_key, top_key))
                elif top_endpoint.role == member.sink_role:
                    expanded.append((top_key, child_key))
                else:
                    raise SimulationError(
                        f"aggregate delegation role '{top_endpoint.role}' does "
                        f"not own member '{member.name}'"
                    )
        return expanded

    def preview(
        self,
        inputs: dict[str, object],
        reset_active: bool = False,
        *,
        external_values: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if external_values is not None:
            raise SimulationError(
                "ready/valid hierarchy does not accept external scalar values"
            )
        return self._evaluate_cycle(inputs, reset_active, commit=False)

    def step(
        self,
        inputs: dict[str, object],
        reset_active: bool = False,
        *,
        external_values: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if external_values is not None:
            raise SimulationError(
                "ready/valid hierarchy does not accept external scalar values"
            )
        return self._evaluate_cycle(inputs, reset_active, commit=True)

    def _parent_external_values(
        self,
        signals: dict[tuple[str, str], dict[str, object]],
        scalar_outputs: dict[str, object],
    ) -> dict[str, object]:
        values = dict(scalar_outputs)
        for name, port in self.top_ready_valid_ports.items():
            signal = signals[(self.module.name, name)]
            if port.direction is PortDirection.INPUT:
                values[f"{name}.ready"] = int(signal["ready"])
            else:
                values[f"{name}.payload"] = deepcopy(signal["payload"])
                values[f"{name}.valid"] = int(signal["valid"])
        return values

    def _evaluate_cycle(
        self,
        inputs: dict[str, object],
        reset_active: bool,
        *,
        commit: bool,
    ) -> dict[str, object]:
        missing = self.expected_top_inputs.keys() - inputs.keys()
        extra = inputs.keys() - self.expected_top_inputs.keys()
        if missing:
            raise SimulationError(f"missing input '{sorted(missing)[0]}'")
        if extra:
            raise SimulationError(f"unknown input '{sorted(extra)[0]}'")

        signals: dict[tuple[str, str], dict[str, object]] = {
            key: {
                "payload": _zero_runtime(port.type),
                "valid": 0,
                "ready": 0,
            }
            for key, port in self.endpoint_ports.items()
        }
        for name, port in self.top_ready_valid_ports.items():
            value = inputs[name]
            if not isinstance(value, dict):
                raise SimulationError(
                    f"ready/valid input '{name}' must be a field mapping"
                )
            key = (self.module.name, name)
            if port.direction is PortDirection.INPUT:
                if set(value) != {"payload", "valid"}:
                    raise SimulationError(
                        f"ready/valid input '{name}' requires fields: "
                        "payload, valid"
                    )
                if not _fits(value["payload"], port.type):
                    raise SimulationError(
                        f"input '{name}.payload' does not fit {port.type}"
                    )
                if not _fits(value["valid"], BitType()):
                    raise SimulationError(
                        f"input '{name}.valid' does not fit bit"
                    )
                signals[key]["payload"] = deepcopy(value["payload"])
                signals[key]["valid"] = int(value["valid"])
            else:
                if set(value) != {"ready"}:
                    raise SimulationError(
                        f"ready/valid input '{name}' requires fields: ready"
                    )
                if not _fits(value["ready"], BitType()):
                    raise SimulationError(
                        f"input '{name}.ready' does not fit bit"
                    )
                signals[key]["ready"] = int(value["ready"])

        scalar_outputs = deepcopy(self.initial_scalar_outputs)
        final_child_inputs: dict[str, dict[str, object]] = {}
        final_parent_external_values: dict[str, object] = {}
        final_parent_outputs: dict[str, object] = {}
        converged = False
        for _ in range(max(8, len(self.children) * 4 + 4)):
            previous = (deepcopy(signals), deepcopy(scalar_outputs))
            for source_key, destination_key in self.connections:
                signals[destination_key]["payload"] = deepcopy(
                    signals[source_key]["payload"]
                )
                signals[destination_key]["valid"] = int(
                    signals[source_key]["valid"]
                )
                signals[source_key]["ready"] = int(
                    signals[destination_key]["ready"]
                )

            parent_external_values = self._parent_external_values(
                signals,
                scalar_outputs,
            )
            parent_outputs, parent_values = self.local_state.preview_values(
                inputs,
                reset_active,
                external_values=parent_external_values,
            )

            current_inputs: dict[str, dict[str, object]] = {}
            current_scalar_outputs: dict[str, object] = {}
            for owner, child in self.children.items():
                child_inputs: dict[str, object] = {}
                for port in child.ports:
                    if port.protocol is InterfaceProtocol.WIRE:
                        if port.direction is PortDirection.OUTPUT:
                            continue
                        binding = self.scalar_bindings.get((owner, port.name))
                        if binding is None:
                            raise SimulationError(
                                f"instance input '{owner}.{port.name}' is unbound"
                            )
                        child_inputs[port.name] = _evaluate(
                            binding,
                            parent_values,
                            self.local_state.functions,
                        )
                        continue
                    signal = signals[(owner, port.name)]
                    if port.direction is PortDirection.INPUT:
                        child_inputs[port.name] = {
                            "payload": deepcopy(signal["payload"]),
                            "valid": int(signal["valid"]),
                        }
                    else:
                        child_inputs[port.name] = {
                            "ready": int(signal["ready"]),
                        }
                current_inputs[owner] = child_inputs
                child_result = self.child_states[owner].preview(
                    child_inputs, reset_active
                )
                for port in child.ports:
                    if port.protocol is InterfaceProtocol.WIRE:
                        if port.direction is PortDirection.OUTPUT:
                            current_scalar_outputs[f"{owner}.{port.name}"] = (
                                deepcopy(child_result[port.name])
                            )
                        continue
                    result = child_result[port.name]
                    signal = signals[(owner, port.name)]
                    if port.direction is PortDirection.INPUT:
                        signal["ready"] = int(result["ready"])
                    else:
                        signal["payload"] = deepcopy(result["payload"])
                        signal["valid"] = int(result["valid"])
            final_child_inputs = current_inputs
            scalar_outputs = current_scalar_outputs
            final_parent_outputs = parent_outputs

            for source_key, destination_key in self.connections:
                signals[destination_key]["payload"] = deepcopy(
                    signals[source_key]["payload"]
                )
                signals[destination_key]["valid"] = int(
                    signals[source_key]["valid"]
                )
                signals[source_key]["ready"] = int(
                    signals[destination_key]["ready"]
                )
            final_parent_external_values = self._parent_external_values(
                signals,
                scalar_outputs,
            )
            if (signals, scalar_outputs) == previous:
                converged = True
                break

        if not converged:
            raise SimulationError(
                "hierarchical ready/valid combinational signals did not "
                "converge"
            )

        cycle_result: dict[str, object] = {}
        for name, port in self.top_ports.items():
            if port.protocol is InterfaceProtocol.WIRE:
                if port.direction is PortDirection.OUTPUT:
                    cycle_result[name] = deepcopy(final_parent_outputs[name])
                continue
            signal = signals[(self.module.name, name)]
            transfer = int(bool(signal["valid"]) and bool(signal["ready"]))
            if port.direction is PortDirection.INPUT:
                cycle_result[name] = {
                    "ready": int(signal["ready"]),
                    "transfer": transfer,
                }
            else:
                cycle_result[name] = {
                    "payload": deepcopy(signal["payload"]),
                    "valid": int(signal["valid"]),
                    "transfer": transfer,
                }

        if commit:
            self.local_state.step(
                inputs,
                reset_active,
                external_values=final_parent_external_values,
            )
            for owner in self.children:
                self.child_states[owner].step(
                    final_child_inputs[owner], reset_active
                )
        return cycle_result


class _PersistentStorageSimulationState:
    """Persistent pre-edge state for one sequential storage module.

    ``preview`` evaluates combinational outputs and rule selection without
    changing architectural state.  ``step`` evaluates the same pre-edge view
    and commits its transition exactly once.  The split is what lets a parent
    hierarchy solve ready/valid feedback without replaying the child's entire
    history or advancing it during a fixed-point iteration.
    """

    def __init__(self, module: Module) -> None:
        if not module.is_sequential:
            raise SimulationError(
                "persistent child simulation requires a sequential module"
            )
        self.module = module
        self.expected = {
            port.name: port
            for port in module.ports
            if port.direction is PortDirection.INPUT
            or port.protocol is InterfaceProtocol.READY_VALID
        }
        self.functions = _function_table(module)
        self.scalar_children = {
            elaborated.instance.name: child
            for elaborated, child in zip(
                module.elaborated_instances, module.children, strict=True
            )
            if all(
                port.protocol is InterfaceProtocol.WIRE
                for port in child.ports
            )
        }
        self.scalar_child_bindings = {
            (item.instance, item.port): item.expression
            for item in module.instance_bindings
            if item.instance in self.scalar_children
        }
        self.scalar_child_states = {
            owner: _PersistentStorageSimulationState(child)
            for owner, child in self.scalar_children.items()
            if child.is_sequential
        }
        self.initial_register_state = {
            register.name: _evaluate(register.initial, {}, self.functions)
            for register in module.registers
        }
        self.delay_nodes = _module_delay_nodes(module)
        try:
            self.rom_contents: dict[str, tuple[object, ...]] = {
                rom.name: tuple(
                    constant_runtime_value(word) for word in rom.contents
                )
                for rom in module.roms
            }
        except ConstantExpressionError as error:
            raise SimulationError(
                f"invalid initialized ROM contents: {error}"
            ) from error
        self.verification_monitor = _module_verification_monitor(module)
        self.verification_cycle = 0
        self._reset()

    def _fresh_state(self) -> tuple[
        dict[str, list[object]],
        dict[str, list[object]],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        module = self.module
        return (
            {fifo.name: [] for fifo in module.fifos},
            {
                memory.name: [
                    _zero_runtime(memory.element_type)
                    for _ in range(memory.depth)
                ]
                for memory in module.memories
            },
            {
                memory.name: _zero_runtime(memory.element_type)
                for memory in module.memories
            },
            {
                rom.name: _zero_runtime(rom.element_type)
                for rom in module.roms
            },
            dict(self.initial_register_state),
        )

    def _reset(self) -> None:
        (
            self.fifo_contents,
            self.memory_cells,
            self.memory_read_data,
            self.rom_read_data,
            self.register_state,
        ) = self._fresh_state()
        self.delay_stages = {
            instance: [_zero_runtime(delay.type)] * expr.sequential_stage_count(delay)
            for instance, delay in self.delay_nodes.items()
        }

    def preview(
        self,
        inputs: dict[str, object],
        reset_active: bool = False,
        *,
        external_values: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._evaluate_cycle(
            inputs,
            reset_active,
            commit=False,
            external_values=external_values,
        )

    def preview_values(
        self,
        inputs: dict[str, object],
        reset_active: bool = False,
        *,
        external_values: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Return public outputs and the complete immutable pre-edge view.

        Hierarchical composition uses this only to type/evaluate explicit
        scalar child bindings.  It does not expose state publicly and does not
        commit architectural state.
        """

        result = self._evaluate_cycle(
            inputs,
            reset_active,
            commit=False,
            external_values=external_values,
            capture_values=True,
        )
        if not isinstance(result, tuple):
            raise AssertionError("captured storage preview did not return values")
        return result

    def step(
        self,
        inputs: dict[str, object],
        reset_active: bool = False,
        *,
        external_values: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._evaluate_cycle(
            inputs,
            reset_active,
            commit=True,
            external_values=external_values,
        )

    def _evaluate_cycle(
        self,
        inputs: dict[str, object],
        reset_active: bool,
        *,
        commit: bool,
        external_values: dict[str, object] | None = None,
        capture_values: bool = False,
    ) -> dict[str, object] | tuple[dict[str, object], dict[str, object]]:
        module = self.module
        expected = self.expected
        missing = expected.keys() - inputs.keys()
        extra = inputs.keys() - expected.keys()
        if missing:
            raise SimulationError(f"missing input '{sorted(missing)[0]}'")
        if extra:
            raise SimulationError(f"unknown input '{sorted(extra)[0]}'")

        if reset_active:
            (
                fifo_contents,
                cleared_memory_cells,
                cleared_memory_read_data,
                rom_read_data,
                register_state,
            ) = self._fresh_state()
            # Writable-memory cells and their observable read result have
            # independent runtime-reset policies.  Power-up construction is
            # still deterministic zero; ``preserve`` only changes what a
            # later reset edge does to already committed state.
            memory_cells = {
                memory.name: (
                    self.memory_cells[memory.name]
                    if memory.contents_reset.value == "preserve"
                    else cleared_memory_cells[memory.name]
                )
                for memory in module.memories
            }
            memory_read_data = {
                memory.name: (
                    self.memory_read_data[memory.name]
                    if memory.read_data_reset.value == "preserve"
                    else cleared_memory_read_data[memory.name]
                )
                for memory in module.memories
            }
            delay_stages = {
                instance: [_zero_runtime(delay.type)] * expr.sequential_stage_count(delay)
                for instance, delay in self.delay_nodes.items()
            }
        else:
            fifo_contents = self.fifo_contents
            memory_cells = self.memory_cells
            memory_read_data = self.memory_read_data
            rom_read_data = self.rom_read_data
            register_state = self.register_state
            delay_stages = self.delay_stages

        values: dict[str, object] = dict(register_state)
        values.update(
            {
                f"$delay_{instance}": stages[-1]
                for instance, stages in delay_stages.items()
            }
        )
        if external_values is not None:
            values.update(external_values)
        for name, port in expected.items():
            value = inputs[name]
            if port.protocol is InterfaceProtocol.WIRE:
                if not _fits(value, port.type):
                    raise SimulationError(
                        f"input '{name}' does not fit {port.type}"
                    )
                values[name] = value
                continue
            if not isinstance(value, dict):
                raise SimulationError(
                    f"ready/valid input '{name}' must be a field mapping"
                )
            required = (
                {"payload", "valid"}
                if port.direction is PortDirection.INPUT
                else {"ready"}
            )
            if set(value) != required:
                raise SimulationError(
                    f"ready/valid input '{name}' requires fields: "
                    f"{', '.join(sorted(required))}"
                )
            for field, field_value in value.items():
                type_ = port.type if field == "payload" else BitType()
                if not _fits(field_value, type_):
                    raise SimulationError(
                        f"input '{name}.{field}' does not fit {type_}"
                    )
                values[f"{name}.{field}"] = field_value

        for fifo in module.fifos:
            contents = fifo_contents[fifo.name]
            count = len(contents)
            values[f"{fifo.name}.count"] = count
            values[f"{fifo.name}.front"] = (
                contents[0]
                if contents
                else _zero_runtime(fifo.element_type)
            )
            values[f"{fifo.name}.empty"] = int(count == 0)
            values[f"{fifo.name}.full"] = int(count == fifo.depth)
            values[f"{fifo.name}.valid"] = int(
                not reset_active and count != 0
            )
            if fifo.scheduled:
                values[f"{fifo.name}.ready"] = int(
                    not reset_active and count < fifo.depth
                )
                values[f"{fifo.name}.overflow"] = 0
                values[f"{fifo.name}.underflow"] = 0
        for memory in module.memories:
            if memory.read_latency == 1:
                values[f"{memory.name}.read_data"] = memory_read_data[memory.name]
        for rom in module.roms:
            values[f"{rom.name}.read_data"] = rom_read_data[rom.name]

        scalar_child_inputs: dict[str, dict[str, object]] = {}
        pending_locals = list(module.locals)
        pending_children = (
            dict(self.scalar_children) if external_values is None else {}
        )
        while pending_locals or pending_children:
            progressed = False
            deferred_locals = []
            for local in pending_locals:
                try:
                    value = _evaluate(
                        local.expression, values, self.functions
                    )
                except KeyError:
                    deferred_locals.append(local)
                    continue
                values[local.name] = value
                progressed = True
            pending_locals = deferred_locals

            for owner, child in tuple(pending_children.items()):
                child_inputs: dict[str, object] = {}
                try:
                    for port in child.inputs:
                        binding = self.scalar_child_bindings.get(
                            (owner, port.name)
                        )
                        if binding is None:
                            raise SimulationError(
                                f"instance input '{owner}.{port.name}' is unbound"
                            )
                        child_inputs[port.name] = _evaluate(
                            binding, values, self.functions
                        )
                except KeyError:
                    continue
                if child.is_sequential:
                    child_result = self.scalar_child_states[owner].preview(
                        child_inputs, reset_active
                    )
                else:
                    child_result = simulate(child, **child_inputs)
                for port in child.outputs:
                    values[f"{owner}.{port.name}"] = deepcopy(
                        child_result[port.name]
                    )
                scalar_child_inputs[owner] = child_inputs
                del pending_children[owner]
                progressed = True
            if not progressed:
                unresolved = [
                    *(local.name for local in pending_locals),
                    *sorted(pending_children),
                ]
                raise SimulationError(
                    "unresolved immutable-local or hierarchical scalar "
                    "dependency: " + ", ".join(unresolved)
                )

        pending_assignments = list(module.assignments)
        pending_controls: list[tuple[str, object, str, expr.Expression]] = []
        for fifo in module.fifos:
            if fifo.scheduled:
                continue
            pending_controls.extend(
                (
                    (fifo.name, fifo, "data", fifo.data),
                    (fifo.name, fifo, "push", fifo.push),
                    (fifo.name, fifo, "pop", fifo.pop),
                )
            )
        for memory in module.memories:
            if memory.scheduled:
                continue
            controls = [
                    (memory.name, memory, "read_address", memory.read_address),
                    (memory.name, memory, "write_enable", memory.write_enable),
                    (memory.name, memory, "write_address", memory.write_address),
                    (memory.name, memory, "write_data", memory.write_data),
            ]
            if memory.write_mask is not None:
                controls.append(
                    (memory.name, memory, "write_mask", memory.write_mask)
                )
            pending_controls.extend(controls)
        for rom in module.roms:
            pending_controls.append(
                (rom.name, rom, "read_address", rom.read_address)
            )

        unresolved_fifo_status = {
            fifo.name for fifo in module.fifos if not fifo.scheduled
        }
        unresolved_async_memory_reads = {
            memory.name
            for memory in module.memories
            if memory.read_latency == 0
        }
        while (
            pending_assignments
            or pending_controls
            or unresolved_fifo_status
            or unresolved_async_memory_reads
        ):
            progressed = False
            deferred_assignments = []
            for assignment in pending_assignments:
                try:
                    value = _evaluate(
                        assignment.expression, values, self.functions
                    )
                except KeyError:
                    deferred_assignments.append(assignment)
                    continue
                key = (
                    assignment.target.name
                    if assignment.signal is None
                    else f"{assignment.target.name}.{assignment.signal.value}"
                )
                values[key] = value
                progressed = True
            pending_assignments = deferred_assignments

            deferred_controls = []
            for resource_name, resource, field, expression in pending_controls:
                try:
                    value = _evaluate(expression, values, self.functions)
                except KeyError:
                    deferred_controls.append(
                        (resource_name, resource, field, expression)
                    )
                    continue
                values[f"${resource_name}.{field}"] = value
                progressed = True
            pending_controls = deferred_controls

            for fifo in module.fifos:
                if fifo.scheduled or fifo.name not in unresolved_fifo_status:
                    continue
                pop_key = f"${fifo.name}.pop"
                if pop_key not in values:
                    continue
                count = len(fifo_contents[fifo.name])
                pop_requested = bool(values[pop_key])
                dequeued = bool(
                    not reset_active and pop_requested and count > 0
                )
                values[f"${fifo.name}.dequeue"] = int(dequeued)
                values[f"{fifo.name}.ready"] = int(
                    not reset_active and (count < fifo.depth or dequeued)
                )
                values[f"{fifo.name}.underflow"] = int(
                    not reset_active and pop_requested and count == 0
                )
                push_key = f"${fifo.name}.push"
                if push_key in values:
                    push_requested = bool(values[push_key])
                    values[f"{fifo.name}.overflow"] = int(
                        not reset_active
                        and push_requested
                        and count == fifo.depth
                        and not dequeued
                    )
                    values[f"${fifo.name}.enqueue"] = int(
                        push_requested and bool(values[f"{fifo.name}.ready"])
                    )
                    unresolved_fifo_status.remove(fifo.name)
                progressed = True

            for memory in module.memories:
                if memory.name not in unresolved_async_memory_reads:
                    continue
                try:
                    values[f"{memory.name}.read_data"] = (
                        _async_memory_read_value(
                            memory,
                            memory_cells[memory.name],
                            values,
                            reset_active=reset_active,
                        )
                    )
                except KeyError:
                    continue
                unresolved_async_memory_reads.remove(memory.name)
                progressed = True

            if not progressed:
                unresolved = [
                    *(
                        assignment.target.name
                        for assignment in pending_assignments
                    ),
                    *(
                        f"{name}.{field}"
                        for name, _, field, _ in pending_controls
                    ),
                    *sorted(unresolved_fifo_status),
                    *(
                        f"{name}.read_data"
                        for name in sorted(unresolved_async_memory_reads)
                    ),
                ]
                raise SimulationError(
                    "unresolved combinational storage dependency: "
                    + ", ".join(unresolved)
                )

        fired_rule_names = (
            _select_storage_action_groups(
                module, values, fifo_contents, self.functions
            )
            if module.rules and not reset_active
            else ()
        )
        fired_rules = tuple(
            rule for rule in module.rules if rule.name in fired_rule_names
        )
        cycle_outputs: dict[str, object] = {}
        for port in module.ports:
            if port.protocol is InterfaceProtocol.WIRE:
                if port.direction is PortDirection.OUTPUT:
                    if port.name in values:
                        cycle_outputs[port.name] = values[port.name]
                    else:
                        selected = next(
                            (
                                action
                                for rule in reversed(fired_rules)
                                for action in _active_rule_assignments(
                                    rule, values, self.functions
                                )
                                if action.target.name == port.name
                            ),
                            None,
                        )
                        cycle_outputs[port.name] = (
                            _evaluate(
                                selected.expression, values, self.functions
                            )
                            if selected is not None
                            else _zero_runtime(port.type)
                        )
                continue
            if port.direction is PortDirection.INPUT:
                ready = int(values[f"{port.name}.ready"])
                cycle_outputs[port.name] = {
                    "ready": ready,
                    "transfer": int(
                        bool(values[f"{port.name}.valid"]) and bool(ready)
                    ),
                }
            else:
                valid = int(values[f"{port.name}.valid"])
                cycle_outputs[port.name] = {
                    "payload": values[f"{port.name}.payload"],
                    "valid": valid,
                    "transfer": int(
                        bool(valid) and bool(values[f"{port.name}.ready"])
                    ),
                }

        if commit:
            _sample_module_verification(
                module,
                self.verification_monitor,
                values,
                cycle_outputs,
                cycle=self.verification_cycle,
                reset_active=reset_active,
            )
            self.verification_cycle += 1
        if not commit:
            if capture_values:
                return cycle_outputs, deepcopy(values)
            return cycle_outputs
        if reset_active:
            (
                self.fifo_contents,
                self.memory_cells,
                self.memory_read_data,
                self.rom_read_data,
                self.register_state,
                self.delay_stages,
            ) = (
                fifo_contents,
                memory_cells,
                memory_read_data,
                rom_read_data,
                register_state,
                delay_stages,
            )
            if external_values is None:
                for owner, state in self.scalar_child_states.items():
                    state.step(scalar_child_inputs[owner], reset_active)
            return cycle_outputs

        next_fifo_contents = {
            name: list(contents) for name, contents in fifo_contents.items()
        }
        next_memory_cells = {
            name: list(cells) for name, cells in memory_cells.items()
        }
        next_memory_read_data = dict(memory_read_data)
        next_rom_read_data = dict(rom_read_data)
        next_register_state = dict(register_state)
        next_delay_stages = {
            instance: [
                _evaluate(delay.expression, values, self.functions),
                *delay_stages[instance][:-1],
            ]
            for instance, delay in self.delay_nodes.items()
        }
        memory_reads: dict[str, int] = {}
        memory_writes: dict[str, tuple[int, object]] = {}

        for fifo in module.fifos:
            if fifo.scheduled:
                continue
            if values[f"{fifo.name}.underflow"]:
                raise ProtocolViolation(
                    f"FIFO '{fifo.name}' underflow: pop requested while empty"
                )
            if values[f"{fifo.name}.overflow"]:
                raise ProtocolViolation(
                    f"FIFO '{fifo.name}' overflow: push requested while full"
                )
            contents = next_fifo_contents[fifo.name]
            if values[f"${fifo.name}.dequeue"]:
                contents.pop(0)
            if values[f"${fifo.name}.enqueue"]:
                contents.append(values[f"${fifo.name}.data"])

        if module.resolved_transition is not None:
            groups = {
                item.rule_name: item
                for item in module.resolved_transition.action_groups
            }
            resources = {
                item.semantic_id: item
                for item in module.resolved_transition.resources
            }
            for assignment in module.next_assignments:
                if isinstance(assignment.target, Register):
                    next_register_state[assignment.target.name] = _evaluate(
                        assignment.expression, values, self.functions
                    )
            for rule_name in fired_rule_names:
                for action in groups[rule_name].actions:
                    if action.activation is not None and not bool(
                        _evaluate(action.activation, values, self.functions)
                    ):
                        continue
                    resource = resources[action.resource_id]
                    if action.kind is StateActionKind.REGISTER_WRITE:
                        next_register_state[resource.name] = _evaluate(
                            action.operands[0], values, self.functions
                        )
                    elif action.kind is StateActionKind.FIFO_POP:
                        next_fifo_contents[resource.name] = list(
                            next_fifo_contents[resource.name][1:]
                        )
                    elif action.kind is StateActionKind.FIFO_PUSH:
                        next_fifo_contents[resource.name] = [
                            *next_fifo_contents[resource.name],
                            _evaluate(
                                action.operands[0], values, self.functions
                            ),
                        ]
                    elif action.kind is StateActionKind.MEMORY_READ_REQUEST:
                        memory_reads[resource.name] = int(
                            _evaluate(action.operands[0], values, self.functions)
                        )
                    elif action.kind is StateActionKind.MEMORY_WRITE:
                        memory_writes[resource.name] = (
                            int(_evaluate(action.operands[0], values, self.functions)),
                            _evaluate(action.operands[1], values, self.functions),
                            (
                                int(_evaluate(action.operands[2], values, self.functions))
                                if len(action.operands) == 3 else None
                            ),
                        )
                    elif action.kind is StateActionKind.OUTPUT_WRITE:
                        # Scalar outputs are combinational scheduler effects;
                        # the corresponding active Rule assignment was
                        # evaluated above and there is nothing to commit.
                        continue

        for memory in module.memories:
            if memory.scheduled:
                cells = memory_cells[memory.name]
                read_address = memory_reads.get(memory.name)
                write = memory_writes.get(memory.name)
                merged_write = (
                    _merge_memory_bytes(
                        cells[write[0]], write[1], write[2], memory.element_type
                    )
                    if write is not None and write[2] is not None else (
                        write[1] if write is not None else None
                    )
                )
                if read_address is not None:
                    next_read_data = cells[read_address]
                    if (
                        write is not None
                        and write[0] == read_address
                        and memory.collision.value == "write_first"
                    ):
                        next_read_data = merged_write
                    next_memory_read_data[memory.name] = next_read_data
                if write is not None:
                    next_memory_cells[memory.name][write[0]] = merged_write
                continue
            cells = memory_cells[memory.name]
            read_address = int(values[f"${memory.name}.read_address"])
            write_enable = bool(values[f"${memory.name}.write_enable"])
            write_address = int(values[f"${memory.name}.write_address"])
            write_data = values[f"${memory.name}.write_data"]
            merged_write = (
                _merge_memory_bytes(
                    cells[write_address], write_data,
                    int(values[f"${memory.name}.write_mask"]),
                    memory.element_type,
                )
                if memory.write_mask is not None else write_data
            )
            if memory.read_latency == 1:
                if (
                    write_enable
                    and read_address == write_address
                    and memory.collision.value == "write_first"
                ):
                    next_read_data = merged_write
                else:
                    next_read_data = cells[read_address]
                next_memory_read_data[memory.name] = next_read_data
            if write_enable:
                next_memory_cells[memory.name][write_address] = merged_write
        for rom in module.roms:
            read_address = int(values[f"${rom.name}.read_address"])
            if not 0 <= read_address < rom.depth:
                raise SimulationError(
                    f"ROM '{rom.name}' read address {read_address} is outside "
                    f"0..{rom.depth - 1}"
                )
            next_rom_read_data[rom.name] = self.rom_contents[rom.name][
                read_address
            ]

        self.fifo_contents = next_fifo_contents
        self.memory_cells = next_memory_cells
        self.memory_read_data = next_memory_read_data
        self.rom_read_data = next_rom_read_data
        self.register_state = next_register_state
        self.delay_stages = next_delay_stages
        if external_values is None:
            for owner, state in self.scalar_child_states.items():
                state.step(scalar_child_inputs[owner], reset_active)
        return cycle_outputs


def simulate_storage_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    """Simulate declared FIFOs and one-cycle synchronous memories."""

    if not module.is_sequential or not (
        module.fifos or module.memories or module.roms
    ):
        raise SimulationError(
            "simulate_storage_cycles requires a sequential storage module"
        )
    cycles = list(input_cycles)
    resets = _effective_reset_cycles(module, reset, len(cycles))

    state = _PersistentStorageSimulationState(module)
    return [
        state.step(inputs, reset_active)
        for inputs, reset_active in zip(cycles, resets, strict=True)
    ]


def _select_storage_action_groups(
    module: Module,
    values: dict[str, object],
    fifo_contents: dict[str, list[object]],
    functions: dict[str, Function],
) -> tuple[str, ...]:
    """Select a deterministic maximal legal set of atomic action groups."""

    transition = module.resolved_transition
    if transition is None:
        return ()
    guards = {
        rule.name: bool(_evaluate(rule.guard, values, functions))
        for rule in module.rules
    }
    activations = {
        action.semantic_id: bool(
            _evaluate(action.activation, values, functions)
        )
        for action in conditional_actions(transition)
        if action.activation is not None
    }
    counts = {name: len(contents) for name, contents in fifo_contents.items()}
    try:
        return select_action_groups(
            transition, guards, counts, activations
        )
    except ValueError as error:
        raise SimulationError(str(error)) from error


def _active_rule_assignments(
    rule: Rule,
    values: dict[str, object],
    functions: dict[str, Function],
) -> tuple[NextAssignment, ...]:
    """Return effects selected by one rule's nested runtime control.

    Activation predicates and effect operands are both evaluated from
    ``values``, the immutable pre-edge snapshot.  Filtering here is deliberately
    separate from scheduler selection: readiness may suppress the whole outer
    action group, but can never make an ``else`` branch participate.
    """

    return tuple(
        action
        for action in rule.actions
        if action.activation is None
        or bool(_evaluate(action.activation, values, functions))
    )


def simulate_cdc_steps(
    module: Module,
    input_steps: Iterable[dict[str, object]],
    domain_edges: Iterable[set[str]],
    resets: Iterable[set[str]] | None = None,
) -> list[dict[str, object]]:
    """Simulate one explicit CDC connection on an asynchronous edge schedule."""

    crossings = [
        connection
        for connection in module.connections
        if connection.crossing is not None
    ]
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

    steps = list(input_steps)
    edges = list(domain_edges)
    reset_steps = list(resets) if resets is not None else [set() for _ in steps]
    if len(edges) != len(steps) or len(reset_steps) != len(steps):
        raise SimulationError(
            "CDC input, edge, and reset schedules must have equal lengths"
        )
    known_domains = {domain.clock for domain in module.clock_domains}

    bit_stage_one = 0
    bit_stage_two = 0
    source_toggle = 0
    previous_toggle = 0
    pending_pulses = 0
    source_request = 0
    destination_acknowledge = 0
    acknowledge_stage_one = 0
    acknowledge_stage_two = 0
    request_stage_one = 0
    request_stage_two = 0
    held_data = _zero_runtime(source.type)
    fifo_entries: list[list[object]] = []
    source_used = 0
    returned_slots: list[int] = []
    results: list[dict[str, object]] = []

    for inputs, active_edges, active_resets in zip(
        steps, edges, reset_steps, strict=True
    ):
        unknown_edges = active_edges - known_domains
        unknown_resets = active_resets - known_domains
        if unknown_edges or unknown_resets:
            unknown = sorted(unknown_edges | unknown_resets)[0]
            raise SimulationError(f"unknown clock domain '{unknown}'")
        endpoint_domains = {source.domain, destination.domain}
        relevant_resets = active_resets & endpoint_domains
        if relevant_resets and relevant_resets != endpoint_domains:
            raise ProtocolViolation(
                "CDC endpoint resets must be asserted together"
            )

        if source.protocol is InterfaceProtocol.WIRE:
            if set(inputs) != {source.name}:
                raise SimulationError(
                    f"CDC input step requires only '{source.name}'"
                )
            if not _fits(inputs[source.name], source.type):
                raise SimulationError(
                    f"input '{source.name}' does not fit {source.type}"
                )
            source_value = int(inputs[source.name])
            source_payload = None
            source_valid = 0
            destination_ready = 0
        else:
            if set(inputs) != {source.name, destination.name}:
                raise SimulationError(
                    f"CDC input step requires '{source.name}' and "
                    f"'{destination.name}'"
                )
            source_input = inputs[source.name]
            destination_input = inputs[destination.name]
            if not isinstance(source_input, dict) or set(source_input) != {
                "payload",
                "valid",
            }:
                raise SimulationError(
                    f"ready/valid input '{source.name}' requires payload and valid"
                )
            if not isinstance(destination_input, dict) or set(destination_input) != {
                "ready"
            }:
                raise SimulationError(
                    f"ready/valid input '{destination.name}' requires ready"
                )
            if not _fits(source_input["payload"], source.type):
                raise SimulationError(
                    f"input '{source.name}.payload' does not fit {source.type}"
                )
            if not _fits(source_input["valid"], BitType()) or not _fits(
                destination_input["ready"], BitType()
            ):
                raise SimulationError("ready/valid control fields must be bit")
            source_value = 0
            source_payload = source_input["payload"]
            source_valid = int(source_input["valid"])
            destination_ready = int(destination_input["ready"])

        coordinated_reset = relevant_resets == endpoint_domains
        source_edge = source.domain in active_edges
        destination_edge = destination.domain in active_edges

        if crossing.kind is CrossingKind.SYNC_LEVEL:
            results.append(
                {destination.name: 0 if coordinated_reset else bit_stage_two}
            )
        elif crossing.kind is CrossingKind.PULSE_TOGGLE:
            results.append(
                {
                    destination.name: (
                        0
                        if coordinated_reset
                        else bit_stage_two ^ previous_toggle
                    )
                }
            )
        elif crossing.kind is CrossingKind.HANDSHAKE:
            source_ready = int(
                not coordinated_reset
                and source_request == acknowledge_stage_two
            )
            destination_valid = int(
                not coordinated_reset
                and request_stage_two != destination_acknowledge
            )
            results.append(
                {
                    source.name: {
                        "ready": source_ready,
                        "transfer": int(
                            source_edge and source_valid and source_ready
                        ),
                    },
                    destination.name: {
                        "payload": held_data,
                        "valid": destination_valid,
                        "transfer": int(
                            destination_edge
                            and destination_valid
                            and destination_ready
                        ),
                    },
                }
            )
        else:
            assert crossing.depth is not None
            visible = bool(fifo_entries and int(fifo_entries[0][1]) == 0)
            source_ready = int(
                not coordinated_reset and source_used < crossing.depth
            )
            destination_valid = int(not coordinated_reset and visible)
            destination_payload = (
                fifo_entries[0][0]
                if fifo_entries
                else _zero_runtime(source.type)
            )
            results.append(
                {
                    source.name: {
                        "ready": source_ready,
                        "transfer": int(
                            source_edge and source_valid and source_ready
                        ),
                    },
                    destination.name: {
                        "payload": destination_payload,
                        "valid": destination_valid,
                        "transfer": int(
                            destination_edge
                            and destination_valid
                            and destination_ready
                        ),
                    },
                }
            )

        if coordinated_reset:
            bit_stage_one = 0
            bit_stage_two = 0
            source_toggle = 0
            previous_toggle = 0
            pending_pulses = 0
            source_request = 0
            destination_acknowledge = 0
            acknowledge_stage_one = 0
            acknowledge_stage_two = 0
            request_stage_one = 0
            request_stage_two = 0
            held_data = _zero_runtime(source.type)
            fifo_entries = []
            source_used = 0
            returned_slots = []
        else:
            if crossing.kind is CrossingKind.SYNC_LEVEL:
                old_stage_one = bit_stage_one
                if destination_edge:
                    bit_stage_one = source_value
                    bit_stage_two = old_stage_one

            elif crossing.kind is CrossingKind.PULSE_TOGGLE:
                old_source_toggle = source_toggle
                old_stage_one = bit_stage_one
                old_stage_two = bit_stage_two
                if source_edge and source_value:
                    if pending_pulses:
                        raise ProtocolViolation(
                            "pulse_toggle source pulse arrived before the previous "
                            "pulse crossed"
                        )
                    source_toggle ^= 1
                    pending_pulses += 1
                if destination_edge:
                    bit_stage_one = old_source_toggle
                    bit_stage_two = old_stage_one
                    previous_toggle = old_stage_two
                    if bit_stage_two != previous_toggle and pending_pulses:
                        pending_pulses -= 1

            elif crossing.kind is CrossingKind.HANDSHAKE:
                source_ready_before = int(
                    source_request == acknowledge_stage_two
                )
                destination_valid_before = int(
                    request_stage_two != destination_acknowledge
                )
                source_transfer = bool(source_valid and source_ready_before)
                destination_transfer = bool(
                    destination_valid_before and destination_ready
                )
                old_source_request = source_request
                old_destination_acknowledge = destination_acknowledge
                old_ack_stage_one = acknowledge_stage_one
                old_request_stage_one = request_stage_one
                if source_edge:
                    acknowledge_stage_one = old_destination_acknowledge
                    acknowledge_stage_two = old_ack_stage_one
                    if source_transfer:
                        assert source_payload is not None
                        held_data = source_payload
                        source_request ^= 1
                if destination_edge:
                    request_stage_one = old_source_request
                    request_stage_two = old_request_stage_one
                    if destination_transfer:
                        destination_acknowledge = request_stage_two

            else:
                assert crossing.depth is not None
                visible = bool(fifo_entries and int(fifo_entries[0][1]) == 0)
                source_ready_before = int(source_used < crossing.depth)
                destination_valid_before = int(visible)
                source_transfer = bool(source_valid and source_ready_before)
                destination_transfer = bool(
                    destination_valid_before and destination_ready
                )
                if source_edge:
                    returned_slots = [delay - 1 for delay in returned_slots]
                    returned_now = sum(delay <= 0 for delay in returned_slots)
                    if returned_now:
                        source_used -= returned_now
                        returned_slots = [
                            delay for delay in returned_slots if delay > 0
                        ]
                if destination_edge:
                    for entry in fifo_entries:
                        entry[1] = max(0, int(entry[1]) - 1)
                    if destination_transfer:
                        fifo_entries.pop(0)
                if source_edge and source_transfer:
                    assert source_payload is not None
                    fifo_entries.append([source_payload, 2])
                    source_used += 1
                if destination_edge and destination_transfer:
                    returned_slots.append(2)

    return results


def _simulation_rule_schedule(module: Module) -> list[Rule]:
    remaining = {rule.name: rule for rule in module.rules}
    edges = {(item.higher, item.lower) for item in module.rule_priorities}
    ordered: list[Rule] = []
    while remaining:
        ready = sorted(
            name
            for name in remaining
            if not any(lower == name and higher in remaining for higher, lower in edges)
        )
        if not ready:
            raise SimulationError("rule priority graph contains a cycle")
        for name in ready:
            ordered.append(remaining.pop(name))
    return ordered


def simulate_connection_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    """Simulate explicit protocol connections, buffers, and adapters."""

    if not module.is_sequential or not module.connections:
        raise SimulationError(
            "simulate_connection_cycles requires a clocked module with connections"
        )
    cycles = list(input_cycles)
    resets = _effective_reset_cycles(module, reset, len(cycles))

    expected = {
        port.name: port
        for port in module.ports
        if port.direction is PortDirection.INPUT
        or (
            port.direction is PortDirection.OUTPUT
            and port.protocol is not InterfaceProtocol.WIRE
        )
    }
    queues: dict[int, list[object]] = {
        index: []
        for index, connection in enumerate(module.connections)
        if connection.buffer_depth
    }
    credits: dict[int, int] = {
        index: _credit_capacity(connection.destination)
        for index, connection in enumerate(module.connections)
        if connection.adapter is ConnectionAdapter.READY_VALID_TO_CREDIT
    }
    results: list[dict[str, object]] = []
    previous_inputs: dict[str, object] | None = None
    previous_outputs: dict[str, object] | None = None
    verification_monitor = _module_verification_monitor(module)

    for cycle, (inputs, reset_active) in enumerate(
        zip(cycles, resets, strict=True)
    ):
        missing = expected.keys() - inputs.keys()
        extra = inputs.keys() - expected.keys()
        if missing:
            raise SimulationError(f"missing input '{sorted(missing)[0]}'")
        if extra:
            raise SimulationError(f"unknown input '{sorted(extra)[0]}'")
        if reset_active:
            queues = {index: [] for index in queues}
            credits = {
                index: _credit_capacity(connection.destination)
                for index, connection in enumerate(module.connections)
                if connection.adapter
                is ConnectionAdapter.READY_VALID_TO_CREDIT
            }

        values: dict[str, object] = {}
        for name, port in expected.items():
            value = inputs[name]
            if port.protocol is InterfaceProtocol.WIRE:
                if not _fits(value, port.type):
                    raise SimulationError(f"input '{name}' does not fit {port.type}")
                values[name] = value
                continue
            if not isinstance(value, dict):
                raise SimulationError(
                    f"protocol input '{name}' must be a field mapping"
                )
            if port.protocol is InterfaceProtocol.READY_VALID:
                required = (
                    {"payload", "valid"}
                    if port.direction is PortDirection.INPUT
                    else {"ready"}
                )
                if set(value) != required:
                    raise SimulationError(
                        f"ready/valid input '{name}' requires fields: "
                        f"{', '.join(sorted(required))}"
                    )
                if port.direction is PortDirection.INPUT:
                    if not _fits(value["payload"], port.type):
                        raise SimulationError(
                            f"input '{name}.payload' does not fit {port.type}"
                        )
                    if not _fits(value["valid"], BitType()):
                        raise SimulationError(
                            f"input '{name}.valid' does not fit bit"
                        )
                    values[f"{name}.payload"] = value["payload"]
                    values[f"{name}.valid"] = value["valid"]
                else:
                    if not _fits(value["ready"], BitType()):
                        raise SimulationError(
                            f"input '{name}.ready' does not fit bit"
                        )
                    values[f"{name}.ready"] = value["ready"]
                continue

            required = (
                {"payload", "send"}
                if port.direction is PortDirection.INPUT
                else {"return"}
            )
            if set(value) != required:
                raise SimulationError(
                    f"credit input '{name}' requires fields: "
                    f"{', '.join(sorted(required))}"
                )
            if port.direction is PortDirection.INPUT:
                if not _fits(value["payload"], port.type):
                    raise SimulationError(
                        f"input '{name}.payload' does not fit {port.type}"
                    )
                if not _fits(value["send"], BitType()):
                    raise SimulationError(f"input '{name}.send' does not fit bit")
                values[f"{name}.payload"] = value["payload"]
                values[f"{name}.send"] = value["send"]
            else:
                if not _fits(value["return"], BitType()):
                    raise SimulationError(
                        f"input '{name}.return' does not fit bit"
                    )
                values[f"{name}.return"] = value["return"]

        cycle_result: dict[str, object] = {}
        for index, connection in enumerate(module.connections):
            source = connection.source
            destination = connection.destination
            if source.protocol is InterfaceProtocol.WIRE:
                cycle_result[destination.name] = values[source.name]
                continue

            if connection.adapter is ConnectionAdapter.READY_VALID_TO_CREDIT:
                available = credits[index]
                ready = int(not reset_active and available > 0)
                sent = int(bool(values[f"{source.name}.valid"]) and ready)
                returned = int(
                    not reset_active and bool(values[f"{destination.name}.return"])
                )
                capacity = _credit_capacity(destination)
                if returned and not sent and available == capacity:
                    raise ProtocolViolation(
                        f"rv_to_credit adapter '{source.name}->{destination.name}' "
                        "received a return at maximum credits"
                    )
                cycle_result[source.name] = {
                    "ready": ready,
                    "transfer": sent,
                }
                cycle_result[destination.name] = {
                    "payload": values[f"{source.name}.payload"],
                    "send": sent,
                    "transfer": sent,
                    "credits": available,
                }
                if not reset_active:
                    credits[index] = available - sent + returned
                continue

            if connection.adapter is ConnectionAdapter.CREDIT_TO_READY_VALID:
                queue = queues[index]
                valid = int(not reset_active and bool(queue))
                payload = queue[0] if queue else _zero_runtime(source.type)
                dequeued = int(
                    valid and bool(values[f"{destination.name}.ready"])
                )
                sent = int(
                    not reset_active and bool(values[f"{source.name}.send"])
                )
                capacity = _credit_capacity(source)
                if sent and len(queue) >= capacity and not dequeued:
                    raise ProtocolViolation(
                        f"credit_to_rv adapter '{source.name}->{destination.name}' "
                        "received a transfer without credit"
                    )
                cycle_result[source.name] = {
                    "return": dequeued,
                    "transfer": sent,
                }
                cycle_result[destination.name] = {
                    "payload": payload,
                    "valid": valid,
                    "transfer": dequeued,
                }
                if not reset_active:
                    next_queue = list(queue)
                    if dequeued:
                        next_queue.pop(0)
                    if sent:
                        next_queue.append(values[f"{source.name}.payload"])
                    queues[index] = next_queue
                continue

            if source.protocol is InterfaceProtocol.READY_VALID:
                if connection.buffer_depth:
                    queue = queues[index]
                    valid = int(not reset_active and bool(queue))
                    payload = queue[0] if queue else _zero_runtime(source.type)
                    dequeued = int(
                        valid and bool(values[f"{destination.name}.ready"])
                    )
                    ready = int(
                        not reset_active
                        and (
                            len(queue) < connection.buffer_depth
                            or bool(dequeued)
                        )
                    )
                    enqueued = int(
                        ready and bool(values[f"{source.name}.valid"])
                    )
                    if not reset_active:
                        next_queue = list(queue)
                        if dequeued:
                            next_queue.pop(0)
                        if enqueued:
                            next_queue.append(values[f"{source.name}.payload"])
                        queues[index] = next_queue
                else:
                    ready = int(bool(values[f"{destination.name}.ready"]))
                    valid = int(bool(values[f"{source.name}.valid"]))
                    payload = values[f"{source.name}.payload"]
                    enqueued = dequeued = int(ready and valid)
                cycle_result[source.name] = {
                    "ready": ready,
                    "transfer": enqueued,
                }
                cycle_result[destination.name] = {
                    "payload": payload,
                    "valid": valid,
                    "transfer": dequeued,
                }
                continue

            sent = int(not reset_active and bool(values[f"{source.name}.send"]))
            returned = int(
                not reset_active and bool(values[f"{destination.name}.return"])
            )
            cycle_result[source.name] = {
                "return": returned,
                "transfer": sent,
            }
            cycle_result[destination.name] = {
                "payload": values[f"{source.name}.payload"],
                "send": sent,
                "transfer": sent,
            }

        if previous_inputs is not None and previous_outputs is not None:
            _check_ready_valid_stability(
                module,
                previous_inputs,
                previous_outputs,
                inputs,
                cycle_result,
            )
        _sample_module_verification(
            module,
            verification_monitor,
            values,
            cycle_result,
            cycle=cycle,
            reset_active=reset_active,
        )
        results.append(cycle_result)
        previous_inputs = inputs
        previous_outputs = cycle_result

    return results


def simulate_csr_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    """Simulate the fixed 32-bit CSR bus and software access policies."""

    if not module.is_sequential or not module.csr_blocks:
        raise SimulationError("simulate_csr_cycles requires a clocked CSR module")
    cycles = list(input_cycles)
    external_resets = list(reset) if reset is not None else [False] * len(cycles)
    if len(external_resets) != len(cycles):
        raise SimulationError("reset sequence length must match input cycles")
    has_user_state = bool(
        module.registers
        or module.next_assignments
        or module.rules
        or module.assignments
    )
    # CSR storage and ordinary rule/register storage are disjoint typed state
    # contributors.  Reuse the existing cycle simulator for the latter and
    # merge its public outputs with the CSR bus/state result below; this keeps
    # one pre-edge snapshot and one commit per contributor without inventing a
    # second schedule or approximating CSR access as rule actions.
    user_results = (
        simulate_cycles(
            module,
            cycles,
            external_resets,
            sample_verification=False,
        )
        if has_user_state
        else None
    )
    resets = _effective_reset_cycles(module, external_resets, len(cycles))
    registers = {
        block.base_address + register.offset: (block, register)
        for block in module.csr_blocks
        for register in block.registers
    }

    def initial_state() -> dict[str, int]:
        return {
            f"{block.name}.{register.name}.{field.name}": field.reset
            for block in module.csr_blocks
            for register in block.registers
            for field in register.fields
            if field.access is not ir_csr.CsrAccess.RESERVED
            and not (
                field.binding is not None
                and field.binding.kind is ir_csr.CsrBindingKind.STATUS
            )
        }

    state = initial_state()
    results: list[dict[str, object]] = []
    internal_ports = ir_csr.csr_internal_port_names(
        module.csr_access, module.csr_blocks
    )
    hardware_inputs = {
        port.name: port for port in module.inputs if port.name not in internal_ports
    }
    verification_monitor = _module_verification_monitor(module)
    required = {"addr", "write", "wdata", "read", *hardware_inputs}
    for cycle_index, (inputs, reset_active) in enumerate(
        zip(cycles, resets, strict=True)
    ):
        if set(inputs) != required:
            missing = required - inputs.keys()
            extra = inputs.keys() - required
            if missing:
                raise SimulationError(f"missing input '{sorted(missing)[0]}'")
            raise SimulationError(f"unknown input '{sorted(extra)[0]}'")
        for name, type_ in (
            ("addr", UIntType(32)),
            ("write", BitType()),
            ("wdata", BitsType(32)),
            ("read", BitType()),
        ):
            if not _fits(inputs[name], type_):
                raise SimulationError(f"input '{name}' does not fit {type_}")
        for name, port in hardware_inputs.items():
            if not _fits(inputs[name], port.type):
                raise SimulationError(f"input '{name}' does not fit {port.type}")
        if reset_active:
            state = initial_state()

        current_state = dict(state)
        for block in module.csr_blocks:
            for register in block.registers:
                for field in register.fields:
                    if (
                        field.binding is not None
                        and field.binding.kind is ir_csr.CsrBindingKind.STATUS
                    ):
                        current_state[
                            f"{block.name}.{register.name}.{field.name}"
                        ] = int(inputs[field.binding.signal])

        address = int(inputs["addr"])
        mapped = registers.get(address)
        ready = int(
            mapped is not None and (bool(inputs["read"]) or bool(inputs["write"]))
        )
        rdata = 0
        if mapped is not None and inputs["read"]:
            block, register = mapped
            for field in register.fields:
                if field.access not in {
                    ir_csr.CsrAccess.READ_WRITE,
                    ir_csr.CsrAccess.READ_ONLY,
                    ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR,
                }:
                    continue
                key = f"{block.name}.{register.name}.{field.name}"
                rdata |= current_state[key] << field.lsb
        cycle_result: dict[str, object] = {
            **(
                {}
                if user_results is None
                else {
                    name: value
                    for name, value in user_results[cycle_index].items()
                    if name not in internal_ports
                }
            ),
            "rdata": rdata,
            "ready": ready,
            "state": current_state,
        }
        for block in module.csr_blocks:
            for register in block.registers:
                for field in register.fields:
                    if (
                        field.binding is not None
                        and field.binding.kind is ir_csr.CsrBindingKind.COMMAND
                    ):
                        key = f"{block.name}.{register.name}.{field.name}"
                        cycle_result[field.binding.signal] = state[key]
        verification_values: dict[str, object] = dict(inputs)
        verification_values.update(current_state)
        _sample_module_verification(
            module,
            verification_monitor,
            verification_values,
            cycle_result,
            cycle=cycle_index,
            reset_active=reset_active,
        )
        results.append(cycle_result)

        if reset_active:
            continue
        next_state = dict(state)
        for block in module.csr_blocks:
            for register in block.registers:
                for field in register.fields:
                    if field.access is ir_csr.CsrAccess.PULSE:
                        next_state[
                            f"{block.name}.{register.name}.{field.name}"
                        ] = 0
                    if (
                        field.binding is not None
                        and field.binding.kind is ir_csr.CsrBindingKind.STICKY
                    ):
                        key = f"{block.name}.{register.name}.{field.name}"
                        field_address = block.base_address + register.offset
                        clear = 0
                        if inputs["write"] and address == field_address:
                            clear = (int(inputs["wdata"]) >> field.lsb) & (
                                (1 << field.width) - 1
                            )
                        hardware_set = int(inputs[field.binding.signal])
                        if field.binding.priority is ir_csr.CsrPriority.SOFTWARE:
                            next_state[key] = (state[key] | hardware_set) & ~clear
                        else:
                            next_state[key] = (state[key] & ~clear) | hardware_set
        if mapped is not None and inputs["write"]:
            block, register = mapped
            for field in register.fields:
                key = f"{block.name}.{register.name}.{field.name}"
                incoming = (int(inputs["wdata"]) >> field.lsb) & (
                    (1 << field.width) - 1
                )
                if field.access in {
                    ir_csr.CsrAccess.READ_WRITE,
                    ir_csr.CsrAccess.WRITE_ONLY,
                    ir_csr.CsrAccess.PULSE,
                }:
                    next_state[key] = incoming
                elif field.access is ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR:
                    if not (
                        field.binding is not None
                        and field.binding.kind is ir_csr.CsrBindingKind.STICKY
                    ):
                        next_state[key] = state[key] & ~incoming
        state = next_state
    return results


def simulate_request_response_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    """Simulate bounded role-qualified request/response interfaces."""

    if not module.is_sequential or not module.request_responses:
        raise SimulationError(
            "simulate_request_response_cycles requires a sequential "
            "request/response module"
        )
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        raise SimulationError(
            "mixing request/response with other protocols is not implemented"
        )
    cycles = list(input_cycles)
    resets = _effective_reset_cycles(module, reset, len(cycles))

    wire_inputs = {
        port.name: port
        for port in module.ports
        if port.direction is PortDirection.INPUT
    }
    expected_names = wire_inputs.keys() | {
        interface.name for interface in module.request_responses
    }
    outstanding_counts = {
        interface.name: 0 for interface in module.request_responses
    }
    outstanding_ids: dict[str, list[object]] = {
        interface.name: []
        for interface in module.request_responses
        if interface.ordering is RequestResponseOrdering.OUT_OF_ORDER
    }
    functions = _function_table(module)
    results: list[dict[str, object]] = []
    verification_monitor = _module_verification_monitor(module)

    for cycle, (inputs, reset_active) in enumerate(
        zip(cycles, resets, strict=True)
    ):
        missing = expected_names - inputs.keys()
        extra = inputs.keys() - expected_names
        if missing:
            raise SimulationError(f"missing input '{sorted(missing)[0]}'")
        if extra:
            raise SimulationError(f"unknown input '{sorted(extra)[0]}'")
        if reset_active:
            outstanding_counts = {
                interface.name: 0 for interface in module.request_responses
            }
            outstanding_ids = {
                interface.name: []
                for interface in module.request_responses
                if interface.ordering is RequestResponseOrdering.OUT_OF_ORDER
            }

        values: dict[str, object] = {}
        for name, port in wire_inputs.items():
            value = inputs[name]
            if not _fits(value, port.type):
                raise SimulationError(f"input '{name}' does not fit {port.type}")
            values[name] = value
        for interface in module.request_responses:
            value = inputs[interface.name]
            if not isinstance(value, dict) or set(value) != {"request", "response"}:
                raise SimulationError(
                    f"request/response input '{interface.name}' requires request "
                    "and response mappings"
                )
            request = value["request"]
            response = value["response"]
            if interface.role is RequestResponseRole.REQUESTER:
                if not isinstance(request, dict) or set(request) != {"ready"}:
                    raise SimulationError(
                        f"request input '{interface.name}.request' requires "
                        "field: ready"
                    )
                if not isinstance(response, dict) or set(response) != {
                    "payload", "valid",
                }:
                    raise SimulationError(
                        f"response input '{interface.name}.response' requires "
                        "fields: payload, valid"
                    )
                if not _fits(request["ready"], BitType()):
                    raise SimulationError(
                        f"input '{interface.name}.request.ready' does not fit bit"
                    )
                if not _fits(response["payload"], interface.response_type):
                    raise SimulationError(
                        f"input '{interface.name}.response.payload' does not fit "
                        f"{interface.response_type}"
                    )
                if not _fits(response["valid"], BitType()):
                    raise SimulationError(
                        f"input '{interface.name}.response.valid' does not fit bit"
                    )
                values[f"{interface.name}.request.ready"] = request["ready"]
                values[f"{interface.name}.response.payload"] = response["payload"]
                values[f"{interface.name}.response.valid"] = response["valid"]
            else:
                if interface.ordering is not RequestResponseOrdering.IN_ORDER:
                    raise SimulationError(
                        "standalone out-of-order responder simulation is not "
                        "implemented"
                    )
                if not isinstance(request, dict) or set(request) != {
                    "payload", "valid",
                }:
                    raise SimulationError(
                        f"request input '{interface.name}.request' requires "
                        "fields: payload, valid"
                    )
                if not isinstance(response, dict) or set(response) != {"ready"}:
                    raise SimulationError(
                        f"response input '{interface.name}.response' requires "
                        "field: ready"
                    )
                if not _fits(request["payload"], interface.request_type):
                    raise SimulationError(
                        f"input '{interface.name}.request.payload' does not fit "
                        f"{interface.request_type}"
                    )
                if not _fits(request["valid"], BitType()):
                    raise SimulationError(
                        f"input '{interface.name}.request.valid' does not fit bit"
                    )
                if not _fits(response["ready"], BitType()):
                    raise SimulationError(
                        f"input '{interface.name}.response.ready' does not fit bit"
                    )
                values[f"{interface.name}.request.payload"] = request["payload"]
                values[f"{interface.name}.request.valid"] = request["valid"]
                values[f"{interface.name}.response.ready"] = response["ready"]

        pending = list(module.assignments)
        while pending:
            deferred = []
            for assignment in pending:
                try:
                    value = _evaluate(assignment.expression, values, functions)
                except KeyError:
                    deferred.append(assignment)
                    continue
                if isinstance(assignment.target, RequestResponseInterface):
                    interface = assignment.target
                    channel = assignment.channel
                    signal = assignment.signal
                    prefix = f"{interface.name}.{channel.value}"
                    requester = interface.role is RequestResponseRole.REQUESTER
                    if requester and (
                        channel is RequestResponseChannel.REQUEST
                        and signal is ReadyValidSignal.VALID
                    ):
                        values[f"{prefix}.valid_request"] = value
                        physical = int(
                            not reset_active
                            and bool(value)
                            and outstanding_counts[interface.name]
                            < interface.max_outstanding
                        )
                        values[f"{prefix}.valid"] = physical
                        values[f"{prefix}.transfer"] = int(
                            physical and values[f"{prefix}.ready"]
                        )
                    elif requester and (
                        channel is RequestResponseChannel.RESPONSE
                        and signal is ReadyValidSignal.READY
                    ):
                        request_transfer_key = (
                            f"{interface.name}.request.transfer"
                        )
                        if request_transfer_key not in values:
                            deferred.append(assignment)
                            continue
                        values[f"{prefix}.ready_request"] = value
                        physical = int(
                            not reset_active
                            and bool(value)
                            and (
                                outstanding_counts[interface.name] > 0
                                or (
                                    interface.ordering
                                    is RequestResponseOrdering.IN_ORDER
                                    and bool(values[request_transfer_key])
                                )
                            )
                        )
                        values[f"{prefix}.ready"] = physical
                        values[f"{prefix}.transfer"] = int(
                            physical and values[f"{prefix}.valid"]
                        )
                    elif not requester and (
                        channel is RequestResponseChannel.REQUEST
                        and signal is ReadyValidSignal.READY
                    ):
                        values[f"{prefix}.ready_request"] = value
                        physical = int(
                            not reset_active
                            and bool(value)
                            and outstanding_counts[interface.name]
                            < interface.max_outstanding
                        )
                        values[f"{prefix}.ready"] = physical
                        values[f"{prefix}.transfer"] = int(
                            physical and values[f"{prefix}.valid"]
                        )
                    elif not requester and (
                        channel is RequestResponseChannel.RESPONSE
                        and signal is ReadyValidSignal.VALID
                    ):
                        request_transfer_key = (
                            f"{interface.name}.request.transfer"
                        )
                        if request_transfer_key not in values:
                            deferred.append(assignment)
                            continue
                        values[f"{prefix}.valid_request"] = value
                        physical = int(
                            not reset_active
                            and bool(value)
                            and (
                                outstanding_counts[interface.name] > 0
                                or bool(values[request_transfer_key])
                            )
                        )
                        values[f"{prefix}.valid"] = physical
                        values[f"{prefix}.transfer"] = int(
                            physical and values[f"{prefix}.ready"]
                        )
                    else:
                        values[f"{prefix}.{signal.value}"] = value
                else:
                    values[assignment.target.name] = value
            if len(deferred) == len(pending):
                targets = ", ".join(
                    _simulation_assignment_name(assignment)
                    for assignment in deferred
                )
                raise SimulationError(
                    "unresolved combinational request/response dependency: "
                    f"{targets}"
                )
            pending = deferred

        cycle_result: dict[str, object] = {}
        for port in module.ports:
            if port.direction is PortDirection.OUTPUT:
                cycle_result[port.name] = values[port.name]
        for interface in module.request_responses:
            prefix = interface.name
            request_transfer = int(values[f"{prefix}.request.transfer"])
            response_transfer = int(values[f"{prefix}.response.transfer"])
            count = outstanding_counts[prefix]
            if interface.ordering is RequestResponseOrdering.OUT_OF_ORDER:
                ids = outstanding_ids[prefix]
                request_id = _request_response_id(
                    interface,
                    values[f"{prefix}.request.payload"],
                )
                response_id = _request_response_id(
                    interface,
                    values[f"{prefix}.response.payload"],
                )
                if request_transfer and request_id in ids:
                    raise ProtocolViolation(
                        f"request/response interface '{prefix}' issued duplicate "
                        f"outstanding ID {request_id}"
                    )
                if response_transfer and response_id not in ids:
                    raise ProtocolViolation(
                        f"request/response interface '{prefix}' received response "
                        f"for non-outstanding ID {response_id}"
                    )
                if not reset_active:
                    next_ids = list(ids)
                    if response_transfer:
                        next_ids.remove(response_id)
                    if request_transfer:
                        next_ids.append(request_id)
                    outstanding_ids[prefix] = next_ids
            if interface.role is RequestResponseRole.REQUESTER:
                cycle_result[prefix] = {
                    "request": {
                        "payload": values[f"{prefix}.request.payload"],
                        "valid": values[f"{prefix}.request.valid"],
                        "transfer": request_transfer,
                    },
                    "response": {
                        "ready": values[f"{prefix}.response.ready"],
                        "transfer": response_transfer,
                    },
                    "outstanding": count,
                }
            else:
                cycle_result[prefix] = {
                    "request": {
                        "ready": values[f"{prefix}.request.ready"],
                        "transfer": request_transfer,
                    },
                    "response": {
                        "payload": values[f"{prefix}.response.payload"],
                        "valid": values[f"{prefix}.response.valid"],
                        "transfer": response_transfer,
                    },
                    "outstanding": count,
                }
            if not reset_active:
                outstanding_counts[prefix] = (
                    count + request_transfer - response_transfer
                )
        _sample_module_verification(
            module,
            verification_monitor,
            values,
            cycle_result,
            cycle=cycle,
            reset_active=reset_active,
        )
        results.append(cycle_result)

    return results


def simulate_packet_arbiter_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    """Simulate one explicit packet arbiter with persistent grant state."""

    if not module.is_sequential or len(module.arbiters) != 1:
        raise SimulationError(
            "simulate_packet_arbiter_cycles requires one clocked packet arbiter"
        )
    arbiter = module.arbiters[0]
    endpoint_names = {
        *(source.name for source in arbiter.sources),
        arbiter.destination.name,
    }
    if {port.name for port in module.ports} != endpoint_names:
        raise SimulationError(
            "packet arbiter simulation currently requires only arbiter endpoints"
        )
    cycles = list(input_cycles)
    resets = _effective_reset_cycles(module, reset, len(cycles))

    locked = False
    owner = 0
    next_priority = 0
    previous_inputs: dict[str, object] | None = None
    previous_results: dict[str, object] | None = None
    results: list[dict[str, object]] = []
    verification_monitor = _module_verification_monitor(module)

    for cycle, (inputs, reset_active) in enumerate(
        zip(cycles, resets, strict=True)
    ):
        if set(inputs) != endpoint_names:
            missing = endpoint_names - inputs.keys()
            extra = inputs.keys() - endpoint_names
            if missing:
                raise SimulationError(f"missing input '{sorted(missing)[0]}'")
            raise SimulationError(f"unknown input '{sorted(extra)[0]}'")

        source_values: list[dict[str, object]] = []
        for source in arbiter.sources:
            value = inputs[source.name]
            if not isinstance(value, dict) or set(value) != {
                "payload",
                "valid",
                "last",
            }:
                raise SimulationError(
                    f"packet input '{source.name}' requires payload, valid, and last"
                )
            if not _fits(value["payload"], source.type):
                raise SimulationError(
                    f"input '{source.name}.payload' does not fit {source.type}"
                )
            if not _fits(value["valid"], BitType()) or not _fits(
                value["last"], BitType()
            ):
                raise SimulationError("packet valid and last fields must be bit")
            source_values.append(value)
        destination_input = inputs[arbiter.destination.name]
        if not isinstance(destination_input, dict) or set(destination_input) != {
            "ready"
        }:
            raise SimulationError(
                f"packet output '{arbiter.destination.name}' requires ready"
            )
        if not _fits(destination_input["ready"], BitType()):
            raise SimulationError("packet ready field must be bit")

        if previous_inputs is not None and previous_results is not None:
            for source in arbiter.sources:
                previous_source = previous_inputs[source.name]
                current_source = inputs[source.name]
                previous_output = previous_results[source.name]
                assert isinstance(previous_source, dict)
                assert isinstance(current_source, dict)
                assert isinstance(previous_output, dict)
                if previous_source["valid"] and not previous_output["ready"]:
                    if (
                        not current_source["valid"]
                        or current_source["payload"] != previous_source["payload"]
                        or current_source["last"] != previous_source["last"]
                    ):
                        raise ProtocolViolation(
                            f"packet source '{source.name}' changed while stalled"
                        )

        selected: int | None
        if reset_active:
            selected = None
        elif locked:
            selected = owner
        elif arbiter.policy is ArbitrationPolicy.FIXED_PRIORITY:
            selected = next(
                (
                    index
                    for index, value in enumerate(source_values)
                    if value["valid"]
                ),
                None,
            )
        else:
            selected = next(
                (
                    (next_priority + offset) % len(source_values)
                    for offset in range(len(source_values))
                    if source_values[
                        (next_priority + offset) % len(source_values)
                    ]["valid"]
                ),
                None,
            )

        selected_value = (
            source_values[selected] if selected is not None else None
        )
        destination_valid = int(
            selected_value is not None and bool(selected_value["valid"])
        )
        destination_ready = int(destination_input["ready"])
        transfer = int(destination_valid and destination_ready)
        packet_last = int(selected_value["last"]) if selected_value else 0
        payload = (
            selected_value["payload"]
            if selected_value is not None
            else _zero_runtime(arbiter.destination.type)
        )
        cycle_result: dict[str, object] = {}
        for index, source in enumerate(arbiter.sources):
            ready = int(
                selected == index and destination_valid and destination_ready
            )
            cycle_result[source.name] = {
                "ready": ready,
                "transfer": int(ready and source_values[index]["valid"]),
            }
        cycle_result[arbiter.destination.name] = {
            "payload": payload,
            "valid": destination_valid,
            "last": packet_last,
            "transfer": transfer,
            "grant": selected,
        }
        settled: dict[str, object] = {
            f"{arbiter.destination.name}.ready": destination_ready,
            f"{arbiter.destination.name}.payload": payload,
            f"{arbiter.destination.name}.valid": destination_valid,
            f"{arbiter.destination.name}.last": packet_last,
        }
        for source, value in zip(arbiter.sources, source_values, strict=True):
            settled[f"{source.name}.payload"] = value["payload"]
            settled[f"{source.name}.valid"] = value["valid"]
            settled[f"{source.name}.last"] = value["last"]
            settled[f"{source.name}.ready"] = cycle_result[source.name]["ready"]
        _sample_module_verification(
            module,
            verification_monitor,
            settled,
            cycle_result,
            cycle=cycle,
            reset_active=reset_active,
        )
        results.append(cycle_result)

        if reset_active:
            locked = False
            owner = 0
            next_priority = 0
        elif selected is not None:
            completes_grant = bool(
                transfer
                and (
                    arbiter.grant_scope is GrantScope.BEAT
                    or packet_last
                )
            )
            if completes_grant:
                locked = False
                if arbiter.policy is ArbitrationPolicy.ROUND_ROBIN:
                    next_priority = (selected + 1) % len(source_values)
            else:
                locked = True
                owner = selected

        previous_inputs = inputs
        previous_results = cycle_result

    return results


def simulate_vc_credit_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    """Simulate independent bounded credit counters for every virtual channel."""

    ports = {
        port.name: port
        for port in module.ports
        if port.protocol is InterfaceProtocol.VC_CREDIT
    }
    if not module.is_sequential or not ports:
        raise SimulationError(
            "simulate_vc_credit_cycles requires a clocked vc_credit module"
        )
    if any(
        port.protocol not in {InterfaceProtocol.WIRE, InterfaceProtocol.VC_CREDIT}
        for port in module.ports
    ):
        raise SimulationError(
            "virtual-channel credits cannot be mixed with other protocols"
        )
    cycles = list(input_cycles)
    resets = _effective_reset_cycles(module, reset, len(cycles))
    expected = {
        port.name: port
        for port in module.ports
        if port.direction is PortDirection.INPUT
        or port.protocol is InterfaceProtocol.VC_CREDIT
    }
    sender_credits = {
        port.name: [port.capacity] * port.virtual_channels
        for port in ports.values()
        if port.direction is PortDirection.OUTPUT
    }
    receiver_occupancy = {
        port.name: [0] * port.virtual_channels
        for port in ports.values()
        if port.direction is PortDirection.INPUT
    }
    functions = _function_table(module)
    results: list[dict[str, object]] = []
    verification_monitor = _module_verification_monitor(module)

    for cycle, (inputs, reset_active) in enumerate(
        zip(cycles, resets, strict=True)
    ):
        if set(inputs) != set(expected):
            missing = expected.keys() - inputs.keys()
            extra = inputs.keys() - expected.keys()
            if missing:
                raise SimulationError(f"missing input '{sorted(missing)[0]}'")
            raise SimulationError(f"unknown input '{sorted(extra)[0]}'")
        if reset_active:
            sender_credits = {
                port.name: [port.capacity] * port.virtual_channels
                for port in ports.values()
                if port.direction is PortDirection.OUTPUT
            }
            receiver_occupancy = {
                port.name: [0] * port.virtual_channels
                for port in ports.values()
                if port.direction is PortDirection.INPUT
            }

        values: dict[str, object] = {}
        for name, port in expected.items():
            value = inputs[name]
            if port.protocol is InterfaceProtocol.WIRE:
                if not _fits(value, port.type):
                    raise SimulationError(f"input '{name}' does not fit {port.type}")
                values[name] = value
                continue
            assert port.virtual_channels is not None
            vc_type = UIntType(max(1, (port.virtual_channels - 1).bit_length()))
            if not isinstance(value, dict):
                raise SimulationError(
                    f"virtual-channel credit input '{name}' must be a field mapping"
                )
            required = (
                {"payload", "vc", "send"}
                if port.direction is PortDirection.INPUT
                else {"return", "return_vc"}
            )
            if set(value) != required:
                raise SimulationError(
                    f"virtual-channel credit input '{name}' requires fields: "
                    f"{', '.join(sorted(required))}"
                )
            if port.direction is PortDirection.INPUT:
                if not _fits(value["payload"], port.type):
                    raise SimulationError(
                        f"input '{name}.payload' does not fit {port.type}"
                    )
                if not _fits(value["vc"], vc_type) or not _fits(
                    value["send"], BitType()
                ):
                    raise SimulationError(
                        f"input '{name}' has an invalid vc or send field"
                    )
                values[f"{name}.payload"] = value["payload"]
                values[f"{name}.vc"] = value["vc"]
                values[f"{name}.send"] = 0 if reset_active else value["send"]
                values[f"{name}.transfer"] = values[f"{name}.send"]
            else:
                if not _fits(value["return"], BitType()) or not _fits(
                    value["return_vc"], vc_type
                ):
                    raise SimulationError(
                        f"input '{name}' has an invalid return or return_vc field"
                    )
                values[f"{name}.return"] = 0 if reset_active else value["return"]
                values[f"{name}.return_vc"] = value["return_vc"]
                values[f"{name}.credits"] = tuple(sender_credits[name])

        pending = list(module.assignments)
        while pending:
            deferred = []
            for assignment in pending:
                try:
                    value = _evaluate(assignment.expression, values, functions)
                except KeyError:
                    deferred.append(assignment)
                    continue
                name = assignment.target.name
                signal = assignment.signal
                if signal is VirtualChannelCreditSignal.SEND:
                    values[f"{name}.send_request"] = value
                elif signal is VirtualChannelCreditSignal.RETURN:
                    values[f"{name}.return_request"] = value
                else:
                    key = name if signal is None else f"{name}.{signal.value}"
                    values[key] = value
            if len(deferred) == len(pending):
                raise SimulationError(
                    "unresolved virtual-channel credit assignment dependency"
                )
            pending = deferred

        cycle_result: dict[str, object] = {}
        for port in module.ports:
            if port.protocol is InterfaceProtocol.WIRE:
                if port.direction is PortDirection.OUTPUT:
                    cycle_result[port.name] = values[port.name]
                continue
            assert port.capacity is not None and port.virtual_channels is not None
            if port.direction is PortDirection.OUTPUT:
                vc = int(values[f"{port.name}.vc"])
                request = int(values[f"{port.name}.send_request"])
                sent = int(
                    not reset_active and request and sender_credits[port.name][vc] > 0
                )
                returned = int(values[f"{port.name}.return"])
                return_vc = int(values[f"{port.name}.return_vc"])
                counts = sender_credits[port.name]
                if returned and counts[return_vc] == port.capacity and not (
                    sent and vc == return_vc
                ):
                    raise ProtocolViolation(
                        f"vc_credit interface '{port.name}' overflow on VC {return_vc}"
                    )
                cycle_result[port.name] = {
                    "payload": values[f"{port.name}.payload"],
                    "vc": vc,
                    "send": sent,
                    "transfer": sent,
                    "credits": tuple(counts),
                }
                if not reset_active:
                    counts[vc] -= sent
                    counts[return_vc] += returned
            else:
                vc = int(values[f"{port.name}.vc"])
                sent = int(values[f"{port.name}.send"])
                returned = int(
                    not reset_active
                    and values[f"{port.name}.return_request"]
                )
                return_vc = int(values[f"{port.name}.return_vc"])
                occupancy = receiver_occupancy[port.name]
                if returned and occupancy[return_vc] == 0 and not (
                    sent and vc == return_vc
                ):
                    raise ProtocolViolation(
                        f"vc_credit interface '{port.name}' underflow on VC {return_vc}"
                    )
                if sent and occupancy[vc] == port.capacity and not (
                    returned and return_vc == vc
                ):
                    raise ProtocolViolation(
                        f"vc_credit interface '{port.name}' overflow on VC {vc}"
                    )
                cycle_result[port.name] = {
                    "return": returned,
                    "return_vc": return_vc,
                    "transfer": sent,
                    "occupancy": tuple(occupancy),
                }
                if not reset_active:
                    occupancy[vc] += sent
                    occupancy[return_vc] -= returned
        _sample_module_verification(
            module,
            verification_monitor,
            values,
            cycle_result,
            cycle=cycle,
            reset_active=reset_active,
        )
        results.append(cycle_result)

    return results


def simulate_credit_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
    reset: Iterable[bool] | None = None,
) -> list[dict[str, object]]:
    """Simulate pulse-return credit endpoints and enforce their bounds."""

    credit_ports = {
        port.name: port
        for port in module.ports
        if port.protocol is InterfaceProtocol.CREDIT
    }
    if not module.is_sequential or not credit_ports:
        raise SimulationError(
            "simulate_credit_cycles requires a sequential credit interface module"
        )
    if any(
        port.protocol is InterfaceProtocol.READY_VALID for port in module.ports
    ):
        raise SimulationError(
            "mixing ready/valid and credit interfaces is not implemented"
        )

    cycles = list(input_cycles)
    resets = _effective_reset_cycles(module, reset, len(cycles))
    expected = {
        port.name: port
        for port in module.ports
        if port.protocol is InterfaceProtocol.CREDIT
        or port.direction is PortDirection.INPUT
    }
    sender_credits = {
        port.name: _credit_capacity(port)
        for port in credit_ports.values()
        if port.direction is PortDirection.OUTPUT
    }
    receiver_occupancy = {
        port.name: 0
        for port in credit_ports.values()
        if port.direction is PortDirection.INPUT
    }
    functions = _function_table(module)
    results: list[dict[str, object]] = []
    verification_monitor = _module_verification_monitor(module)

    for cycle, (inputs, reset_active) in enumerate(
        zip(cycles, resets, strict=True)
    ):
        missing = expected.keys() - inputs.keys()
        extra = inputs.keys() - expected.keys()
        if missing:
            raise SimulationError(f"missing input '{sorted(missing)[0]}'")
        if extra:
            raise SimulationError(f"unknown input '{sorted(extra)[0]}'")
        if reset_active:
            sender_credits = {
                port.name: _credit_capacity(port)
                for port in credit_ports.values()
                if port.direction is PortDirection.OUTPUT
            }
            receiver_occupancy = {
                port.name: 0
                for port in credit_ports.values()
                if port.direction is PortDirection.INPUT
            }

        values: dict[str, object] = {}
        for name, port in expected.items():
            value = inputs[name]
            if port.protocol is InterfaceProtocol.WIRE:
                if not _fits(value, port.type):
                    raise SimulationError(f"input '{name}' does not fit {port.type}")
                values[name] = value
                continue
            if not isinstance(value, dict):
                raise SimulationError(f"credit input '{name}' must be a field mapping")
            required_fields = (
                {"payload", "send"}
                if port.direction is PortDirection.INPUT
                else {"return"}
            )
            if set(value) != required_fields:
                rendered = ", ".join(sorted(required_fields))
                raise SimulationError(
                    f"credit input '{name}' requires fields: {rendered}"
                )
            if port.direction is PortDirection.INPUT:
                if not _fits(value["payload"], port.type):
                    raise SimulationError(
                        f"input '{name}.payload' does not fit {port.type}"
                    )
                if not _fits(value["send"], BitType()):
                    raise SimulationError(f"input '{name}.send' does not fit bit")
                values[f"{name}.payload"] = value["payload"]
                values[f"{name}.send"] = value["send"]
                values[f"{name}.transfer"] = (
                    0 if reset_active else value["send"]
                )
            else:
                if not _fits(value["return"], BitType()):
                    raise SimulationError(f"input '{name}.return' does not fit bit")
                values[f"{name}.return"] = value["return"]
                values[f"{name}.credits"] = sender_credits[name]

        pending = list(module.assignments)
        while pending:
            deferred = []
            for assignment in pending:
                try:
                    value = _evaluate(assignment.expression, values, functions)
                except KeyError:
                    deferred.append(assignment)
                    continue
                name = assignment.target.name
                signal = assignment.signal
                if signal is CreditSignal.SEND:
                    values[f"{name}.send_request"] = value
                    physical_send = int(
                        not reset_active
                        and bool(value)
                        and sender_credits[name] > 0
                    )
                    values[f"{name}.send"] = physical_send
                    values[f"{name}.transfer"] = physical_send
                elif signal is CreditSignal.RETURN:
                    values[f"{name}.return_request"] = value
                    values[f"{name}.return"] = 0 if reset_active else value
                else:
                    key = name if signal is None else f"{name}.{signal.value}"
                    values[key] = value
            if len(deferred) == len(pending):
                targets = ", ".join(
                    _simulation_assignment_name(assignment)
                    for assignment in deferred
                )
                raise SimulationError(
                    f"unresolved combinational credit dependency: {targets}"
                )
            pending = deferred

        cycle_result: dict[str, object] = {}
        for port in module.ports:
            if port.protocol is InterfaceProtocol.WIRE:
                if port.direction is PortDirection.OUTPUT:
                    cycle_result[port.name] = values[port.name]
                continue
            if port.direction is PortDirection.OUTPUT:
                credits = sender_credits[port.name]
                sent = int(values[f"{port.name}.send"])
                returned = int(values[f"{port.name}.return"])
                capacity = _credit_capacity(port)
                if (
                    not reset_active
                    and returned
                    and not sent
                    and credits == capacity
                ):
                    raise ProtocolViolation(
                        f"credit interface '{port.name}' overflow: return at "
                        "maximum credits"
                    )
                cycle_result[port.name] = {
                    "payload": values[f"{port.name}.payload"],
                    "send": sent,
                    "transfer": sent,
                    "credits": credits,
                }
                if not reset_active:
                    sender_credits[port.name] = credits - sent + returned
            else:
                sent = int(values[f"{port.name}.send"])
                returned = int(values[f"{port.name}.return"])
                occupancy = receiver_occupancy[port.name]
                capacity = _credit_capacity(port)
                if not reset_active and returned and not sent and occupancy == 0:
                    raise ProtocolViolation(
                        f"credit interface '{port.name}' underflow: return with "
                        "no outstanding transfer"
                    )
                if (
                    not reset_active
                    and sent
                    and not returned
                    and occupancy == capacity
                ):
                    raise ProtocolViolation(
                        f"credit interface '{port.name}' overflow: transfer at "
                        "maximum occupancy"
                    )
                cycle_result[port.name] = {
                    "return": returned,
                    "transfer": int(values[f"{port.name}.transfer"]),
                }
                if not reset_active:
                    receiver_occupancy[port.name] = occupancy + sent - returned
        _sample_module_verification(
            module,
            verification_monitor,
            values,
            cycle_result,
            cycle=cycle,
            reset_active=reset_active,
        )
        results.append(cycle_result)

    return results


def simulate_protocol_cycles(
    module: Module,
    input_cycles: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    """Simulate a combinational protocol module and enforce stall stability."""

    if module.is_sequential or not module.has_protocol_interfaces:
        raise SimulationError(
            "simulate_protocol_cycles requires a combinational protocol module"
        )
    cycles = list(input_cycles)
    results: list[dict[str, object]] = []
    previous_inputs: dict[str, object] | None = None
    previous_outputs: dict[str, object] | None = None

    for inputs in cycles:
        outputs = simulate(module, **inputs)
        if previous_inputs is not None and previous_outputs is not None:
            _check_ready_valid_stability(
                module,
                previous_inputs,
                previous_outputs,
                inputs,
                outputs,
            )
        results.append(outputs)
        previous_inputs = inputs
        previous_outputs = outputs
    return results


def _simulate_protocol_module(
    module: Module, input_values: dict[str, object]
) -> dict[str, object]:
    incoming_ports = {
        port.name: port
        for port in module.ports
        if port.protocol is InterfaceProtocol.READY_VALID
        or port.direction is PortDirection.INPUT
    }
    missing = incoming_ports.keys() - input_values.keys()
    extra = input_values.keys() - incoming_ports.keys()
    if missing:
        raise SimulationError(f"missing input '{sorted(missing)[0]}'")
    if extra:
        raise SimulationError(f"unknown input '{sorted(extra)[0]}'")

    values: dict[str, object] = {}
    for name, port in incoming_ports.items():
        value = input_values[name]
        if port.protocol is InterfaceProtocol.WIRE:
            if not _fits(value, port.type):
                raise SimulationError(f"input '{name}' does not fit {port.type}")
            values[name] = value
            continue
        if not isinstance(value, dict):
            raise SimulationError(
                f"ready/valid input '{name}' must be a field mapping"
            )
        required_fields = (
            {"payload", "valid"}
            if port.direction is PortDirection.INPUT
            else {"ready"}
        )
        if set(value) != required_fields:
            rendered = ", ".join(sorted(required_fields))
            raise SimulationError(
                f"ready/valid input '{name}' requires fields: {rendered}"
            )
        if port.direction is PortDirection.INPUT:
            if not _fits(value["payload"], port.type):
                raise SimulationError(
                    f"input '{name}.payload' does not fit {port.type}"
                )
            if not _fits(value["valid"], BitType()):
                raise SimulationError(f"input '{name}.valid' does not fit bit")
            values[f"{name}.payload"] = value["payload"]
            values[f"{name}.valid"] = value["valid"]
        else:
            if not _fits(value["ready"], BitType()):
                raise SimulationError(f"input '{name}.ready' does not fit bit")
            values[f"{name}.ready"] = value["ready"]

    functions = _function_table(module)
    pending = list(module.assignments)
    while pending:
        deferred = []
        for assignment in pending:
            try:
                value = _evaluate(assignment.expression, values, functions)
            except KeyError:
                deferred.append(assignment)
                continue
            key = (
                assignment.target.name
                if assignment.signal is None
                else f"{assignment.target.name}.{assignment.signal.value}"
            )
            values[key] = value
        if len(deferred) == len(pending):
            targets = ", ".join(
                assignment.target.name for assignment in deferred
            )
            raise SimulationError(
                f"unresolved combinational protocol dependency: {targets}"
            )
        pending = deferred

    results: dict[str, object] = {}
    for port in module.ports:
        if port.protocol is InterfaceProtocol.WIRE:
            if port.direction is PortDirection.OUTPUT:
                results[port.name] = values[port.name]
            continue
        transfer = int(
            bool(values[f"{port.name}.valid"])
            and bool(values[f"{port.name}.ready"])
        )
        if port.direction is PortDirection.INPUT:
            results[port.name] = {
                "ready": values[f"{port.name}.ready"],
                "transfer": transfer,
            }
        else:
            results[port.name] = {
                "payload": values[f"{port.name}.payload"],
                "valid": values[f"{port.name}.valid"],
                "transfer": transfer,
            }
    return results


def _check_ready_valid_stability(
    module: Module,
    previous_inputs: dict[str, object],
    previous_outputs: dict[str, object],
    current_inputs: dict[str, object],
    current_outputs: dict[str, object],
) -> None:
    for port in module.ports:
        if port.protocol is not InterfaceProtocol.READY_VALID:
            continue
        if port.direction is PortDirection.INPUT:
            previous_forward = previous_inputs[port.name]
            current_forward = current_inputs[port.name]
            previous_backward = previous_outputs[port.name]
        else:
            previous_forward = previous_outputs[port.name]
            current_forward = current_outputs[port.name]
            previous_backward = previous_inputs[port.name]
        if not isinstance(previous_forward, dict) or not isinstance(
            current_forward, dict
        ) or not isinstance(previous_backward, dict):
            raise SimulationError("invalid ready/valid simulation value")
        stalled = bool(previous_forward["valid"]) and not bool(
            previous_backward["ready"]
        )
        if stalled and (
            not bool(current_forward["valid"])
            or current_forward["payload"] != previous_forward["payload"]
        ):
            raise ProtocolViolation(
                f"ready/valid interface '{port.name}' changed payload or valid "
                "while stalled"
            )


def _credit_capacity(port: Port) -> int:
    if port.capacity is None:
        raise SimulationError(f"credit interface '{port.name}' has no capacity")
    return port.capacity


def _simulation_assignment_name(assignment: object) -> str:
    target = assignment.target
    signal = assignment.signal
    channel = assignment.channel
    if channel is not None and signal is not None:
        return f"{target.name}.{channel.value}.{signal.value}"
    return target.name if signal is None else f"{target.name}.{signal.value}"


def _request_response_id(
    interface: RequestResponseInterface,
    payload: object,
) -> object:
    if interface.match_by is None or not isinstance(payload, dict):
        raise SimulationError(
            f"request/response interface '{interface.name}' has no usable match field"
        )
    return payload[interface.match_by]


def _evaluate(
    expression: expr.Expression,
    values: dict[str, object],
    functions: dict[str, Function],
) -> object:
    if isinstance(expression, (expr.InputRef, expr.ParameterRef, expr.RegisterRef)):
        return values[expression.name]
    if isinstance(expression, expr.InstanceOutputRef):
        return values[f"{expression.instance}.{expression.port}"]
    if isinstance(expression, expr.ReadyValidRef):
        if expression.signal is ReadyValidSignal.TRANSFER:
            return int(
                bool(values[f"{expression.interface}.valid"])
                and bool(values[f"{expression.interface}.ready"])
            )
        return values[f"{expression.interface}.{expression.signal.value}"]
    if isinstance(expression, expr.CreditRef):
        return values[f"{expression.interface}.{expression.signal.value}"]
    if isinstance(expression, expr.PacketRef):
        if expression.signal is PacketSignal.TRANSFER:
            return int(
                bool(values[f"{expression.interface}.valid"])
                and bool(values[f"{expression.interface}.ready"])
            )
        return values[f"{expression.interface}.{expression.signal.value}"]
    if isinstance(expression, expr.VirtualChannelCreditRef):
        return values[f"{expression.interface}.{expression.signal.value}"]
    if isinstance(expression, expr.RequestResponseRef):
        return values[
            f"{expression.interface}.{expression.channel.value}."
            f"{expression.signal.value}"
        ]
    if isinstance(expression, expr.FifoRef):
        return values[f"{expression.fifo}.{expression.signal.value}"]
    if isinstance(expression, expr.MemoryRef):
        return values[f"{expression.memory}.{expression.signal.value}"]
    if isinstance(expression, expr.RomRef):
        return values[f"{expression.rom}.{expression.signal.value}"]
    if isinstance(expression, (expr.Delay, expr.Pipeline)):
        return values[f"$delay_{expression.instance}"]
    if isinstance(expression, expr.Constant):
        return expression.value
    if isinstance(expression, expr.EnumEncode):
        return _evaluate(expression.expression, values, functions)
    if isinstance(expression, expr.EnumValid):
        value = _evaluate(expression.expression, values, functions)
        return int(expression.enum_type.is_valid_code(value))
    if isinstance(expression, expr.EnumDecode):
        value = _evaluate(expression.expression, values, functions)
        if expression.type.is_valid_code(value):
            return value
        return _evaluate(expression.fallback, values, functions)
    if isinstance(expression, expr.UnionConstruct):
        return TaggedUnionValue(
            expression.type,
            expression.variant,
            tuple(
                (name, _evaluate(value, values, functions))
                for name, value in expression.fields
            ),
        )
    if isinstance(expression, expr.UnionTag):
        value = _evaluate(expression.expression, values, functions)
        if not isinstance(value, TaggedUnionValue):
            raise SimulationError("tagged-union tag projection received a non-union value")
        return value.type.tag(value.variant)
    if isinstance(expression, expr.UnionField):
        value = _evaluate(expression.expression, values, functions)
        if not isinstance(value, TaggedUnionValue):
            raise SimulationError("tagged-union field projection received a non-union value")
        if value.variant != expression.variant:
            raise SimulationError(
                f"inactive tagged-union field projection {value.type.name}."
                f"{expression.variant}.{expression.field}"
            )
        return value.field(expression.field)
    if isinstance(expression, expr.Add):
        return _evaluate(expression.left, values, functions) + _evaluate(
            expression.right, values, functions
        )
    if isinstance(expression, expr.Binary):
        left = _evaluate(expression.left, values, functions)
        right = _evaluate(expression.right, values, functions)
        if expression.operator is expr.BinaryOperator.SUBTRACT:
            value = left - right
        elif expression.operator is expr.BinaryOperator.MULTIPLY:
            value = left * right
        elif expression.operator is expr.BinaryOperator.BIT_AND:
            value = left & right
        elif expression.operator is expr.BinaryOperator.BIT_OR:
            value = left | right
        elif expression.operator is expr.BinaryOperator.BIT_XOR:
            value = left ^ right
        elif expression.operator is expr.BinaryOperator.SHIFT_LEFT:
            value = left << right
        elif expression.operator is expr.BinaryOperator.SHIFT_RIGHT:
            value = left >> right
        elif expression.operator is expr.BinaryOperator.EQUAL:
            return int(left == right)
        elif expression.operator is expr.BinaryOperator.NOT_EQUAL:
            return int(left != right)
        elif expression.operator is expr.BinaryOperator.LESS:
            return int(left < right)
        elif expression.operator is expr.BinaryOperator.LESS_EQUAL:
            return int(left <= right)
        elif expression.operator is expr.BinaryOperator.GREATER:
            return int(left > right)
        elif expression.operator is expr.BinaryOperator.GREATER_EQUAL:
            return int(left >= right)
        else:
            raise SimulationError(f"unsupported operator {expression.operator.value}")
        return _normalize(value, expression.type)
    if isinstance(expression, expr.Extend):
        return _evaluate(expression.expression, values, functions)
    if isinstance(expression, expr.Truncate):
        value = _evaluate(expression.expression, values, functions)
        return _normalize(value, expression.type)
    if isinstance(expression, expr.FixedConvert):
        value = _evaluate(expression.expression, values, functions)
        if expression.kind in {expr.FixedConversionKind.FROM_RAW, expr.FixedConversionKind.TO_RAW}:
            return _normalize(value, expression.type)
        if expression.rational_denominator is not None:
            return quantize_rational(
                value,
                expression.rational_denominator,
                fraction=expression.type.fraction,
                width=expression.type.width,
                signed=isinstance(expression.type, FixedType),
                rounding=expression.rounding,
                overflow=expression.overflow,
            )
        source_fraction = getattr(expression.expression.type, "fraction", 0)
        target_fraction = expression.type.fraction
        delta = target_fraction - source_fraction
        if delta >= 0:
            converted = value << delta
        else:
            converted = round_ratio(
                value, 1 << -delta, expression.rounding
            )
        return apply_overflow(
            converted,
            width=expression.type.width,
            signed=isinstance(expression.type, FixedType),
            policy=expression.overflow,
        )
    if isinstance(expression, expr.Mux):
        branch = (
            expression.when_true
            if _evaluate(expression.condition, values, functions)
            else expression.when_false
        )
        return _evaluate(branch, values, functions)
    if isinstance(expression, expr.Switch):
        selector = _evaluate(expression.selector, values, functions)
        for case in expression.cases:
            if case.key == selector:
                return _evaluate(case.expression, values, functions)
        return _evaluate(expression.default, values, functions)
    if isinstance(expression, expr.Call):
        function = functions[expression.function]
        arguments = [
            _evaluate(argument, values, functions) for argument in expression.arguments
        ]
        parameters = {
            name: value
            for name, value in values.items()
            if name.startswith(_FUNCTIONAL_PREFIX)
        }
        parameters.update({
            parameter.name: argument
            for parameter, argument in zip(
                function.parameters, arguments, strict=True
            )
        })
        return _evaluate(function.body, parameters, functions)
    if isinstance(expression, (expr.Generate, expr.Map)):
        return [
            _evaluate(element, values, functions)
            for element in expression.elements
        ]
    if isinstance(expression, expr.FunctionalRegion):
        return list(_iterate_functional_region(expression, values, functions))
    if isinstance(expression, expr.Dot):
        return [
            _evaluate(product, values, functions)
            for product in expression.products
        ]
    if isinstance(expression, expr.Reduce):
        if expression.plan is not None:
            collection = (
                _iterate_functional_region(expression.collection, values, functions)
                if isinstance(expression.collection, expr.FunctionalRegion)
                else iter(_evaluate(expression.collection, values, functions))
            )
            return _evaluate_exact_reduction(
                expression.plan,
                collection,
                values,
                functions,
            )
        return _evaluate(lower_reduction(expression), values, functions)
    if isinstance(expression, expr.ImplementationChoice):
        return _evaluate(
            expression.selected_alternative.expression,
            values,
            functions,
        )
    if isinstance(expression, expr.FieldAccess):
        aggregate = _evaluate(expression.expression, values, functions)
        return aggregate[expression.field]
    if isinstance(expression, expr.StructConstruct):
        return {
            name: _evaluate(value, values, functions)
            for name, value in expression.fields
        }
    if isinstance(expression, expr.TupleConstruct):
        return tuple(
            _evaluate(value, values, functions) for value in expression.elements
        )
    if isinstance(expression, expr.TupleProject):
        value = _evaluate(expression.expression, values, functions)
        if not isinstance(value, tuple):
            raise SimulationError("tuple projection received a non-tuple value")
        return value[expression.index]
    if isinstance(expression, expr.FunctionalCaptureRef):
        key = f"{_FUNCTIONAL_CAPTURE_PREFIX}{expression.identity}"
        if key not in values:
            raise SimulationError(
                f"functional capture '{expression.display_name}' is not bound"
            )
        return values[key]
    if isinstance(expression, expr.FunctionalTableLookup):
        key = f"{_FUNCTIONAL_TABLE_PREFIX}{expression.table_name}"
        table = values.get(key)
        if not isinstance(table, FunctionalTable):
            raise SimulationError(
                f"functional table '{expression.table_name}' is not bound"
            )
        try:
            index = evaluate_compile_time(
                expression.index,
                _functional_binder_values(values),
            )
        except ValueError as error:
            raise SimulationError(str(error)) from error
        offset = index - table.start
        if not 0 <= offset < len(table.values):
            raise SimulationError(
                f"functional table index {index} is outside "
                f"{table.start}..{table.stop}"
            )
        return _evaluate(table.values[offset], values, functions)
    if isinstance(expression, expr.VectorIndex):
        vector = _evaluate(expression.expression, values, functions)
        try:
            index = (
                expression.index
                if isinstance(expression.index, int)
                else evaluate_compile_time(
                    expression.index,
                    _functional_binder_values(values),
                )
            )
        except ValueError as error:
            raise SimulationError(str(error)) from error
        return vector[index]
    if isinstance(expression, expr.RuntimeIndex):
        vector = _evaluate(expression.expression, values, functions)
        index = _evaluate(expression.index, values, functions)
        if not isinstance(index, int) or not 0 <= index < expression.vector_length:
            raise SimulationError(
                f"runtime vector index {index!r} escaped proven range "
                f"0..{expression.vector_length - 1}"
            )
        return vector[index]
    if isinstance(expression, expr.VectorUpdate):
        vector = list(_evaluate(expression.expression, values, functions))
        index = _evaluate(expression.index, values, functions)
        if not isinstance(index, int) or not 0 <= index < expression.vector_length:
            raise SimulationError(
                f"vector update index {index!r} escaped proven range "
                f"0..{expression.vector_length - 1}"
            )
        vector[index] = _evaluate(expression.value, values, functions)
        return vector
    if isinstance(expression, expr.Slice):
        value = _evaluate(expression.expression, values, functions)
        try:
            raw = ir_packing.pack_runtime(expression.expression.type, value)
            return ir_packing.slice_runtime(
                raw,
                expression.expression.type.width,
                expression.msb,
                expression.lsb,
            )
        except ir_packing.PackingError as error:
            raise SimulationError(f"cannot evaluate bit slice: {error}") from error
    if isinstance(expression, expr.Concat):
        try:
            return ir_packing.concat_runtime(
                (
                    ir_packing.pack_runtime(operand.type, value),
                    ir_packing.packed_width(operand.type),
                )
                for operand in expression.operands
                for value in (_evaluate(operand, values, functions),)
            )
        except ir_packing.PackingError as error:
            raise SimulationError(f"cannot evaluate concat: {error}") from error
    if isinstance(expression, expr.VectorConcat):
        result: list[object] = []
        for operand in expression.operands:
            value = _evaluate(operand, values, functions)
            if not isinstance(value, (list, tuple)):
                raise SimulationError(
                    "cannot evaluate vector concat: operand is not a vector value"
                )
            result.extend(value)
        return result
    if isinstance(expression, expr.Reshape):
        value = _evaluate(expression.expression, values, functions)
        source_type = expression.expression.type
        target_type = expression.type
        if not isinstance(source_type, VecType) or not isinstance(target_type, VecType):
            raise SimulationError("reshape requires vector source and target types")
        leaves = _flatten_vector_runtime(source_type, value)
        rebuilt, consumed = _rebuild_vector_runtime(target_type, leaves, 0)
        if consumed != len(leaves):
            raise SimulationError("reshape did not consume every source vector leaf")
        return rebuilt
    if isinstance(expression, expr.Bitcast):
        value = _evaluate(expression.expression, values, functions)
        try:
            raw = ir_packing.pack_runtime(expression.expression.type, value)
            return ir_packing.unpack_runtime(expression.type, raw)
        except ir_packing.PackingError as error:
            raise SimulationError(f"cannot evaluate bitcast: {error}") from error
    if isinstance(expression, expr.Pack):
        value = _evaluate(expression.expression, values, functions)
        try:
            return ir_packing.pack_runtime(expression.expression.type, value)
        except ir_packing.PackingError as error:
            raise SimulationError(f"cannot evaluate pack: {error}") from error
    if isinstance(expression, expr.Unpack):
        value = _evaluate(expression.expression, values, functions)
        try:
            return ir_packing.unpack_runtime(expression.type, value)
        except ir_packing.PackingError as error:
            raise SimulationError(f"cannot evaluate unpack: {error}") from error
    raise SimulationError(f"cannot evaluate expression {expression!r}")


def _functional_binder_values(values: dict[str, object]) -> dict[str, int]:
    result: dict[str, int] = {}
    for name, value in values.items():
        if not name.startswith(_FUNCTIONAL_BINDER_PREFIX):
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise SimulationError("functional binder value is not an integer")
        result[name[len(_FUNCTIONAL_BINDER_PREFIX) :]] = value
    return result


def _iterate_functional_region(
    region: expr.FunctionalRegion,
    values: dict[str, object],
    functions: dict[str, Function],
) -> Iterable[object]:
    """Evaluate one template per binder value without materializing IR clones."""

    base = dict(values)
    for reference, capture in region.captures:
        base[f"{_FUNCTIONAL_CAPTURE_PREFIX}{reference.identity}"] = _evaluate(
            capture, values, functions
        )
    for table in region.tables:
        base[f"{_FUNCTIONAL_TABLE_PREFIX}{table.name}"] = table
    for index in range(region.binder.start, region.binder.stop):
        iteration = dict(base)
        iteration[f"{_FUNCTIONAL_BINDER_PREFIX}{region.binder.identity}"] = index
        yield _evaluate(region.template, iteration, functions)


def _evaluate_exact_reduction(
    plan: ExactReductionPlan,
    collection: Iterable[object],
    values: dict[str, object],
    functions: dict[str, Function],
) -> object:
    current = list(collection)
    if len(current) != plan.length:
        raise SimulationError(
            f"exact reduction expected {plan.length} values, got {len(current)}"
        )
    if any(not _fits(value, plan.leaf_type) for value in current):
        raise SimulationError("exact reduction leaf value does not fit its type")
    for level in plan.levels:
        if len(current) != len(level.input_types):
            raise SimulationError("exact reduction level input count does not match")
        by_left = {operation.left_index: operation for operation in level.operations}
        next_values: list[object] = []
        index = 0
        while index < len(current):
            operation = by_left.get(index)
            if operation is None:
                next_values.append(current[index])
                index += 1
                continue
            next_values.append(
                _evaluate_reduction_operation(
                    operation,
                    current[index],
                    current[index + 1],
                    values,
                    functions,
                )
            )
            index += 2
        current = next_values
    if len(current) != 1 or not _fits(current[0], plan.root_type):
        raise SimulationError("exact reduction did not produce its declared root type")
    return current[0]


def _evaluate_reduction_operation(
    operation: ExactReductionOperation,
    left: object,
    right: object,
    values: dict[str, object],
    functions: dict[str, Function],
) -> object:
    if not _fits(left, operation.left_type) or not _fits(right, operation.right_type):
        raise SimulationError("exact reduction operand does not fit its declared type")
    if operation.function is None:
        if not isinstance(left, int) or not isinstance(right, int):
            raise SimulationError("built-in exact addition requires scalar integers")
        return _normalize(left + right, operation.result_type)
    function = functions.get(operation.function)
    if function is None:
        raise SimulationError(
            f"exact reduction callable '{operation.function}' is not defined"
        )
    if function.callee_identity != operation.callee_identity:
        raise SimulationError(
            f"exact reduction callable '{operation.function}' identity does not match"
        )
    if tuple(parameter.type for parameter in function.parameters) != (
        operation.left_type,
        operation.right_type,
    ) or function.return_type != operation.result_type:
        raise SimulationError(
            f"exact reduction callable '{operation.function}' signature does not match"
        )
    parameters = {
        name: value
        for name, value in values.items()
        if name.startswith(_FUNCTIONAL_PREFIX)
    }
    parameters.update(
        {
            function.parameters[0].name: left,
            function.parameters[1].name: right,
        }
    )
    result = _evaluate(function.body, parameters, functions)
    if not _fits(result, operation.result_type):
        raise SimulationError(
            f"exact reduction callable '{operation.function}' returned an invalid value"
        )
    return result


def _flatten_vector_runtime(type_: VecType, value: object) -> list[object]:
    """Flatten nested vectors in outer-to-inner logical sequence order."""

    try:
        return flatten_vector_value(type_, value)
    except RuntimeValueError as error:
        raise SimulationError(str(error)) from error


def _rebuild_vector_runtime(
    type_: VecType,
    leaves: list[object],
    offset: int,
) -> tuple[list[object], int]:
    """Rebuild one nested vector shape from an outer-to-inner leaf stream."""

    try:
        return rebuild_vector_value(type_, leaves, offset)
    except RuntimeValueError as error:
        raise SimulationError(str(error)) from error


def _fits(value: object, type_: HardwareType) -> bool:
    return runtime_value_fits(value, type_)


def _normalize(value: int, type_: HardwareType) -> int:
    try:
        return normalize_scalar(value, type_)
    except RuntimeValueError as error:
        raise SimulationError(str(error)) from error


def _async_memory_read_value(
    memory: object,
    cells: list[object],
    values: Mapping[str, object],
    *,
    reset_active: bool,
) -> object:
    """Evaluate one latency-zero global-memory read from the pre-edge view."""

    name = str(getattr(memory, "name"))
    element_type = getattr(memory, "element_type")
    if reset_active and getattr(memory, "read_data_reset").value == "clear":
        return _zero_runtime(element_type)

    read_address = int(values[f"${name}.read_address"])
    if not 0 <= read_address < len(cells):
        raise SimulationError(
            f"memory '{name}' read address {read_address} is outside "
            f"0..{len(cells) - 1}"
        )
    old_value = cells[read_address]
    # Reset suppresses writes regardless of whether the combinational result
    # itself is preserved or masked.
    if reset_active or getattr(memory, "collision").value != "write_first":
        return old_value

    write_enable = bool(values[f"${name}.write_enable"])
    write_address = int(values[f"${name}.write_address"])
    if not write_enable or read_address != write_address:
        return old_value
    new_value = values[f"${name}.write_data"]
    write_mask = getattr(memory, "write_mask")
    if write_mask is None:
        return new_value
    return _merge_memory_bytes(
        old_value,
        new_value,
        int(values[f"${name}.write_mask"]),
        element_type,
    )


def _merge_memory_bytes(
    old_value: object,
    new_value: object,
    byte_mask: int,
    type_: HardwareType,
) -> object:
    """Merge byte lanes using raw representation bits, lane zero at the LSB."""

    width = type_.width
    raw_limit = (1 << width) - 1
    expanded = 0
    for lane in range((width + 7) // 8):
        if byte_mask & (1 << lane):
            expanded |= 0xFF << (lane * 8)
    if ir_packing.is_bit_packable(type_):
        try:
            old_raw = ir_packing.pack_runtime(type_, old_value)
            new_raw = ir_packing.pack_runtime(type_, new_value)
            merged = (
                (old_raw & raw_limit & ~expanded)
                | (new_raw & raw_limit & expanded)
            )
            return ir_packing.unpack_runtime(type_, merged)
        except ir_packing.PackingError as error:
            raise SimulationError(
                f"cannot merge byte-masked memory value of type {type_}: {error}"
            ) from error

    # Preserve the historical scalar path for nominal enum memories, which
    # are deliberately outside the general raw packing boundary.
    merged = (
        (int(old_value) & raw_limit & ~expanded)
        | (int(new_value) & raw_limit & expanded)
    )
    return _normalize(merged, type_)


def _zero_runtime(type_: HardwareType) -> object:
    try:
        return zero_runtime_value(type_)
    except RuntimeValueError as error:
        raise SimulationError(f"no reset value for delayed {type_}") from error


def _collect_delays(
    expression: expr.Expression,
    found: dict[int, expr.Delay | expr.Pipeline],
) -> None:
    for node in walk_expression(
        expression,
        policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
    ):
        if isinstance(node, (expr.Delay, expr.Pipeline)):
            found.setdefault(node.instance, node)


def _module_delay_nodes(
    module: Module,
) -> dict[int, expr.Delay | expr.Pipeline]:
    """Collect every sequential expression owned by one module exactly once.

    A delay in a rule guard or nested-effect activation is just as physical as
    one used by an output or next-state expression.  Keep collection rooted in
    the typed module instead of relying on whichever expressions an individual
    simulator path happened to evaluate first.
    """

    found: dict[int, expr.Delay | expr.Pipeline] = {}
    for local in module.locals:
        _collect_delays(local.expression, found)
    for assignment in module.assignments:
        _collect_delays(assignment.expression, found)
    for assignment in module.next_assignments:
        _collect_delays(assignment.expression, found)
        if assignment.activation is not None:
            _collect_delays(assignment.activation, found)
    for rule in module.rules:
        _collect_delays(rule.guard, found)
        for action in rule.actions:
            _collect_delays(action.expression, found)
            if action.activation is not None:
                _collect_delays(action.activation, found)
    if module.resolved_transition is not None:
        for group in module.resolved_transition.action_groups:
            _collect_delays(group.guard, found)
            for action in group.actions:
                for operand in action.operands:
                    _collect_delays(operand, found)
                if action.activation is not None:
                    _collect_delays(action.activation, found)
    return found
