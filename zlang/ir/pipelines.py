"""Typed architectural records for bounded automatic pipeline exploration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from zlang.ir.target import TimingDAG
from zlang.ir.scheduled import ScheduledValueGraph
from zlang.ir.types import HardwareType
from zlang.source import SourceOrigin

if TYPE_CHECKING:
    from zlang.ir.expressions import Expression


class PipelineMetric(str, Enum):
    LATENCY = "latency"
    THROUGHPUT = "throughput"
    DSP = "dsp"
    FMAX = "fmax"


class PipelineRelation(str, Enum):
    MAXIMUM = "<="
    EXACT = "=="
    MINIMUM = ">="


@dataclass(frozen=True)
class PipelineConstraint:
    metric: PipelineMetric
    relation: PipelineRelation
    value: int

    def render(self) -> str:
        return f"{self.metric.value}{self.relation.value}{self.value}"


class PipelineTree(str, Enum):
    LINEAR = "linear"
    BALANCED = "balanced"
    DAG = "dag"


class RegisterPlacement(str, Enum):
    OUTPUT = "output"
    BALANCED_LEVELS = "balanced_levels"
    SCHEDULED_DAG = "scheduled_dag"


class MultiplierMapping(str, Enum):
    LOGIC = "logic"
    DSP = "dsp"


class PipelineCostSource(str, Enum):
    # Compatibility value used by the accepted sum-of-products estimator.
    ESTIMATE = "estimate"
    STRUCTURAL_ESTIMATE = "structural_estimate"
    TARGET_ESTIMATE = "target_estimate"
    MEASURED = "measured"


@dataclass(frozen=True)
class ScheduledOperationCost:
    """One honestly labelled cost used by the pure DAG scheduler."""

    delay_ps: int
    lut: int
    dsp: int
    resource_class: str
    source: PipelineCostSource
    target_primitive: str | None = None

    def __post_init__(self) -> None:
        if self.delay_ps < 0 or self.lut < 0 or self.dsp < 0:
            raise ValueError("scheduled operation costs must not be negative")
        if not self.resource_class:
            raise ValueError("scheduled operation resource class must be named")


@dataclass(frozen=True)
class ScheduledPipelineOperation:
    """Stable typed operation placement within one exact pipeline contract."""

    identity: str
    semantic_identity: str
    ordinal: int
    operation: str
    stage: int
    operand_identities: tuple[str, ...]
    operand_types: tuple[HardwareType, ...]
    result_type: HardwareType
    cost: ScheduledOperationCost
    source_origin: SourceOrigin | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.identity or not self.semantic_identity or not self.operation:
            raise ValueError("scheduled operation identity and kind must be non-empty")
        if self.ordinal < 0 or self.stage < 0:
            raise ValueError("scheduled operation ordinal/stage must not be negative")
        if len(self.operand_identities) != len(self.operand_types):
            raise ValueError("scheduled pipeline operation operand metadata is incomplete")


@dataclass(frozen=True)
class ScheduledPipelineStage:
    index: int
    operation_identities: tuple[str, ...]
    estimated_delay_ps: int

    def __post_init__(self) -> None:
        if self.index < 0 or self.estimated_delay_ps < 0:
            raise ValueError("scheduled pipeline stage values must not be negative")
        if len(self.operation_identities) != len(set(self.operation_identities)):
            raise ValueError("scheduled pipeline stage repeats an operation")


@dataclass(frozen=True)
class PipelinePlan:
    """Backend-independent register/stage plan for one timed candidate."""
    stage_boundaries: tuple[str, ...]
    inserted_registers: int
    alignment_delays: tuple[int, ...] = ()
    scheduler: str = "legacy"
    requested_latency: int | None = None
    initiation_interval: int = 1
    cost_source: PipelineCostSource = PipelineCostSource.ESTIMATE
    operations: tuple[ScheduledPipelineOperation, ...] = ()
    stages: tuple[ScheduledPipelineStage, ...] = ()
    timing_dag: TimingDAG | None = None
    source_expression_identity: str | None = None
    scheduled_expression_identity: str | None = None
    timed_equivalence: str = "not_checked"
    scheduled_value_graph: ScheduledValueGraph | None = None
    selected_value_identity: str | None = None
    rewrite_certificate: tuple[str, ...] = ()
    # The semantic source is retained only so M36 can compare the final
    # physical candidate against the expression written by the user.  It is
    # intentionally excluded from dataclass equality/repr; the stable source
    # identity above is authoritative for caches and manifests.
    source_expression: Expression | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.inserted_registers < 0:
            raise ValueError("pipeline plan register count must not be negative")
        if self.initiation_interval < 1:
            raise ValueError("pipeline plan initiation interval must be positive")
        if self.scheduler == "legacy":
            return
        if self.requested_latency is None or self.requested_latency < 1:
            raise ValueError("scheduled pipeline plan requires positive exact latency")
        if len(self.stages) != self.requested_latency:
            raise ValueError("scheduled pipeline plan must describe every visible stage")
        if tuple(item.index for item in self.stages) != tuple(
            range(self.requested_latency)
        ):
            raise ValueError("scheduled pipeline stages must be dense and ordered")
        operation_ids = tuple(item.identity for item in self.operations)
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("scheduled pipeline operation identities must be unique")
        known = set(operation_ids)
        staged_operation_ids = tuple(
            identity
            for stage in self.stages
            for identity in stage.operation_identities
        )
        if len(staged_operation_ids) != len(set(staged_operation_ids)):
            raise ValueError("scheduled operation appears in more than one stage")
        if set(staged_operation_ids) != known:
            raise ValueError("scheduled stages do not cover every operation exactly once")
        for stage in self.stages:
            if not set(stage.operation_identities) <= known:
                raise ValueError("scheduled stage references an unknown operation")
        if any(item.stage >= self.requested_latency for item in self.operations):
            raise ValueError("scheduled operation is outside the visible pipeline")
        if self.timing_dag is None:
            raise ValueError("scheduled pipeline plan requires a timing DAG")
        if self.timing_dag.output_latency != self.requested_latency:
            raise ValueError("scheduled timing DAG latency does not match the contract")
        if not self.source_expression_identity or not self.scheduled_expression_identity:
            raise ValueError("scheduled pipeline plan requires source/scheduled identities")
        if not self.selected_value_identity:
            raise ValueError("scheduled pipeline plan requires a selected value identity")
        if self.timed_equivalence != "verified":
            raise ValueError("scheduled pipeline plan must be timed-equivalence verified")
        if self.scheduled_value_graph is None:
            raise ValueError("scheduled pipeline plan requires one shared scheduled value graph")
        graph = self.scheduled_value_graph
        if graph.exact_latency != self.requested_latency:
            raise ValueError("scheduled value graph latency does not match pipeline plan")
        if graph.initiation_interval != self.initiation_interval:
            raise ValueError("scheduled value graph II does not match pipeline plan")
        if graph.source_expression_identity != self.source_expression_identity:
            raise ValueError("scheduled value graph source does not match pipeline plan")
        if graph.rewrite_certificate != self.rewrite_certificate:
            raise ValueError("scheduled value graph rewrite certificate does not match pipeline plan")
        if set(graph.operation_identities) != set(operation_ids):
            raise ValueError("scheduled value graph operations do not match pipeline plan")


@dataclass(frozen=True)
class PipelineEstimate:
    lut: int
    ff: int
    dsp: int
    fmax_mhz: int


@dataclass(frozen=True)
class PipelineCandidate:
    name: str
    expression: Expression
    tree: PipelineTree
    register_placement: RegisterPlacement
    multiplier_mapping: MultiplierMapping
    transformations: tuple[str, ...]
    latency: int
    initiation_interval: int
    estimate: PipelineEstimate
    cost_source: PipelineCostSource = PipelineCostSource.ESTIMATE
    violations: tuple[str, ...] = ()
    pipeline_plan: PipelinePlan = PipelinePlan((), 0)

    @property
    def throughput(self) -> int:
        return self.initiation_interval

    @property
    def legal(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class PipelineExploration:
    output: str
    result_type: HardwareType
    source_expression: Expression
    constraints: tuple[PipelineConstraint, ...]
    candidates: tuple[PipelineCandidate, ...]
    selected: str
    search_bound: int
    # M39 execution evidence is orchestration metadata, not pipeline semantics.
    # Excluding it from equality keeps canonical IR/proof identity independent
    # from cache hits, tool availability, and solver runtime.
    formal_records: tuple[object, ...] = field(default=(), compare=False)

    @property
    def selected_candidate(self) -> PipelineCandidate:
        return next(
            candidate for candidate in self.candidates
            if candidate.name == self.selected
        )
