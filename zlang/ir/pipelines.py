"""Typed architectural records for bounded automatic pipeline exploration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from zlang.ir.types import HardwareType

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


class RegisterPlacement(str, Enum):
    OUTPUT = "output"
    BALANCED_LEVELS = "balanced_levels"


class MultiplierMapping(str, Enum):
    LOGIC = "logic"
    DSP = "dsp"


class PipelineCostSource(str, Enum):
    ESTIMATE = "estimate"


@dataclass(frozen=True)
class PipelinePlan:
    """Backend-independent register/stage plan for one timed candidate."""
    stage_boundaries: tuple[str, ...]
    inserted_registers: int
    alignment_delays: tuple[int, ...] = ()


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
