"""Compiler-side erasure of bounded elastic pipelines.

The primitive simulation machine has no elastic-pipeline operation.  This
module turns the already selected global-clock-enable implementation into
ordinary registers, combinational expressions, and explicit next-state muxes
before protocol fields and the semantic expression DAG are lowered further.
"""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir.interfaces import InterfaceProtocol, ReadyValidSignal
from zlang.ir.module import Assignment, Module, NextAssignment, PortDirection, Register
from zlang.ir.temporal_admission import TemporalAdmissionPolicy
from zlang.ir.types import (
    BitType,
    EnumType,
    FixedType,
    HardwareType,
    SIntType,
    StructType,
    TaggedUnionType,
    TupleType,
    UFixedType,
    UIntType,
    VecType,
    BitsType,
)
from zlang.simulation_rewrite import SimulationExpressionRewriter
from zlang.simulation_primitives import bit_binary as _bit_binary
from zlang.simulation_primitives import bit_not as _bit_not
from zlang.shared_arithmetic import (
    SharedArithmeticError,
    match_shared_multiply_add,
)


class ElasticSimulationLoweringError(ValueError):
    """An elastic region cannot be erased to primitive state exactly."""


def _zero_expression(type_: HardwareType) -> expr.Expression:
    """Build an exact typed reset-zero without depending on packed ABI tricks."""

    if isinstance(
        type_,
        (BitType, UIntType, SIntType, BitsType, FixedType, UFixedType),
    ):
        return expr.Constant(0, type_)
    if isinstance(type_, EnumType):
        return expr.Constant(type_.codes[0], type_)
    if isinstance(type_, StructType):
        return expr.StructConstruct(
            type_.name,
            tuple(
                (field.name, _zero_expression(field.type))
                for field in type_.fields
            ),
            type_,
        )
    if isinstance(type_, TupleType):
        return expr.TupleConstruct(
            tuple(_zero_expression(item) for item in type_.elements),
            type_,
        )
    if isinstance(type_, VecType):
        return expr.Generate(
            "$zlang_elastic_zero",
            0,
            type_.length,
            tuple(_zero_expression(type_.element_type) for _ in range(type_.length)),
            type_,
        )
    if isinstance(type_, TaggedUnionType):
        variant = type_.variants[0]
        return expr.UnionConstruct(
            variant.name,
            tuple(
                (field.name, _zero_expression(field.type))
                for field in variant.fields
            ),
            type_,
        )
    raise ElasticSimulationLoweringError(
        f"elastic pipeline cannot construct reset zero for type '{type_}'"
    )


class _PipelineStateLowerer(SimulationExpressionRewriter):
    def __init__(self, *, prefix: str, domain: str, advance: expr.Expression) -> None:
        super().__init__()
        self._prefix = prefix
        self._domain = domain
        self._advance = advance
        self._instances: dict[int, tuple[expr.Pipeline, expr.RegisterRef]] = {}
        self.registers: list[Register] = []
        self.next_assignments: list[NextAssignment] = []

    @property
    def stage_layout(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            sorted((node.instance, node.stages) for node, _ in self._instances.values())
        )

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if isinstance(value, expr.Delay):
            raise ElasticSimulationLoweringError(
                "elastic selected expression contains fixed Delay state"
            )
        if isinstance(value, expr.Pipeline):
            return self._pipeline(value)
        return None

    def _pipeline(self, value: expr.Pipeline) -> expr.RegisterRef:
        existing = self._instances.get(value.instance)
        if existing is not None:
            previous, reference = existing
            if previous != value:
                raise ElasticSimulationLoweringError(
                    f"elastic pipeline instance {value.instance} has conflicting definitions"
                )
            return reference
        if value.stages < 1 or value.domain not in {None, self._domain}:
            raise ElasticSimulationLoweringError(
                f"elastic pipeline instance {value.instance} has invalid stage/domain metadata"
            )

        source = self.expression(value.expression)
        previous: expr.Expression = source
        final: expr.RegisterRef | None = None
        for stage in range(value.stages):
            name = f"{self._prefix}_data_{value.instance}_{stage}"
            register = Register(name, value.type, _zero_expression(value.type), self._domain)
            current = expr.RegisterRef(name, value.type, origin=value.origin)
            held = expr.Mux(
                self._advance,
                previous,
                current,
                value.type,
                origin=value.origin,
            )
            self.registers.append(register)
            self.next_assignments.append(NextAssignment(register, held))
            previous = current
            final = current
        assert final is not None
        self._instances[value.instance] = (value, final)
        return final


class _TemporalInputSnapshotRewriter(SimulationExpressionRewriter):
    """Replace only the source RV payload with its captured transaction."""

    def __init__(self, source: str, captured: expr.RegisterRef) -> None:
        super().__init__()
        self._source = source
        self._captured = captured

    def rewrite_special(self, value: expr.Expression) -> expr.Expression | None:
        if (
            isinstance(value, expr.ReadyValidRef)
            and value.interface == self._source
            and value.signal is ReadyValidSignal.PAYLOAD
        ):
            return self._captured
        return None


def _state_is(
    state: expr.RegisterRef,
    value: int,
    state_type: UIntType,
) -> expr.Binary:
    return expr.Binary(
        expr.BinaryOperator.EQUAL,
        state,
        expr.Constant(value, state_type),
        state_type,
        BitType(),
    )


def _lower_noninterleaved_temporal_region(
    module: Module,
    region: object,
    source: object,
    destination: object,
) -> Module:
    """Erase the deliberately bounded capacity-one temporal multiply schedule."""

    graph = getattr(region, "temporal_graph")
    assert graph is not None
    try:
        shape = match_shared_multiply_add(region.source_expression)
    except SharedArithmeticError as error:
        raise ElasticSimulationLoweringError(str(error)) from error
    if tuple(item.operation_id for item in graph.operations) != ("mul0", "mul1", "add0"):
        raise ElasticSimulationLoweringError("unsupported temporal shared-arithmetic schedule")
    if graph.admission_policy is not TemporalAdmissionPolicy.RETIRE_AND_RELOAD:
        raise ElasticSimulationLoweringError("unsupported temporal admission policy")

    prefix = "$zlang_temporal_" + region.semantic_id.removeprefix("elastic:")[:16]
    state_type = UIntType(3)
    state_register = Register(
        f"{prefix}_state", state_type, expr.Constant(0, state_type), region.clock,
    )
    storage_registers = tuple(
        Register(
            f"{prefix}_{item.register_id}", item.type,
            _zero_expression(item.type), region.clock,
        )
        for item in graph.storage_plan.registers
    )
    register_by_id = {item.name.removeprefix(f"{prefix}_"): item for item in storage_registers}

    def stored(value_id: str) -> expr.RegisterRef:
        register = register_by_id[graph.storage_plan.register_for(value_id).register_id]
        return expr.RegisterRef(register.name, register.type)

    def storage_register(value_id: str) -> Register:
        return register_by_id[graph.storage_plan.register_for(value_id).register_id]

    state = expr.RegisterRef(state_register.name, state_type)
    captured = stored("input")
    product0 = stored("mul0")
    product1 = stored("mul1")
    result = stored("result")
    rewriter = _TemporalInputSnapshotRewriter(source.name, captured)
    first_product = rewriter.expression(shape.product0)
    second_product = rewriter.expression(shape.product1)
    scheduled_sum = expr.Add(product0, product1, destination.type)

    reset_active = expr.InputRef(f"$reset:{region.reset}", BitType())
    reset_deasserted = _bit_not(reset_active)
    idle = _state_is(state, 0, state_type)
    mul0 = _state_is(state, 1, state_type)
    mul1 = _state_is(state, 2, state_type)
    add = _state_is(state, 3, state_type)
    output_pending = _state_is(state, 4, state_type)
    output_ready = expr.ReadyValidRef(destination.name, ReadyValidSignal.READY, BitType())
    can_reload = _bit_binary(expr.BinaryOperator.BIT_AND, output_pending, output_ready)
    source_can_accept = _bit_binary(expr.BinaryOperator.BIT_OR, idle, can_reload)
    source_ready = _bit_binary(expr.BinaryOperator.BIT_AND, reset_deasserted, source_can_accept)
    source_valid = expr.ReadyValidRef(source.name, ReadyValidSignal.VALID, BitType())
    input_transfer = _bit_binary(expr.BinaryOperator.BIT_AND, source_ready, source_valid)
    output_valid = _bit_binary(
        expr.BinaryOperator.BIT_AND, reset_deasserted, output_pending,
    )

    # The capacity-one pending-output state may atomically retire the old
    # result and capture the next one.  No compute stages overlap.
    state_next: expr.Expression = state
    state_next = expr.Mux(output_pending, expr.Mux(
        output_ready,
        expr.Mux(input_transfer, expr.Constant(1, state_type), expr.Constant(0, state_type), state_type),
        state,
        state_type,
    ), state_next, state_type)
    state_next = expr.Mux(add, expr.Constant(4, state_type), state_next, state_type)
    state_next = expr.Mux(mul1, expr.Constant(3, state_type), state_next, state_type)
    state_next = expr.Mux(mul0, expr.Constant(2, state_type), state_next, state_type)
    state_next = expr.Mux(idle, expr.Mux(
        input_transfer, expr.Constant(1, state_type), state, state_type,
    ), state_next, state_type)

    def held(register: Register, enable: expr.Expression, value: expr.Expression) -> NextAssignment:
        current = expr.RegisterRef(register.name, register.type)
        return NextAssignment(register, expr.Mux(enable, value, current, register.type))

    generated_assignments = (
        Assignment(source, source_ready, ReadyValidSignal.READY),
        Assignment(destination, result, ReadyValidSignal.PAYLOAD),
        Assignment(destination, output_valid, ReadyValidSignal.VALID),
    )
    return replace(
        module,
        assignments=(*generated_assignments, *module.assignments),
        registers=(
            state_register, *storage_registers,
        ),
        next_assignments=(
            NextAssignment(state_register, state_next),
            held(storage_register("input"), input_transfer, expr.ReadyValidRef(
                source.name, ReadyValidSignal.PAYLOAD, source.type,
            )),
            held(storage_register("mul0"), mul0, first_product),
            held(storage_register("mul1"), mul1, second_product),
            held(storage_register("result"), add, scheduled_sum),
        ),
        elastic_pipeline_regions=(),
        semantic_expression_arena_statistics=None,
        semantic_expression_provenance=None,
    )


def lower_elastic_pipeline_module(module: Module) -> Module:
    """Erase one frozen elastic region to scalar state and ready/valid equations."""

    if not module.elastic_pipeline_regions:
        return module
    if len(module.elastic_pipeline_regions) != 1:
        raise ElasticSimulationLoweringError(
            "primitive simulation requires exactly one elastic pipeline region"
        )
    region = module.elastic_pipeline_regions[0]
    if module.registers or module.next_assignments or module.rules:
        raise ElasticSimulationLoweringError(
            "elastic compiler-owned state cannot be mixed with user state"
        )
    try:
        source = next(
            port for port in module.ports if port.name == region.source_endpoint
        )
        destination = next(
            port for port in module.ports if port.name == region.destination_endpoint
        )
    except StopIteration as error:
        raise ElasticSimulationLoweringError(
            "elastic endpoints do not resolve uniquely"
        ) from error
    if (
        source.direction is not PortDirection.INPUT
        or destination.direction is not PortDirection.OUTPUT
        or source.protocol is not InterfaceProtocol.READY_VALID
        or destination.protocol is not InterfaceProtocol.READY_VALID
        or source.domain != region.clock
        or destination.domain != region.clock
    ):
        raise ElasticSimulationLoweringError(
            "elastic endpoints do not match the frozen ready/valid clock domain"
        )

    if region.temporal_graph is not None:
        return _lower_noninterleaved_temporal_region(
            module, region, source, destination,
        )

    prefix = "$zlang_elastic_" + region.semantic_id.removeprefix("elastic:")[:16]
    valid_registers = tuple(
        Register(
            f"{prefix}_valid_{stage}",
            BitType(),
            expr.Constant(0, BitType()),
            region.clock,
        )
        for stage in range(region.timing.capacity)
    )
    output_valid_state = expr.RegisterRef(valid_registers[-1].name, BitType())
    output_ready = expr.ReadyValidRef(
        destination.name,
        ReadyValidSignal.READY,
        BitType(),
        origin=region.source_origin,
    )
    reset_active = expr.InputRef(f"$reset:{region.reset}", BitType())
    reset_deasserted = _bit_not(reset_active)
    available = _bit_binary(
        expr.BinaryOperator.BIT_OR,
        _bit_not(output_valid_state),
        output_ready,
    )
    advance = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        reset_deasserted,
        available,
    )

    data = _PipelineStateLowerer(
        prefix=prefix,
        domain=region.clock,
        advance=advance,
    )
    payload = data.expression(region.selected_candidate.expression)
    if data.stage_layout != region.plan.data_stage_instances:
        raise ElasticSimulationLoweringError(
            "elastic data state does not match the frozen physical plan"
        )

    source_valid = expr.ReadyValidRef(
        source.name,
        ReadyValidSignal.VALID,
        BitType(),
        origin=region.source_origin,
    )
    valid_next: list[NextAssignment] = []
    previous: expr.Expression = source_valid
    for register in valid_registers:
        current = expr.RegisterRef(register.name, BitType())
        valid_next.append(
            NextAssignment(
                register,
                expr.Mux(advance, previous, current, BitType()),
            )
        )
        previous = current

    output_valid = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        reset_deasserted,
        output_valid_state,
    )
    generated_assignments = (
        Assignment(source, advance, ReadyValidSignal.READY),
        Assignment(destination, payload, ReadyValidSignal.PAYLOAD),
        Assignment(destination, output_valid, ReadyValidSignal.VALID),
    )
    return replace(
        module,
        assignments=(*generated_assignments, *module.assignments),
        registers=(*data.registers, *valid_registers),
        next_assignments=(*data.next_assignments, *valid_next),
        elastic_pipeline_regions=(),
        semantic_expression_arena_statistics=None,
        semantic_expression_provenance=None,
    )


__all__ = [
    "ElasticSimulationLoweringError",
    "lower_elastic_pipeline_module",
]
