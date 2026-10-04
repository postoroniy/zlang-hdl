# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Verification instrumentation overlay for native simulation plans."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zlang.ir.verification import VerificationGoalKind
from zlang.opt.ir import ExpressionOp, TargetKind
from zlang.simulation_expression_plan import (
    native_expression_attributes,
    validate_native_expression_node,
)
from zlang.simulation_plan_encoding import (
    expression_node_payload as _expression_node_payload,
    origin_payload as _origin_payload,
)
from zlang.simulation_plan_policy import SimulationPlanError

@dataclass(frozen=True)
class VerificationOverlayProduct:
    events: list[dict[str, Any]]
    instrumentation_scopes: list[dict[str, Any]]


@dataclass
class VerificationOverlayBuilder:
    """Append executable verification nodes and own their instrumentation."""

    _canonical: object
    _nodes: list[dict[str, Any]]
    _max_events: int
    _max_nodes: int
    _max_width: int
    _events: list[dict[str, Any]] = field(default_factory=list, init=False)

    def _instrumentation_event(
        self,
        *,
        category: str,
        scope: object,
        clause: object,
        goal_kind: str | None = None,
    ) -> int:
        identifier = len(self._events)
        if identifier >= self._max_events:
            raise SimulationPlanError(
                f"simulation plan exceeds {self._max_events} instrumentation events"
            )
        metadata: dict[str, Any] = {
            "category": category,
            "scope_id": scope.semantic_id,
            "scope_name": scope.name,
            "clause_id": clause.semantic_id,
            "clause_name": clause.name,
            "hierarchy_path": [self._canonical.name],
            "source_origin": _origin_payload(clause.source_origin),
        }
        if goal_kind is not None:
            metadata["goal_kind"] = goal_kind
        self._events.append({"id": identifier, "metadata": metadata})
        return identifier

    def _append_expressions(self) -> dict[int, int]:
        canonical = self._canonical
        nodes = self._nodes
        if len(nodes) + len(canonical.verification_expressions) > self._max_nodes:
            raise SimulationPlanError(
                f"simulation plan exceeds {self._max_nodes} expression nodes"
            )
        output_expression_by_name = {
            assignment.target_name: assignment.expression
            for assignment in canonical.assignments
            if assignment.target_kind is TargetKind.PORT
        }
        verification_node_ids: dict[int, int] = {}
        for expected, node in enumerate(canonical.verification_expressions):
            if node.id != expected:
                raise SimulationPlanError(
                    "canonical verification expression IDs are not contiguous"
                )
            validate_native_expression_node(
                node,
                self._max_width,
                verification=True,
            )
            attributes = native_expression_attributes(node, verification=True)
            if (
                node.op is ExpressionOp.INPUT
                and attributes.get("name") in output_expression_by_name
            ):
                verification_node_ids[node.id] = output_expression_by_name[
                    attributes["name"]
                ]
                continue
            identifier = len(nodes)
            verification_node_ids[node.id] = identifier
            nodes.append(
                _expression_node_payload(
                    node,
                    identifier=identifier,
                    op=node.op.value,
                    operands=[verification_node_ids[item] for item in node.operands],
                    attributes=attributes,
                )
            )
        return verification_node_ids

    def build(self) -> VerificationOverlayProduct:
        canonical = self._canonical
        verification_node_ids = self._append_expressions()
        instrumentation_scopes: list[dict[str, Any]] = []
        for scope in canonical.verification_scopes:
            requirements = [
                {
                    "node": verification_node_ids[requirement.expression],
                    "event": self._instrumentation_event(
                        category="requirement_violation",
                        scope=scope,
                        clause=requirement,
                    ),
                }
                for requirement in scope.requirements
            ]
            goals = []
            for goal in scope.goals:
                category = (
                    "runtime_violation"
                    if scope.semantic_id.startswith("$zlang_runtime_protocol:")
                    else (
                        "cover_witness"
                        if goal.kind is VerificationGoalKind.COVER
                        else "assertion_failure"
                    )
                )
                goals.append(
                    {
                        "node": verification_node_ids[goal.expression],
                        "event": self._instrumentation_event(
                            category=category,
                            scope=scope,
                            clause=goal,
                            goal_kind=(
                                None
                                if category == "runtime_violation"
                                else goal.kind.value
                            ),
                        ),
                        "kind": goal.kind.value,
                    }
                )
            instrumentation_scopes.append(
                {
                    "clock": scope.clock,
                    "reset": scope.reset,
                    "requirements": requirements,
                    "goals": goals,
                }
            )
        return VerificationOverlayProduct(self._events, instrumentation_scopes)
