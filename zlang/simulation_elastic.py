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
