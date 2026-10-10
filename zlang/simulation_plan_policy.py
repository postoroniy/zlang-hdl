# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Versioned native-plan schema and immutable resource bounds."""

from __future__ import annotations

from dataclasses import dataclass

SIMULATION_PLAN_SCHEMA = "zlang-simulation-plan-v12"
SIMULATION_RUNTIME_ABI = "zlang-native-simulation-abi-v12"
CRANELIFT_VERSION = "0.135.2"
MAX_PLAN_BYTES = 16_777_216
MAX_PLAN_NODES = 32_768
MAX_PLAN_WIDTH = 8192
MAX_PLAN_ARITHMETIC_WIDTH = 512
MAX_PLAN_MEMORY_WIDTH = MAX_PLAN_WIDTH
MAX_PLAN_LIMB_WORK = 32768
MAX_PLAN_MEMORY_BITS = 16_777_216
MAX_PLAN_EVENTS = 4_096
MAX_PLAN_REGION_DEPTH = 8
MAX_PLAN_REGION_ITERATIONS = 1_000_000
MAX_PLAN_DYNAMIC_NODE_WORK = 8_000_000


@dataclass(frozen=True)
class SimulationPlanPolicy:
    """One immutable set of schema identities and validation bounds."""

    cranelift_version: str = CRANELIFT_VERSION
    max_arithmetic_width: int = MAX_PLAN_ARITHMETIC_WIDTH
    max_bytes: int = MAX_PLAN_BYTES
    max_dynamic_node_work: int = MAX_PLAN_DYNAMIC_NODE_WORK
    max_events: int = MAX_PLAN_EVENTS
    max_limb_work: int = MAX_PLAN_LIMB_WORK
    max_memory_bits: int = MAX_PLAN_MEMORY_BITS
    max_memory_width: int = MAX_PLAN_MEMORY_WIDTH
    max_nodes: int = MAX_PLAN_NODES
    max_region_depth: int = MAX_PLAN_REGION_DEPTH
    max_region_iterations: int = MAX_PLAN_REGION_ITERATIONS
    max_width: int = MAX_PLAN_WIDTH
    schema: str = SIMULATION_PLAN_SCHEMA
    runtime_abi: str = SIMULATION_RUNTIME_ABI


DEFAULT_SIMULATION_PLAN_POLICY = SimulationPlanPolicy()


class SimulationPlanError(ValueError):
    """A module cannot be represented by the native simulation plan."""


class JitUnsupportedFeatureError(SimulationPlanError):
    """The current native runtime does not implement an exact module feature."""
