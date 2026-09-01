"""Typed records for bounded FIR-like architecture exploration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from zlang.ir.types import HardwareType

if TYPE_CHECKING:
    from zlang.ir.expressions import Expression


class ArchitectureMetric(str, Enum):
    PARALLELISM = "parallelism"
    DEPTH = "depth"
    CANDIDATES = "candidates"


@dataclass(frozen=True)
class ArchitectureConstraint:
    metric: ArchitectureMetric
    maximum: int

    def render(self) -> str:
        return f"{self.metric.value}<={self.maximum}"


class FirArchitectureKind(str, Enum):
    DIRECT = "direct"
    TRANSPOSED = "transposed"
    FOLDED = "folded"


class ArchitectureEquivalence(str, Enum):
    MATHEMATICAL = "mathematical"


@dataclass(frozen=True)
class ArchitectureCandidate:
    name: str
    expression: Expression
    kind: FirArchitectureKind
    parallelism: int
    add_depth: int
    multiplier_count: int
    adder_count: int
    transformations: tuple[str, ...]
    equivalence: ArchitectureEquivalence = ArchitectureEquivalence.MATHEMATICAL
    violations: tuple[str, ...] = ()

    @property
    def legal(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class ArchitectureExploration:
    output: str
    result_type: HardwareType
    source_expression: Expression
    constraints: tuple[ArchitectureConstraint, ...]
    candidates: tuple[ArchitectureCandidate, ...]
    selected: str
    theoretical_candidates: int
    search_bound: int
    budget_pruned: int
    constraint_pruned: int
    # Compiler-owned M39 evidence; never architecture or RTL semantics.
    formal_records: tuple[object, ...] = field(default=(), compare=False)

    @property
    def selected_candidate(self) -> ArchitectureCandidate:
        return next(
            candidate for candidate in self.candidates
            if candidate.name == self.selected
        )
