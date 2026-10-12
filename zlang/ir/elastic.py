"""Typed IR for the bounded globally-stalled ready/valid pipeline slice.

This relation is deliberately separate from timing alignment fixed-latency equivalence.  A
selected scalar implementation has a fixed *advance* latency, while wall-clock
latency varies under downstream backpressure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from zlang.ir.expressions import Expression
from zlang.ir.pipelines import PipelineCandidate, PipelineConstraint
from zlang.ir.temporal import TemporalClass, TemporalImplementationGraph
from zlang.ir.types import HardwareType
from zlang.source import SourceOrigin


class ElasticStallPolicy(str, Enum):
    GLOBAL_CLOCK_ENABLE = "global_clock_enable"
    NON_INTERLEAVED_TRANSACTION = "non_interleaved_transaction"


@dataclass(frozen=True)
class ElasticTimingContract:
    """Transaction timing of a selected elastic region."""

    minimum_unstalled_latency: int
    ii_no_stall: int
    capacity: int
    variable_wall_clock_latency: bool = True
    stall_policy: ElasticStallPolicy = ElasticStallPolicy.GLOBAL_CLOCK_ENABLE

    def __post_init__(self) -> None:
        if self.minimum_unstalled_latency < 1:
            raise ValueError("elastic timing requires positive advance latency")
        if self.stall_policy is ElasticStallPolicy.GLOBAL_CLOCK_ENABLE:
            if self.ii_no_stall != 1:
                raise ValueError("global-stall elastic pipelines require II=1")
            if self.capacity != self.minimum_unstalled_latency:
                raise ValueError("global-stall elastic capacity must equal latency")
        elif self.stall_policy is ElasticStallPolicy.NON_INTERLEAVED_TRANSACTION:
            if self.capacity != 1:
                raise ValueError("non-interleaved elastic candidates require capacity=1")
        else:  # pragma: no cover - enum validation protects constructed values.
            raise ValueError("unknown elastic stall policy")
        if not self.variable_wall_clock_latency:
            raise ValueError("elastic wall-clock latency must be variable")

@dataclass(frozen=True)
class ElasticPipelinePlan:
    """Physical state owned by one selected globally-stalled candidate."""

    selected_candidate: str
    latency: int
    data_stage_instances: tuple[tuple[int, int], ...]
    valid_stage_count: int
    ready_control_lut_estimate: int = 2
    stall_policy: ElasticStallPolicy = ElasticStallPolicy.GLOBAL_CLOCK_ENABLE

    def __post_init__(self) -> None:
        if self.latency < 1 or self.valid_stage_count != self.latency:
            raise ValueError("elastic plan valid stages must equal positive latency")
        if self.data_stage_instances != tuple(sorted(self.data_stage_instances)):
            raise ValueError("elastic plan data-stage instances must be ordered")
        if any(
            type(instance) is not int or instance < 0
            for instance, _ in self.data_stage_instances
        ):
            raise ValueError(
                "elastic plan data-stage identities must be non-negative integers"
            )
        instances = tuple(instance for instance, _ in self.data_stage_instances)
        if len(instances) != len(set(instances)):
            raise ValueError("elastic plan data-stage instances must be unique")
        if any(stages < 1 for _, stages in self.data_stage_instances):
            raise ValueError("elastic plan data-stage depths must be positive")
        if self.ready_control_lut_estimate < 1:
            raise ValueError("elastic ready/control estimate must be positive")


@dataclass(frozen=True)
class ElasticPipelineRegion:
    """One source-to-sink ready/valid transform and its frozen selected plan."""

    semantic_id: str
    source_endpoint: str
    destination_endpoint: str
    input_type: HardwareType
    output_type: HardwareType
    source_expression: Expression
    constraints: tuple[PipelineConstraint, ...]
    candidates: tuple[PipelineCandidate, ...]
    selected: str
    plan: ElasticPipelinePlan | None
    timing: ElasticTimingContract
    clock: str
    reset: str
    source_origin: SourceOrigin | None = field(default=None, compare=False)
    # formal-aware selection route evidence is orchestration metadata, never elastic semantics.
    formal_records: tuple[object, ...] = field(default=(), compare=False)
    # A non-None temporal graph is the sole authority for a transactional
    # candidate.  Legacy globally-stalled elastic plans stay physically and
    # byte-identically represented by ``plan``.
    temporal_graph: TemporalImplementationGraph | None = field(default=None)

    def __post_init__(self) -> None:
        validate_elastic_region_metadata(self)
        if self.source_expression.type != self.output_type:
            raise ValueError("elastic source expression has wrong result type")
        if any(candidate.expression.type != self.output_type for candidate in self.candidates):
            raise ValueError("elastic candidate expression has wrong result type")

        if self.temporal_graph is not None:
            self._validate_temporal()
            return

        # The plan is sealed against the already typed selected expression.  A
        # backend must never be able to reinterpret a stale/corrupt list of
        # physical stage identities after canonical restoration.
        from zlang.ir import expressions as expr
        from zlang.ir.traversal import (
            ExpressionTraversalPolicy,
            walk_expression,
        )
        from zlang.timing import timing_info

        selected = self.selected_candidate
        nodes = tuple(
            walk_expression(
                selected.expression,
                policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
            )
        )
        if any(isinstance(node, expr.Delay) for node in nodes):
            raise ValueError("elastic selected expression cannot contain Delay state")
        stages = tuple(sorted(
            (node.instance, node.stages)
            for node in nodes
            if isinstance(node, expr.Pipeline)
        ))
        assert self.plan is not None
        if not stages or stages != self.plan.data_stage_instances:
            raise ValueError(
                "elastic selected expression data stages disagree with its plan"
            )
        if timing_info(self.source_expression).latency != 0:
            raise ValueError("elastic source expression must be zero-latency")
        if timing_info(selected.expression).latency != selected.latency:
            raise ValueError(
                "elastic selected expression latency disagrees with its candidate"
            )

    def _validate_temporal(self) -> None:
        """Seal the bounded capacity-one transaction wrapper contract."""

        assert self.temporal_graph is not None
        graph = self.temporal_graph
        selected = self.selected_candidate
        if self.plan is not None:
            raise ValueError("temporal elastic candidates must not carry a legacy stage plan")
        if graph.temporal_class is not TemporalClass.FLOW_CONTROLLED:
            raise ValueError("elastic temporal candidates require flow-controlled admission")
        if graph.capacity != 1:
            raise ValueError("initial temporal elastic candidates require capacity=1")
        if graph.semantic_region_identity != self.semantic_id:
            raise ValueError("temporal graph semantic identity disagrees with elastic region")
        if selected.expression != self.source_expression:
            raise ValueError("temporal candidates must retain the exact semantic expression")
        if selected.latency != graph.latency or selected.initiation_interval != graph.initiation_interval:
            raise ValueError("temporal candidate timing disagrees with temporal graph")
        if self.timing.minimum_unstalled_latency != graph.latency:
            raise ValueError("temporal timing latency disagrees with temporal graph")
        if self.timing.ii_no_stall != graph.initiation_interval:
            raise ValueError("temporal timing II disagrees with temporal graph")
        if self.timing.capacity != graph.capacity:
            raise ValueError("temporal timing capacity disagrees with temporal graph")
        if self.timing.stall_policy is not ElasticStallPolicy.NON_INTERLEAVED_TRANSACTION:
            raise ValueError("temporal candidate uses the wrong elastic stall policy")

    @property
    def selected_candidate(self) -> PipelineCandidate:
        return next(item for item in self.candidates if item.name == self.selected)


def validate_elastic_region_metadata(region: object) -> None:
    """Validate metadata shared by typed and canonical elastic regions.

    Canonical candidates carry node IDs rather than typed expressions, so graph
    checks remain in ``CanonicalModule``.  Everything else is validated here
    and again on restoration through ``ElasticPipelineRegion``.
    """

    semantic_id = getattr(region, "semantic_id")
    source_endpoint = getattr(region, "source_endpoint")
    destination_endpoint = getattr(region, "destination_endpoint")
    if not semantic_id or not source_endpoint or not destination_endpoint:
        raise ValueError("elastic pipeline identity and endpoints must be non-empty")
    if source_endpoint == destination_endpoint:
        raise ValueError("elastic source and destination endpoints must be distinct")
    candidates = tuple(getattr(region, "candidates"))
    names = tuple(candidate.name for candidate in candidates)
    if not names or len(names) != len(set(names)):
        raise ValueError("elastic candidate names must be non-empty and unique")
    selected_name = getattr(region, "selected")
    if selected_name not in names:
        raise ValueError("elastic selected candidate is absent")
    plan = getattr(region, "plan")
    timing = getattr(region, "timing")
    temporal_graph = getattr(region, "temporal_graph", None)
    if temporal_graph is None:
        if plan is None or plan.selected_candidate != selected_name:
            raise ValueError("elastic plan candidate does not match selection")
    elif plan is not None:
        raise ValueError("temporal elastic candidate cannot carry a legacy plan")
    selected = next(item for item in candidates if item.name == selected_name)
    if selected.violations:
        raise ValueError("elastic selected candidate is not statically legal")
    if any(candidate.latency < 1 for candidate in candidates):
        raise ValueError("elastic candidates require positive latency")
    if temporal_graph is None and any(
        candidate.initiation_interval != 1 for candidate in candidates
    ):
        raise ValueError("globally-stalled elastic candidates require II=1")
    if selected.latency != timing.minimum_unstalled_latency:
        raise ValueError("elastic selected latency disagrees with timing")
    if selected.initiation_interval != timing.ii_no_stall:
        raise ValueError("elastic selected II disagrees with timing")
    if temporal_graph is None:
        assert plan is not None
        if plan.latency != selected.latency or plan.valid_stage_count != timing.capacity:
            raise ValueError("elastic plan latency/capacity disagrees with selection")
        if plan.stall_policy is not timing.stall_policy:
            raise ValueError("elastic plan and timing stall policies disagree")
        if selected.pipeline_plan.inserted_registers != selected.latency:
            raise ValueError("elastic selected register plan disagrees with latency")
    elif temporal_graph.implementation_identity == "":
        raise ValueError("temporal graph implementation identity must be present")


__all__ = [
    "ElasticPipelinePlan",
    "ElasticPipelineRegion",
    "ElasticStallPolicy",
    "ElasticTimingContract",
    "validate_elastic_region_metadata",
]
