# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Immutable semantic elaboration budgets shared by bounded analyzers."""

FUNCTIONAL_RANGE = 4096
TOTAL_GENERATED = 65536
MAX_LOCAL_EXPANSION_NODES = 500_000
MAX_ANALYSIS_LOCAL_EXPANSION_NODES = 4_000_000
FUNCTIONAL_REGION_THRESHOLD = 32
COMPILE_TIME_CALLS = 64
COMPILE_TIME_OPERATIONS = 1_000_000
COMPILE_TIME_EVALUATOR_SCHEMA = "zlang-ct-v2"
REAL_INTRINSICS = frozenset({"pi", "sin", "cos", "log2", "log", "exp", "sqrt"})
