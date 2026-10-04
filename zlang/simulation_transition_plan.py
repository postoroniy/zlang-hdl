# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-resolved scheduler transition plan construction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from zlang.ir import state as ir_state
from zlang.simulation_plan_policy import JitUnsupportedFeatureError


@dataclass(frozen=True)
class TransitionPlanProduct:
    transitions: list[dict[str, Any]]
    scheduler_fifos: list[str]
    scheduler_guards: list[str]
    scheduler_activations: list[int]


@dataclass
class TransitionPlanBuilder:
    """Encode the compiler-resolved scheduler without rediscovering policy."""

    _canonical: object

    def build(self) -> TransitionPlanProduct:
        canonical = self._canonical
        transition = canonical.resolved_transition
        if transition is None:
            return TransitionPlanProduct([], [], [], [])

        resources = {item.semantic_id: item for item in transition.resources}
        ordered = ir_state.ordered_groups(transition)
        scheduler_fifos = [
            item.name
            for item in transition.resources
            if item.kind is ir_state.StateResourceKind.FIFO
        ]
        scheduler_guards = [item.rule_name for item in ordered]
        scheduler_activations = list(
            ir_state.conditional_activation_predicates(transition)
        )
        scheduler_regions = ir_state.selection_regions_for_transition(transition)
        transitions: list[dict[str, Any]] = []
        for group in ordered:
            actions = []
            for action in group.actions:
                resource = resources[action.resource_id]
                valid = (
                    action.kind is ir_state.StateActionKind.REGISTER_WRITE
                    and resource.kind is ir_state.StateResourceKind.REGISTER
                    and len(action.operands) == 1
                ) or (
                    action.kind is ir_state.StateActionKind.FIFO_PUSH
                    and resource.kind is ir_state.StateResourceKind.FIFO
                    and len(action.operands) == 1
                ) or (
                    action.kind is ir_state.StateActionKind.FIFO_POP
                    and resource.kind is ir_state.StateResourceKind.FIFO
                    and not action.operands
                ) or (
                    action.kind is ir_state.StateActionKind.MEMORY_READ_REQUEST
                    and resource.kind is ir_state.StateResourceKind.MEMORY
                    and len(action.operands) == 1
                ) or (
                    action.kind is ir_state.StateActionKind.MEMORY_WRITE
                    and resource.kind is ir_state.StateResourceKind.MEMORY
                    and len(action.operands) in {2, 3}
                ) or (
                    action.kind is ir_state.StateActionKind.OUTPUT_WRITE
                    and resource.kind is ir_state.StateResourceKind.OUTPUT
                    and len(action.operands) == 1
                )
                if not valid:
                    raise JitUnsupportedFeatureError(
                        "native simulation encountered an invalid scheduled action"
                    )
                actions.append(
                    {
                        "kind": action.kind.value,
                        "target": resource.name,
                        "node": action.operands[0] if action.operands else None,
                        "operands": list(action.operands),
                        "activation": action.activation,
                    }
                )
            transitions.append(
                {
                    "name": group.rule_name,
                    "domain": group.domain or transition.domain or canonical.clock,
                    "guard": group.guard,
                    "selection_regions": [
                        [
                            value.value if isinstance(value, Enum) else value
                            for value in region
                        ]
                        for region in scheduler_regions[group.rule_name]
                    ],
                    "actions": actions,
                }
            )
        return TransitionPlanProduct(
            transitions,
            scheduler_fifos,
            scheduler_guards,
            scheduler_activations,
        )
