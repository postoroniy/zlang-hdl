# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Immutable bounded-search policy for one implementation request."""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class IntentExplorationLimits:
    """Deterministic count limits shared by profiles and exploration owners."""

    max_candidates: int = 64
    max_value_alternatives: int = 16
    max_structural_alternatives: int = 8
    max_candidates_per_structure: int = 32
    max_saturation_iterations: int = 6
    max_eclasses: int = 4_096
    max_enodes: int = 16_384
    max_architectures: int = 16
    max_reductions: int = 16
    max_pipeline_candidates: int = 32

    def __post_init__(self) -> None:
        values = tuple(getattr(self, item.name) for item in fields(self))
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ValueError("intent exploration limits must be integers")
        if any(value < 1 for value in values):
            raise ValueError("exploration bounds must be positive")
        if any(
            getattr(self, name) > 256
            for name in (
                "max_candidates",
                "max_value_alternatives",
                "max_structural_alternatives",
                "max_candidates_per_structure",
                "max_saturation_iterations",
                "max_architectures",
                "max_reductions",
                "max_pipeline_candidates",
            )
        ):
            raise ValueError(
                "candidate/iteration exploration bounds must not exceed 256"
            )
        if self.max_eclasses > 65_536 or self.max_enodes > 262_144:
            raise ValueError("e-graph exploration bounds exceed the hard safety ceiling")

    def to_data(self) -> dict[str, int]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


__all__ = ["IntentExplorationLimits"]
