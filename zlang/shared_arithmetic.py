"""Bounded temporal arithmetic candidates for flow-controlled regions.

Only one shape is intentionally supported here: an exact two-product integer
sum.  It proves the candidate-provider / temporal-schedule boundary without
turning implementation selection into a general HLS scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from zlang.ir import expressions as expr
from zlang.ir.pipelines import (
    MultiplierMapping,
    PipelineCandidate,
    PipelineCostSource,
    PipelineEstimate,
    PipelinePlan,
    PipelineTree,
    RegisterPlacement,
)
from zlang.ir.temporal import (
    ResourceBinding,
    ScheduledOperation,
    TemporalClass,
    TemporalImplementationGraph,
    TemporalResourceCost,
)
from zlang.ir.temporal_admission import (
    TemporalAdmissionPolicy,
    derive_noninterleaved_admission,
)
from zlang.ir.temporal_storage import (
    RegisterBinding,
    ValueLifetime,
    build_temporal_storage_plan,
)
from zlang.ir.types import HardwareType, SIntType, UIntType
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.pipelines import pipeline_candidate_violations


class SharedArithmeticError(ValueError):
    """The deliberately small shared arithmetic subset does not match."""


@dataclass(frozen=True)
class SharedMultiplyAddShape:
    product0: expr.Binary
    product1: expr.Binary
    addition: expr.Add


@dataclass(frozen=True)
class SharedArithmeticCandidate:
    """One selected-expression projection and its authoritative temporal graph."""

    pipeline_candidate: PipelineCandidate
    temporal_graph: TemporalImplementationGraph


def match_shared_multiply_add(value: expr.Expression) -> SharedMultiplyAddShape:
    """Recognize exactly ``a*b + c*d`` without reassociation or coercion.

    This deliberately excludes fixed-point operations, implicit conversions,
    reductions and arbitrary DAGs.  The source expression remains the semantic
    value; this function merely decides whether the bounded temporal provider
    can own its execution schedule.
    """

    if not isinstance(value, expr.Add):
        raise SharedArithmeticError("shared arithmetic requires exactly a*b + c*d")
    if not all(
        isinstance(item, expr.Binary) and item.operator is expr.BinaryOperator.MULTIPLY
        for item in (value.left, value.right)
    ):
        raise SharedArithmeticError("shared arithmetic requires exactly two multiply terms")
    product0 = value.left
    product1 = value.right
    assert isinstance(product0, expr.Binary) and isinstance(product1, expr.Binary)
    if not isinstance(value.type, (UIntType, SIntType)):
        raise SharedArithmeticError("shared arithmetic supports only exact integer results")
    if product0.type != product1.type:
        raise SharedArithmeticError("shared arithmetic products must have identical types")
    if product0.type != value.left.type or product1.type != value.right.type:
        raise SharedArithmeticError("shared arithmetic product representation is inconsistent")
    if any(
        not isinstance(operand.type, type(product0.operand_type))
        for product in (product0, product1)
        for operand in (product.left, product.right)
    ):
        raise SharedArithmeticError("shared arithmetic operands must use one integer family")
    return SharedMultiplyAddShape(product0, product1, value)


def allocate_temporal_registers(
    lifetimes: tuple[ValueLifetime, ...],
) -> tuple[RegisterBinding, ...]:
    """Deterministic interval allocator, exact type/width compatibility only."""

    allocated: list[tuple[str, ValueLifetime]] = []
    result: list[RegisterBinding] = []
    for item in sorted(lifetimes, key=lambda value: (value.first_live_cycle, value.last_live_cycle, value.value_id)):
        register = next((
            name
            for name, previous in allocated
            if previous.last_live_cycle < item.first_live_cycle
            and previous.width == item.width
            and previous.signed == item.signed
        ), None)
        if register is None:
            register = f"shared_reg_{len(allocated)}"
            allocated.append((register, item))
        else:
            allocated = [
                (name, item if name == register else previous)
                for name, previous in allocated
            ]
        result.append(RegisterBinding(item.value_id, register))
    return tuple(result)


class SharedArithmeticProvider:
    """Operation-specific provider for one non-interleaved multiply/add plan."""

    # The schedule accepts one transaction, performs two multiplies plus an
    # add, then permits same-edge output retirement/input reload.  Timing is
    # derived below from the final scheduled result cycle.
    ADMISSION_POLICY = TemporalAdmissionPolicy.RETIRE_AND_RELOAD

    def spatial_candidate(
        self,
        value: expr.Expression,
        *,
        allocate_instance: Callable[[], int],
        domain: str,
        constraints: tuple[object, ...],
    ) -> PipelineCandidate:
        """Return the ordinary II=1 spatial control candidate for this shape."""

        shape = match_shared_multiply_add(value)
        staged = expr.Pipeline(1, value, allocate_instance(), value.type, domain=domain)
        candidate = PipelineCandidate(
            "spatial_mul2",
            staged,
            PipelineTree.DAG,
            RegisterPlacement.OUTPUT,
            MultiplierMapping.DSP,
            ("spatial", "two_multiply_resources", "register_output"),
            1,
            1,
            PipelineEstimate(
                max(2, shape.addition.type.width // 3),
                shape.addition.type.width + 1,
                2,
                300,
            ),
            PipelineCostSource.STRUCTURAL_ESTIMATE,
            pipeline_plan=PipelinePlan(("register_output",), 1),
        )
        return replace(candidate, violations=pipeline_candidate_violations(candidate, constraints))

    def candidate(
        self,
        value: expr.Expression,
        *,
        semantic_region_identity: str,
        constraints: tuple[object, ...],
        input_type: HardwareType,
    ) -> SharedArithmeticCandidate:
        shape = match_shared_multiply_add(value)
        product0_identity = expression_semantic_identity(shape.product0)
        product1_identity = expression_semantic_identity(shape.product1)
        add_identity = expression_semantic_identity(shape.addition)
        captured_operands = (
            shape.product0.left,
            shape.product0.right,
            shape.product1.left,
            shape.product1.right,
        )
        payload_roots = tuple(
            operand.expression
            for operand in captured_operands
            if isinstance(operand, expr.FieldAccess)
        )
        if len(payload_roots) == len(captured_operands) and len({
            expression_semantic_identity(item) for item in payload_roots
        }) == 1:
            # The lowering snapshots the entire RV payload, including any
            # source fields unused by this arithmetic shape.
            capture_width = payload_roots[0].type.width
        else:
            capture_width = sum(
                operand.type.width
                for index, operand in enumerate(captured_operands)
                if expression_semantic_identity(operand) not in {
                    expression_semantic_identity(previous)
                    for previous in captured_operands[:index]
                }
            )
        operations = (
            ScheduledOperation("mul0", product0_identity, "multiply", 1, 1, "multiply"),
            ScheduledOperation("mul1", product1_identity, "multiply", 2, 2, "multiply"),
            ScheduledOperation("add0", add_identity, "add", 3, 3, "add"),
        )
        lifetimes = (
            ValueLifetime("input", 0, 2, capture_width, False),
            ValueLifetime("mul0", 1, 3, shape.product0.type.width, isinstance(shape.product0.type, SIntType)),
            ValueLifetime("mul1", 2, 3, shape.product1.type.width, isinstance(shape.product1.type, SIntType)),
            ValueLifetime("result", 3, 4, shape.addition.type.width, isinstance(shape.addition.type, SIntType)),
        )
        bindings = allocate_temporal_registers(lifetimes)
        storage = build_temporal_storage_plan(
            lifetimes=lifetimes,
            bindings=bindings,
            value_types={
                "input": input_type,
                "mul0": shape.product0.type,
                "mul1": shape.product1.type,
                "result": shape.addition.type,
            },
            control_ff=3,
        )
        admission = derive_noninterleaved_admission(
            final_result_cycle=max(item.result_cycle for item in operations),
            policy=self.ADMISSION_POLICY,
        )
        cost = TemporalResourceCost(
            # Operand/result selection and the small counter are deliberately
            # charged; one multiplier is never advertised as free control.
            lut=max(4, shape.addition.type.width // 2) + 2,
            ff=storage.ff_cost,
            dsp=1,
        )
        resources = (
            ResourceBinding("mul0", "multiply0"),
            ResourceBinding("mul1", "multiply0"),
            ResourceBinding("add0", "add0"),
        )
        dependencies = (("mul0", "add0"), ("mul1", "add0"))
        identity = TemporalImplementationGraph.identity_data(
            semantic_region_identity=semantic_region_identity,
            operations=operations,
            dependencies=dependencies,
            resource_bindings=resources,
            value_lifetimes=lifetimes,
            register_bindings=bindings,
            storage_plan=storage,
            admission_policy=admission.policy,
            latency=admission.latency,
            initiation_interval=admission.initiation_interval,
            capacity=admission.capacity,
            temporal_class=TemporalClass.FLOW_CONTROLLED,
            resource_cost=cost,
        )
        graph = TemporalImplementationGraph(
            semantic_region_identity, operations, dependencies, resources, lifetimes,
            bindings, storage, admission.policy, admission.latency,
            admission.initiation_interval, admission.capacity,
            TemporalClass.FLOW_CONTROLLED, cost, identity,
        )
        candidate = PipelineCandidate(
            "shared_noninterleaved_mul1",
            value,
            PipelineTree.DAG,
            RegisterPlacement.SCHEDULED_DAG,
            # Existing report/RTL architecture values have no generic
            # 'multiply resource' spelling.  DSP is a cost unit here, not a
            # target primitive binding; target providers remain responsible
            # for physical DSP mapping.
            MultiplierMapping.DSP,
            ("shared_noninterleaved", "capacity_one", "transactional"),
            graph.latency,
            graph.initiation_interval,
            PipelineEstimate(cost.lut, cost.ff, cost.dsp, 200),
            PipelineCostSource.STRUCTURAL_ESTIMATE,
            pipeline_plan=PipelinePlan(
                ("shared_noninterleaved",), graph.latency,
                initiation_interval=graph.initiation_interval,
            ),
        )
        return SharedArithmeticCandidate(
            replace(candidate, violations=pipeline_candidate_violations(candidate, constraints)),
            graph,
        )


__all__ = [
    "allocate_temporal_registers",
    "SharedArithmeticCandidate",
    "SharedArithmeticError",
    "SharedArithmeticProvider",
    "SharedMultiplyAddShape",
    "match_shared_multiply_add",
]
