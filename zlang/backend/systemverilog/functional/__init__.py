# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned planning products for compact functional RTL regions."""

from zlang.backend.systemverilog.functional.model import (
    FunctionalExpressionContext,
    FunctionalReductionEmissionPlan,
    FunctionalRegionCompositionNode,
    FunctionalRegionCompositionPlan,
    FunctionalRegionEmissionPlan,
    FunctionalScatterDimension,
    FunctionalScatterEmissionPlan,
    FunctionalScatterEntry,
)
from zlang.backend.systemverilog.functional.planning import FunctionalRegionPlanner

__all__ = [
    "FunctionalExpressionContext",
    "FunctionalReductionEmissionPlan",
    "FunctionalRegionCompositionNode",
    "FunctionalRegionCompositionPlan",
    "FunctionalRegionEmissionPlan",
    "FunctionalRegionPlanner",
    "FunctionalScatterDimension",
    "FunctionalScatterEmissionPlan",
    "FunctionalScatterEntry",
]
