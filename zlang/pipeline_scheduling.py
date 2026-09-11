"""Deterministic physical partitioning for exact fixed scalar pipelines.

The source ``pipeline(N)`` contract is already typed before this module runs.
This scheduler never changes an arithmetic node, width, signedness, rounding or
overflow policy.  It only inserts ordinary typed one-cycle :class:`Pipeline`
boundaries and exact-width balancing paths.  The resulting graph is consumed
unchanged by simulation and the production direct-SystemVerilog backend.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from hashlib import sha256
from typing import Callable, Protocol

from zlang.ir import expressions as expr
from zlang.ir.functional import lower_reduction
from zlang.ir.pipelines import (
    PipelineCostSource,
    PipelinePlan,
    ScheduledOperationCost,
    ScheduledPipelineOperation,
    ScheduledPipelineStage,
)
from zlang.ir.scheduled import ScheduledValueGraph
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.module import Assignment, EquivalenceRule, Module, Port, PortDirection
from zlang.ir.target import (
    ImplementationDelay,
    ResourceDefinition,
    TimingCut,
    TimingDAG,
    TimingEdge,
    TimingNode,
)
from zlang.ir.traversal import (
    ExpressionTraversalPolicy,
    expression_children,
    walk_expression,
)
from zlang.ir.types import (
    BitType,
    BitsType,
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
)
from zlang.timing import timing_info, validate_timed_candidate


FIXED_PIPELINE_SCHEDULER = "dag_partition_v1"
FIXED_PIPELINE_SCHEDULE_SCHEMA = "zlang-fixed-pipeline-schedule-v1"


class PipelineSchedulingError(ValueError):
    """A fixed pipeline body cannot be represented by the bounded scheduler."""


class OperationCostModel(Protocol):
    """Backend-independent cost interface used before register placement."""

    @property
    def source(self) -> PipelineCostSource: ...

    def cost(self, operation: expr.Expression, operation_class: str) -> ScheduledOperationCost: ...


@dataclass(frozen=True)
class StructuralOperationCostModel:
    """Deterministic width-aware estimates, never synthesis measurements."""

    source: PipelineCostSource = PipelineCostSource.STRUCTURAL_ESTIMATE

    def cost(
        self,
        operation: expr.Expression,
        operation_class: str,
    ) -> ScheduledOperationCost:
        width = operation.type.width
        operand_widths = tuple(child.type.width for child in _operation_children(operation))
        largest = max((*operand_widths, width), default=width)

        if operation_class in {"multiply", "fixed_multiply"}:
            smaller = min(operand_widths, default=width)
            delay = 520 + 28 * largest + 8 * smaller
            lut = max(1, smaller * largest)
            resource = "logic_multiplier"
        elif operation_class in {"add", "subtract", "fixed_add", "fixed_subtract"}:
            delay = 110 + 18 * width
            lut = width
            resource = "logic_adder"
        elif operation_class == "fixed_convert":
            assert isinstance(operation, expr.FixedConvert)
            discarded = max(0, operation.expression.type.width - operation.type.width)
            rounding = 0 if discarded == 0 else {
                expr.FixedRounding.FLOOR: 45,
                expr.FixedRounding.TOWARD_ZERO: 90,
                expr.FixedRounding.AWAY_ZERO: 130,
                expr.FixedRounding.NEAREST_EVEN: 180,
            }[operation.rounding]
            saturation = 120 if operation.overflow is expr.FixedOverflow.SATURATE else 0
            delay = 35 + 8 * largest + rounding + saturation
            lut = max(1, width + discarded)
            resource = "fixed_conversion"
        elif operation_class in {"shift", "runtime_index"}:
            constant_shift = (
                isinstance(operation, expr.Binary)
                and isinstance(operation.right, expr.Constant)
            )
            delay = 45 if constant_shift else 170 + 7 * largest
            lut = 0 if constant_shift else width
            resource = "routing" if constant_shift else "logic_mux"
        elif operation_class in {"mux", "switch"}:
            delay = 100 + 7 * width
            lut = max(1, width)
            resource = "logic_mux"
        elif operation_class == "compare":
            delay = 95 + 11 * largest
            lut = max(1, largest)
            resource = "logic_compare"
        elif operation_class == "bitwise":
            delay = 35 + 3 * width
            lut = max(1, width)
            resource = "logic_bitwise"
        elif operation_class in {
            "extend", "truncate", "slice", "concat", "bitcast", "pack",
            "unpack", "field", "tuple_project", "vector_index", "reshape",
            "enum_encode", "enum_valid", "enum_decode", "struct_construct",
            "tuple_construct",
        }:
            delay = 0 if operation_class in {
                "extend", "truncate", "slice", "concat", "bitcast", "pack",
                "unpack", "field", "tuple_project", "vector_index", "reshape",
                "enum_encode", "struct_construct", "tuple_construct",
            } else 50 + 4 * largest
            lut = 0 if delay == 0 else max(1, largest)
            resource = "wiring" if delay == 0 else "logic_decode"
        else:  # pragma: no cover - the closed classifier rejects first.
            raise PipelineSchedulingError(
                f"no structural pipeline cost for operation '{operation_class}'"
            )
        return ScheduledOperationCost(delay, lut, 0, resource, self.source)


@dataclass(frozen=True)
class TargetResourceOperationCostModel:
    """Use source-described target segment costs, with an honest fallback.

    A zero ``delay_ps`` means that the target library has not published a
    timing estimate; it is never interpreted as a zero-delay DSP path.  In
    that case the generic structural operation remains in the DAG and cannot
    satisfy target-frequency policy merely because a primitive exists.
    """

    resources: tuple[ResourceDefinition, ...]
    structural: StructuralOperationCostModel = StructuralOperationCostModel()

    @property
    def source(self) -> PipelineCostSource:
        return (
            PipelineCostSource.TARGET_ESTIMATE
            if any(
                site.estimated_delay_ps > 0
                for resource in self.resources
                for site in resource.pipeline_sites
            )
            else PipelineCostSource.STRUCTURAL_ESTIMATE
        )

    def cost(
        self,
        operation: expr.Expression,
        operation_class: str,
    ) -> ScheduledOperationCost:
        generic = self.structural.cost(operation, operation_class)
        if operation_class not in {"multiply", "fixed_multiply"}:
            return replace(generic, source=self.source)
        operands = _operation_children(operation)
        for resource in sorted(self.resources, key=lambda item: item.identity):
            if resource.resource_class != "dsp_mac" or len(operands) != 2:
                continue
            try:
                fits = (
                    _physical_dsp_width(operands[0].type)
                    <= resource.limit("multiplier_a")
                    and _physical_dsp_width(operands[1].type)
                    <= resource.limit("multiplier_b")
                ) or (
                    _physical_dsp_width(operands[1].type)
                    <= resource.limit("multiplier_a")
                    and _physical_dsp_width(operands[0].type)
                    <= resource.limit("multiplier_b")
                )
            except ValueError:
                continue
            site = next(
                (
                    item for item in resource.pipeline_sites
                    if item.semantic_location == "multiply"
                    and item.estimated_delay_ps > 0
                ),
                None,
            )
            if not fits or site is None:
                continue
            return ScheduledOperationCost(
                site.estimated_delay_ps,
                0,
                1,
                resource.resource_class,
                PipelineCostSource.TARGET_ESTIMATE,
                resource.name,
            )
        return replace(generic, source=self.source)


def _physical_dsp_width(type_: HardwareType) -> int:
    """Width of one value when connected to a signed DSP arithmetic port."""

    return type_.width + int(isinstance(type_, (UIntType, UFixedType)))


@dataclass(frozen=True)
class _DagNode:
    expression: expr.Expression
    children: tuple[expr.Expression, ...]
    semantic_identity: str
    operation_class: str
    ordinal: int
    cost: ScheduledOperationCost


@dataclass(frozen=True)
class _Placement:
    stage: int
    arrival_ps: int


@dataclass(frozen=True)
class _ValueAlternative:
    expression: expr.Expression
    certificate: tuple[str, ...]


def schedule_fixed_pipeline(
    source_expression: expr.Expression,
    requested_latency: int,
    allocate_instance: Callable[[], int],
    *,
    cost_model: OperationCostModel | None = None,
    semantic_source_expression: expr.Expression | None = None,
    rewrite_certificate: tuple[str, ...] = (),
) -> expr.Pipeline:
    """Partition one exact pure expression into ``requested_latency`` cycles."""

    if requested_latency < 1:
        raise PipelineSchedulingError("pipeline latency must be positive")
    model = cost_model or StructuralOperationCostModel()
    source = _lower_executable_value(source_expression)
    semantic_source = _lower_executable_value(
        semantic_source_expression
        if semantic_source_expression is not None
        else source_expression
    )
    nodes, leaves = _build_dag(source, model)
    # A single combinational operation has no useful internal cut.  Retain the
    # established fixed-pipeline RTL shape (one enclosing N-cycle boundary),
    # but still publish a complete schedule so exact-value rewrites and target
    # planning remain observable and verifiable.
    if len(nodes) <= 1:
        scheduled = expr.Pipeline(
            requested_latency,
            source,
            allocate_instance(),
            source.type,
            origin=source.origin,
        )
        semantic_source_identity = expression_semantic_identity(semantic_source)
        selected_value_identity = expression_semantic_identity(source)
        scheduled_identity = expression_semantic_identity(scheduled)
        operations = tuple(
            ScheduledPipelineOperation(
                _identity(
                    FIXED_PIPELINE_SCHEDULE_SCHEMA,
                    "operation",
                    node.semantic_identity,
                    node.ordinal,
                ),
                node.semantic_identity,
                node.ordinal,
                node.operation_class,
                0,
                tuple(
                    _identity(
                        FIXED_PIPELINE_SCHEDULE_SCHEMA,
                        "leaf",
                        expression_semantic_identity(child),
                    )
                    for child in node.children
                ),
                tuple(child.type for child in node.children),
                node.expression.type,
                node.cost,
                node.expression.origin,
            )
            for node in nodes
        )
        stages = tuple(
            ScheduledPipelineStage(
                index,
                tuple(item.identity for item in operations) if index == 0 else (),
                sum(item.cost.delay_ps for item in operations) if index == 0 else 0,
            )
            for index in range(requested_latency)
        )
        timing_nodes = tuple(
            TimingNode(
                item.identity,
                item.operation,
                item.identity,
                item.semantic_identity,
                estimated_delay_ps=item.cost.delay_ps,
            )
            for item in operations
        )
        cut_identity = _identity(
            FIXED_PIPELINE_SCHEDULE_SCHEMA,
            "trivial_output_cut",
            selected_value_identity,
            requested_latency,
        )
        timing_dag = TimingDAG(
            timing_nodes,
            (),
            (
                TimingCut(
                    cut_identity,
                    operations[0].identity if operations else selected_value_identity,
                    "generic",
                    requested_latency,
                ),
            ),
            output_latency=requested_latency,
            estimated_critical_delay_ps=stages[0].estimated_delay_ps,
        )
        graph = ScheduledValueGraph(
            semantic_source_identity,
            selected_value_identity,
            tuple(item.identity for item in operations),
            tuple((item.identity, item.semantic_identity) for item in operations),
            (),
            tuple((item.identity, item.stage) for item in operations),
            tuple(item.estimated_delay_ps for item in stages),
            requested_latency,
            1,
            model.source.value,
            cut_identities=(cut_identity,),
            rewrite_certificate=rewrite_certificate,
        )
        plan = PipelinePlan(
            stage_boundaries=tuple(
                f"stage_{index}" for index in range(requested_latency)
            ),
            inserted_registers=requested_latency,
            scheduler=FIXED_PIPELINE_SCHEDULER,
            requested_latency=requested_latency,
            initiation_interval=1,
            cost_source=model.source,
            operations=operations,
            stages=stages,
            timing_dag=timing_dag,
            source_expression_identity=semantic_source_identity,
            scheduled_expression_identity=scheduled_identity,
            timed_equivalence="verified",
            scheduled_value_graph=graph,
            selected_value_identity=selected_value_identity,
            rewrite_certificate=rewrite_certificate,
            source_expression=semantic_source,
        )
        return replace(scheduled, pipeline_plan=plan)
    placements, stage_delays = _partition(nodes, requested_latency)

    registered: dict[tuple[expr.Expression, int], expr.Expression] = {}
    combinational: dict[expr.Expression, expr.Expression] = {}
    alignment_records: dict[tuple[str, str, int, int], ImplementationDelay] = {}
    compensation_records: list[ImplementationDelay] = []
    captured_operations: set[str] = set()

    node_by_expression = {node.expression: node for node in nodes}
    operation_identity = {
        node.expression: _identity(
            FIXED_PIPELINE_SCHEDULE_SCHEMA,
            "operation",
            node.semantic_identity,
            node.ordinal,
        )
        for node in nodes
    }
    leaf_identity = {
        leaf: _identity(
            FIXED_PIPELINE_SCHEDULE_SCHEMA,
            "leaf",
            expression_semantic_identity(leaf),
        )
        for leaf in leaves
    }

    def dynamic_leaf(value: expr.Expression) -> bool:
        return not isinstance(value, (expr.Constant, expr.ParameterRef))

    def delay_to(
        value: expr.Expression,
        current_latency: int,
        target_latency: int,
        *,
        destination: str,
        compensation: bool = False,
        identity_value: expr.Expression | None = None,
        record_delay: bool = True,
    ) -> expr.Expression:
        if target_latency < current_latency:
            raise PipelineSchedulingError("pipeline schedule violates dependency order")
        result = value
        semantic_value = value if identity_value is None else identity_value
        source_identity = (
            operation_identity[semantic_value]
            if semantic_value in operation_identity
            else leaf_identity[semantic_value]
        )
        for latency in range(current_latency + 1, target_latency + 1):
            key = (semantic_value, latency)
            cached = registered.get(key)
            if cached is None:
                cached = expr.Pipeline(
                    1,
                    result,
                    allocate_instance(),
                    semantic_value.type,
                    origin=semantic_value.origin,
                )
                registered[key] = cached
            result = cached
        cycles = target_latency - current_latency
        if cycles and record_delay:
            record = ImplementationDelay(
                identity=_identity(
                    FIXED_PIPELINE_SCHEDULE_SCHEMA,
                    "compensation" if compensation else "alignment",
                    source_identity,
                    destination,
                    current_latency,
                    target_latency,
                    semantic_value.type.width,
                ),
                kind="compensation" if compensation else "alignment",
                source_node=source_identity,
                destination_node=destination,
                cycles=cycles,
                width=semantic_value.type.width,
                ff_cost=cycles * semantic_value.type.width,
                semantic_identity=expression_semantic_identity(semantic_value),
                source_origin=(
                    semantic_value.origin.render()
                    if semantic_value.origin is not None else None
                ),
            )
            if compensation:
                compensation_records.append(record)
            else:
                alignment_records.setdefault(
                    (source_identity, destination, current_latency, target_latency),
                    record,
                )
        return result

    def value_at_input(value: expr.Expression, stage: int, destination: str) -> expr.Expression:
        child_node = node_by_expression.get(value)
        if child_node is None:
            if not dynamic_leaf(value):
                return value
            return delay_to(value, 0, stage, destination=destination)
        child_stage = placements[value].stage
        if child_stage == stage:
            return compute(value)
        if child_stage > stage:
            raise PipelineSchedulingError("consumer precedes its producer")
        produced = delay_to(
            compute(value), child_stage, child_stage + 1,
            destination=operation_identity[value],
            identity_value=value,
            record_delay=False,
        )
        captured_operations.add(operation_identity[value])
        return delay_to(
            produced,
            child_stage + 1,
            stage,
            destination=destination,
            identity_value=value,
        )

    def compute(value: expr.Expression) -> expr.Expression:
        cached = combinational.get(value)
        if cached is not None:
            return cached
        node = node_by_expression[value]
        destination = operation_identity[value]
        rewritten = tuple(
            value_at_input(child, placements[value].stage, destination)
            for child in node.children
        )
        result = _replace_direct_children(value, node.children, rewritten)
        combinational[value] = result
        return result

    root_node = node_by_expression.get(source)
    if root_node is None:
        # A constant pipeline still has reset/fill behavior and therefore must
        # retain real state even though its source value is timeless.
        # Preserve the public shape of the historical fixed-pipeline node for
        # leaf values: one enclosing node records the complete requested
        # latency, rather than exposing an implementation-only chain of
        # one-cycle wrappers.  The scheduler plan below still records the
        # empty operation DAG and exact timing contract.
        scheduled: expr.Expression = expr.Pipeline(
            requested_latency,
            source,
            allocate_instance(),
            source.type,
            origin=source.origin,
        )
    else:
        root_stage = placements[source].stage
        scheduled = delay_to(
            compute(source), root_stage, root_stage + 1,
            destination=operation_identity[source],
            identity_value=source,
            record_delay=False,
        )
        captured_operations.add(operation_identity[source])
        scheduled = delay_to(
            scheduled,
            root_stage + 1,
            requested_latency,
            destination="output",
            compensation=True,
            identity_value=source,
        )

    if not isinstance(scheduled, expr.Pipeline):
        raise PipelineSchedulingError("fixed pipeline schedule has no output boundary")
    erased = erase_pipeline_timing(scheduled)
    if erased != source:
        raise PipelineSchedulingError(
            "scheduled pipeline does not reconstruct the exact typed source expression"
        )
    relation = validate_timed_candidate(source, scheduled, value_equivalent=True)
    if not relation.equivalent or relation.delta != requested_latency:
        raise PipelineSchedulingError(
            "scheduled pipeline failed exact timed-equivalence validation: "
            f"{relation.proof}; delta={relation.delta}"
        )
    if timing_info(scheduled).latency != requested_latency:
        raise PipelineSchedulingError("scheduled pipeline has the wrong structural latency")

    operations = tuple(
        ScheduledPipelineOperation(
            identity=operation_identity[node.expression],
            semantic_identity=node.semantic_identity,
            ordinal=node.ordinal,
            operation=node.operation_class,
            stage=placements[node.expression].stage,
            operand_identities=tuple(
                operation_identity.get(child, leaf_identity.get(child, ""))
                for child in node.children
            ),
            operand_types=tuple(child.type for child in node.children),
            result_type=node.expression.type,
            cost=node.cost,
            source_origin=node.expression.origin,
        )
        for node in nodes
    )
    stages = tuple(
        ScheduledPipelineStage(
            index,
            tuple(
                operation.identity
                for operation in operations
                if operation.stage == index
            ),
            stage_delays[index],
        )
        for index in range(requested_latency)
    )
    timing_nodes = tuple(
        TimingNode(
            operation.identity,
            operation.operation,
            operation.identity,
            operation.semantic_identity,
            latency=operation.stage,
            estimated_delay_ps=operation.cost.delay_ps,
            source_origin=(
                operation.source_origin.render()
                if operation.source_origin is not None else None
            ),
        )
        for operation in operations
    )
    timing_edges = tuple(
        TimingEdge(
            _identity(
                FIXED_PIPELINE_SCHEDULE_SCHEMA,
                "edge",
                operand,
                operation.identity,
                index,
            ),
            "semantic",
            operand,
            operation.identity,
            latency=max(
                0,
                operation.stage - _producer_stage(
                    operand, operations, leaf_identity
                ),
            ),
        )
        for operation in operations
        for index, operand in enumerate(operation.operand_identities)
    )
    cuts = tuple(
        TimingCut(
            _identity(FIXED_PIPELINE_SCHEDULE_SCHEMA, "cut", operation.identity),
            operation.identity,
            "generic",
            1,
        )
        for operation in operations
        if operation.identity in captured_operations
    )
    all_pipeline_nodes = tuple(
        node
        for node in walk_expression(
            scheduled,
            policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
        )
        if isinstance(node, expr.Pipeline)
    )
    scheduled_identity = expression_semantic_identity(scheduled)
    semantic_source_identity = expression_semantic_identity(semantic_source)
    selected_value_identity = expression_semantic_identity(source)
    dag = TimingDAG(
        timing_nodes,
        timing_edges,
        cuts,
        tuple(alignment_records.values()),
        tuple(compensation_records),
        requested_latency,
        max(stage_delays, default=0),
    )
    scheduled_value_graph = ScheduledValueGraph(
        source_expression_identity=semantic_source_identity,
        selected_value_identity=selected_value_identity,
        operation_identities=tuple(item.identity for item in operations),
        operation_semantic_identities=tuple(
            (item.identity, item.semantic_identity) for item in operations
        ),
        dependencies=tuple(
            (item.source_node, item.destination_node, item.latency)
            for item in timing_edges
        ),
        stage_assignment=tuple((item.identity, item.stage) for item in operations),
        stage_delays_ps=stage_delays,
        exact_latency=requested_latency,
        initiation_interval=1,
        cost_source=model.source.value,
        cut_identities=tuple(item.identity for item in cuts),
        alignment_delay_identities=tuple(
            item.identity for item in alignment_records.values()
        ),
        compensation_delay_identities=tuple(
            item.identity for item in compensation_records
        ),
        rewrite_certificate=rewrite_certificate,
    )
    plan = PipelinePlan(
        stage_boundaries=tuple(f"stage_{index}" for index in range(requested_latency)),
        inserted_registers=sum(node.stages for node in all_pipeline_nodes),
        alignment_delays=tuple(
            item.cycles for item in alignment_records.values()
        ),
        scheduler=FIXED_PIPELINE_SCHEDULER,
        requested_latency=requested_latency,
        initiation_interval=1,
        cost_source=model.source,
        operations=operations,
        stages=stages,
        timing_dag=dag,
        source_expression_identity=semantic_source_identity,
        scheduled_expression_identity=scheduled_identity,
        timed_equivalence="verified",
        scheduled_value_graph=scheduled_value_graph,
        selected_value_identity=selected_value_identity,
        rewrite_certificate=rewrite_certificate,
        source_expression=semantic_source,
    )
    return replace(scheduled, pipeline_plan=plan, origin=source_expression.origin)


def schedule_module_fixed_pipelines(
    module: Module,
    *,
    cost_model: OperationCostModel | None = None,
) -> Module:
    """Materialize exact source pipeline contracts during implementation planning.

    Semantic analysis owns the exact latency and typed value only.  Physical
    register placement is deferred until this function, where a target-aware
    cost model can be supplied.  The default remains the honest structural
    model for targetless compilation.
    """

    existing_instances = tuple(
        item.instance
        for assignment in (*module.assignments, *module.next_assignments)
        for item in walk_expression(
            assignment.expression,
            policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
        )
        if isinstance(item, (expr.Delay, expr.Pipeline))
    )
    next_instance = [max(existing_instances, default=-1) + 1]

    def allocate() -> int:
        result = next_instance[0]
        next_instance[0] += 1
        return result

    def materialize(value: expr.Expression) -> expr.Expression:
        if isinstance(value, expr.Pipeline) and value.pipeline_plan is None:
            alternatives = _exact_value_alternatives(
                value.expression,
                module.equivalences,
            )

            def preview(item: _ValueAlternative) -> tuple[object, ...]:
                instance = [0]

                def allocate_preview() -> int:
                    result = instance[0]
                    instance[0] += 1
                    return result

                candidate = schedule_fixed_pipeline(
                    item.expression,
                    value.stages,
                    allocate_preview,
                    cost_model=cost_model,
                    semantic_source_expression=value.expression,
                    rewrite_certificate=item.certificate,
                )
                plan = candidate.pipeline_plan
                assert plan is not None
                critical = max(
                    (stage.estimated_delay_ps for stage in plan.stages),
                    default=0,
                )
                return (
                    critical,
                    sum(operation.cost.lut for operation in plan.operations),
                    sum(operation.cost.dsp for operation in plan.operations),
                    plan.selected_value_identity,
                )

            selected = min(alternatives, key=preview)
            first = [value.instance]

            def allocate_region() -> int:
                if first:
                    return first.pop()
                return allocate()

            return schedule_fixed_pipeline(
                selected.expression,
                value.stages,
                allocate_region,
                cost_model=cost_model,
                semantic_source_expression=value.expression,
                rewrite_certificate=selected.certificate,
            )
        return value

    assignments = tuple(
        replace(item, expression=materialize(item.expression))
        for item in module.assignments
    )
    next_assignments = tuple(
        replace(item, expression=materialize(item.expression))
        for item in module.next_assignments
    )
    children = tuple(
        schedule_module_fixed_pipelines(child, cost_model=cost_model)
        for child in module.children
    )
    return replace(
        module,
        assignments=assignments,
        next_assignments=next_assignments,
        children=children,
    )


def _exact_value_alternatives(
    value: expr.Expression,
    equivalences: tuple[EquivalenceRule, ...],
) -> tuple[_ValueAlternative, ...]:
    """Return bounded exact-typed egglog alternatives useful to scheduling.

    The scheduler remains responsible for every clock/register decision.
    Egglog is invoked only when the graph contains a known simplification
    opportunity; commutation-only saturation cannot improve the current cost
    model and would add substantial compile time to large FFT hierarchies.
    """

    source = _lower_executable_value(value)
    original = _ValueAlternative(source, ())
    if not _may_benefit_from_value_saturation(source):
        return (original,)
    try:
        from zlang.opt import lower, saturate, term_to_expression

        inputs: dict[str, HardwareType] = {}
        for item in walk_expression(
            source,
            policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
        ):
            if isinstance(item, expr.InputRef):
                previous = inputs.setdefault(item.name, item.type)
                if previous != item.type:
                    raise PipelineSchedulingError(
                        f"pipeline input '{item.name}' has inconsistent types"
                    )
        output_name = "__zlang_pipeline_value"
        while output_name in inputs:
            output_name += "_"
        output = Port(PortDirection.OUTPUT, output_name, source.type)
        temporary = Module(
            "__ZLangPipelineValue",
            (
                *(Port(PortDirection.INPUT, name, type_)
                  for name, type_ in sorted(inputs.items())),
                output,
            ),
            (Assignment(output, source),),
            equivalences=equivalences,
        )
        canonical = lower(temporary)
        result = saturate(
            canonical,
            canonical.assignments[0].expression,
            max_iterations=8,
            max_terms=16,
        )
        fired = tuple(
            registration.identity
            for registration in result.registrations
            if registration.fired
        )
        by_identity = {expression_semantic_identity(source): original}
        for term in result.alternatives:
            alternative = term_to_expression(term)
            if source.origin is not None:
                alternative = replace(alternative, origin=source.origin)
            identity = expression_semantic_identity(alternative)
            by_identity.setdefault(
                identity,
                _ValueAlternative(
                    alternative,
                    (
                        "egglog_exact_typed",
                        f"selected_value={identity}",
                        *(f"rewrite={name}" for name in fired),
                    ),
                ),
            )
        return tuple(
            (original, *(
                by_identity[identity]
                for identity in sorted(by_identity)
                if identity != expression_semantic_identity(source)
            ))
        )
    except (TypeError, ValueError):
        # Ineligible e-graph forms remain valid exact source pipelines.  The
        # fail-closed operation classifier below still diagnoses unsupported
        # state/protocol/timing nodes.
        return (original,)


def _may_benefit_from_value_saturation(value: expr.Expression) -> bool:
    for item in walk_expression(
        value,
        policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
    ):
        if isinstance(item, expr.Mux) and (
            isinstance(item.condition, expr.Constant)
            or item.when_true == item.when_false
        ):
            return True
        if isinstance(item, (expr.Add, expr.Binary)):
            children = _operation_children(item)
            constants = tuple(
                child for child in children if isinstance(child, expr.Constant)
            )
            if any(constant.value in {0, 1} for constant in constants):
                return True
            if (
                isinstance(item, expr.Binary)
                and item.operator is expr.BinaryOperator.MULTIPLY
                and any(
                    constant.value > 1
                    and constant.value & (constant.value - 1) == 0
                    for constant in constants
                )
            ):
                return True
    return False


def erase_pipeline_timing(value: expr.Expression) -> expr.Expression:
    """Remove only physical Pipeline nodes, preserving exact value structure."""

    if isinstance(value, expr.Pipeline):
        return erase_pipeline_timing(value.expression)
    children = expression_children(
        value,
        policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
    )
    if not children:
        return value
    rewritten = tuple(erase_pipeline_timing(child) for child in children)
    return _replace_direct_children(value, children, rewritten)


def render_fixed_pipeline_plan(output: str, plan: PipelinePlan) -> str:
    """Render one stable, evidence-honest fixed-pipeline report."""

    if plan.scheduler == "legacy" or plan.requested_latency is None:
        return ""
    lines = [
        f"pipeline output={output}",
        f"requested_latency={plan.requested_latency}",
        f"ii={plan.initiation_interval}",
        f"scheduler={plan.scheduler}",
        f"cost_source={plan.cost_source.value}",
        f"selected_value={plan.selected_value_identity}",
        "rewrite_certificate="
        + (", ".join(plan.rewrite_certificate) or "source_exact"),
    ]
    by_identity = {item.identity: item for item in plan.operations}
    for stage in plan.stages:
        lines.append(f"stage {stage.index}")
        lines.append("  operations:")
        if not stage.operation_identities:
            lines.append("    none (output compensation)")
        else:
            for identity in stage.operation_identities:
                operation = by_identity[identity]
                operands = ",".join(item[:12] for item in operation.operand_identities)
                signature = ",".join(str(item) for item in operation.operand_types)
                lines.append(
                    f"    {operation.operation}<{signature}->{operation.result_type}> "
                    f"({operands}) -> {identity[:12]} "
                    f"delay={operation.cost.delay_ps}ps "
                    f"resource={operation.cost.resource_class}"
                )
        balancing = tuple(
            item for item in plan.timing_dag.alignment_delays
            if item.destination_node in stage.operation_identities
        )
        lines.append(f"  balancing_registers={sum(item.cycles for item in balancing)}")
        lines.append(f"  estimated_delay={stage.estimated_delay_ps / 1000:.3f}ns")
    critical_stage = max(
        plan.stages,
        key=lambda item: (item.estimated_delay_ps, -item.index),
    )
    critical = critical_stage.estimated_delay_ps
    fmax = 0 if critical == 0 else 1_000_000 // critical
    lines.extend((
        f"critical_stage={critical_stage.index}",
        f"estimated_critical_delay={critical / 1000:.3f}ns",
        f"estimated_fmax={fmax}MHz",
        f"visible_latency={plan.requested_latency}",
        f"ii={plan.initiation_interval}",
        f"timed_equivalence={plan.timed_equivalence}",
        f"physical_pipeline_register_nodes={plan.inserted_registers}",
    ))
    return "\n".join(lines) + "\n"


def _lower_executable_value(value: expr.Expression) -> expr.Expression:
    if isinstance(value, expr.Reduce):
        return _lower_executable_value(lower_reduction(value))
    if isinstance(value, expr.ImplementationChoice):
        return _lower_executable_value(value.selected_alternative.expression)
    children = expression_children(
        value,
        policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
    )
    if not children:
        return value
    rewritten = tuple(_lower_executable_value(child) for child in children)
    return _replace_direct_children(value, children, rewritten)


def _build_dag(
    root: expr.Expression,
    cost_model: OperationCostModel,
) -> tuple[tuple[_DagNode, ...], tuple[expr.Expression, ...]]:
    nodes: dict[expr.Expression, _DagNode] = {}
    leaves: dict[expr.Expression, None] = {}

    def visit(value: expr.Expression) -> None:
        operation_class = _operation_class(value)
        if operation_class is None:
            _validate_leaf(value)
            leaves.setdefault(value, None)
            return
        children = _operation_children(value)
        for child in children:
            visit(child)
        if value not in nodes:
            ordinal = len(nodes)
            nodes[value] = _DagNode(
                value,
                children,
                expression_semantic_identity(value),
                operation_class,
                ordinal,
                cost_model.cost(value, operation_class),
            )

    visit(root)
    return tuple(nodes.values()), tuple(leaves)


def _partition(
    nodes: tuple[_DagNode, ...],
    stages: int,
) -> tuple[dict[expr.Expression, _Placement], tuple[int, ...]]:
    if not nodes:
        return {}, tuple(0 for _ in range(stages))
    node_by_expression = {item.expression: item for item in nodes}

    def place(limit_ps: int) -> tuple[dict[expr.Expression, _Placement], bool]:
        result: dict[expr.Expression, _Placement] = {}
        for node in nodes:
            dependencies = tuple(
                result[child]
                for child in node.children
                if child in node_by_expression
            )
            stage = max((item.stage for item in dependencies), default=0)
            arrival = node.cost.delay_ps + max(
                (
                    item.arrival_ps
                    for item in dependencies
                    if item.stage == stage
                ),
                default=0,
            )
            if arrival > limit_ps:
                stage += 1
                arrival = node.cost.delay_ps
            result[node.expression] = _Placement(stage, arrival)
            if stage >= stages:
                return result, False
        return result, True

    maximum_single = max(item.cost.delay_ps for item in nodes)
    critical: dict[expr.Expression, int] = {}
    for node in nodes:
        critical[node.expression] = node.cost.delay_ps + max(
            (
                critical[child]
                for child in node.children
                if child in node_by_expression
            ),
            default=0,
        )
    lower = maximum_single
    upper = max(critical.values())
    while lower < upper:
        middle = (lower + upper) // 2
        _, feasible = place(middle)
        if feasible:
            upper = middle
        else:
            lower = middle + 1
    placements, feasible = place(lower)
    if not feasible:  # pragma: no cover - the full critical path is feasible.
        raise PipelineSchedulingError("cannot partition expression at requested latency")
    stage_delays = tuple(
        max(
            (
                placement.arrival_ps
                for placement in placements.values()
                if placement.stage == stage
            ),
            default=0,
        )
        for stage in range(stages)
    )
    return placements, stage_delays


def _operation_children(value: expr.Expression) -> tuple[expr.Expression, ...]:
    return expression_children(
        value,
        policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
    )


def _operation_class(value: expr.Expression) -> str | None:
    if isinstance(
        value,
        (expr.InputRef, expr.ParameterRef, expr.Constant, expr.RegisterRef),
    ):
        return None
    if isinstance(value, expr.Add):
        return "fixed_add" if isinstance(value.type, (FixedType, UFixedType)) else "add"
    if isinstance(value, expr.Binary):
        if value.operator is expr.BinaryOperator.MULTIPLY:
            return "fixed_multiply" if isinstance(value.type, (FixedType, UFixedType)) else "multiply"
        if value.operator is expr.BinaryOperator.SUBTRACT:
            return "fixed_subtract" if isinstance(value.type, (FixedType, UFixedType)) else "subtract"
        if value.operator in {
            expr.BinaryOperator.BIT_AND,
            expr.BinaryOperator.BIT_OR,
            expr.BinaryOperator.BIT_XOR,
        }:
            return "bitwise"
        if value.operator in {
            expr.BinaryOperator.SHIFT_LEFT,
            expr.BinaryOperator.SHIFT_RIGHT,
        }:
            return "shift"
        return "compare"
    unary = {
        expr.Extend: "extend",
        expr.Truncate: "truncate",
        expr.FixedConvert: "fixed_convert",
        expr.FieldAccess: "field",
        expr.TupleProject: "tuple_project",
        expr.VectorIndex: "vector_index",
        expr.Slice: "slice",
        expr.Bitcast: "bitcast",
        expr.Reshape: "reshape",
        expr.Pack: "pack",
        expr.Unpack: "unpack",
        expr.EnumEncode: "enum_encode",
        expr.EnumValid: "enum_valid",
    }
    for type_, name in unary.items():
        if isinstance(value, type_):
            return name
    if isinstance(value, expr.EnumDecode):
        return "enum_decode"
    if isinstance(value, expr.Mux):
        return "mux"
    if isinstance(value, expr.Switch):
        return "switch"
    if isinstance(value, (expr.Concat, expr.VectorConcat)):
        return "concat"
    if isinstance(value, expr.StructConstruct):
        return "struct_construct"
    if isinstance(value, expr.TupleConstruct):
        return "tuple_construct"
    if isinstance(value, expr.RuntimeIndex):
        return "runtime_index"
    if isinstance(value, (expr.Delay, expr.Pipeline)):
        raise PipelineSchedulingError(
            "nested delay/pipeline is not supported inside fixed pipeline scheduling"
        )
    if isinstance(value, expr.Call):
        raise PipelineSchedulingError(
            f"pipeline callable '{value.function}' was not expanded to concrete arithmetic"
        )
    raise PipelineSchedulingError(
        f"pipeline scheduling does not support typed node {type(value).__name__}"
    )


def _validate_leaf(value: expr.Expression) -> None:
    if isinstance(
        value,
        (expr.InputRef, expr.ParameterRef, expr.Constant, expr.RegisterRef),
    ):
        return
    raise PipelineSchedulingError(
        f"pipeline scheduling does not accept state/protocol leaf {type(value).__name__}"
    )


def _replace_direct_children(
    value: expr.Expression,
    original: tuple[expr.Expression, ...],
    rewritten: tuple[expr.Expression, ...],
) -> expr.Expression:
    if len(original) != len(rewritten):
        raise PipelineSchedulingError("expression child replacement is incomplete")
    replacements = {id(before): after for before, after in zip(original, rewritten, strict=True)}

    def walk(item: object, *, root: bool = False) -> object:
        if isinstance(item, expr.Expression) and not root:
            replacement = replacements.get(id(item))
            return replacement if replacement is not None else item
        if isinstance(item, tuple):
            return tuple(walk(child) for child in item)
        if is_dataclass(item) and not isinstance(item, type):
            updates: dict[str, object] = {}
            for field_ in fields(item):
                if not field_.init or field_.name in {
                    "origin", "source_origin", "type", "pipeline_plan",
                }:
                    continue
                current = getattr(item, field_.name)
                changed = walk(current)
                if changed is not current and changed != current:
                    updates[field_.name] = changed
                elif changed is not current:
                    updates[field_.name] = changed
            return replace(item, **updates) if updates else item
        return item

    result = walk(value, root=True)
    if not isinstance(result, expr.Expression):
        raise PipelineSchedulingError("expression rewrite did not return typed IR")
    return result


def _producer_stage(
    identity: str,
    operations: tuple[ScheduledPipelineOperation, ...],
    leaves: dict[expr.Expression, str],
) -> int:
    operation = next((item for item in operations if item.identity == identity), None)
    if operation is not None:
        return operation.stage
    if identity in leaves.values():
        return 0
    raise PipelineSchedulingError("timing edge references an unknown producer")


def _identity(*items: object) -> str:
    return sha256(repr(items).encode("utf-8")).hexdigest()


__all__ = [
    "FIXED_PIPELINE_SCHEDULE_SCHEMA",
    "FIXED_PIPELINE_SCHEDULER",
    "OperationCostModel",
    "PipelineSchedulingError",
    "StructuralOperationCostModel",
    "TargetResourceOperationCostModel",
    "erase_pipeline_timing",
    "render_fixed_pipeline_plan",
    "schedule_fixed_pipeline",
    "schedule_module_fixed_pipelines",
]
