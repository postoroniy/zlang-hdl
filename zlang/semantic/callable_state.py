# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compilation-local mutable state for callable specialization.

This module is intentionally independent of expression analysis and callable
resolution.  It is the single owner of retained definitions, use counts,
recursion markers, and specialization budget costs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from zlang.ir import callables as ir_callables
from zlang.ir import module as ir_module

from .errors import SemanticError


@dataclass(frozen=True)
class CallableSpecializationSnapshot:
    """Recoverable specialization state used by speculative semantic paths."""

    generic_specializations: tuple[ir_module.GenericSpecialization, ...]
    callable_definitions: tuple[tuple[str, ir_module.Function], ...]
    callable_use_counts: tuple[tuple[str, int], ...]
    specializations_in_progress: frozenset[str]
    specialization_budget_costs: tuple[tuple[str, tuple[int, int]], ...]


@dataclass
class CallableSpecializationCache:
    """Single owner of callable definitions, identities, uses, and costs."""

    generic_specializations: list[ir_module.GenericSpecialization] = field(
        default_factory=list
    )
    function_definitions: dict[str, ir_module.Function] = field(default_factory=dict)
    callable_definitions: dict[str, ir_module.Function] = field(default_factory=dict)
    callable_use_counts: dict[str, int] = field(default_factory=dict)
    specializations_in_progress: set[str] = field(default_factory=set)
    specialization_budget_costs: dict[str, tuple[int, int]] = field(
        default_factory=dict
    )

    def record_use(self, identity: str) -> None:
        self.callable_use_counts[identity] = (
            self.callable_use_counts.get(identity, 0) + 1
        )

    def definition(self, identity: str) -> ir_module.Function | None:
        """Return one retained concrete definition without exposing mutation."""

        return self.callable_definitions.get(identity)

    def is_specializing(self, identity: str) -> bool:
        """Return whether exact specialization of the identity is active."""

        return identity in self.specializations_in_progress

    @contextmanager
    def specializing(self, identity: str) -> Iterator[None]:
        """Own the balanced lifetime of one in-progress specialization."""

        if identity in self.specializations_in_progress:
            raise SemanticError(
                f"internal duplicate callable specialization scope for '{identity}'"
            )
        self.specializations_in_progress.add(identity)
        try:
            yield
        finally:
            self.specializations_in_progress.discard(identity)

    def publish(
        self,
        identity: str,
        definition: ir_module.Function,
        record: ir_module.GenericSpecialization,
        *,
        budget_cost: tuple[int, int] | None,
    ) -> None:
        """Atomically retain one completed specialization and its metadata."""

        previous = self.callable_definitions.get(identity)
        if previous is not None and previous != definition:
            raise SemanticError(
                f"conflicting concrete callable definition for '{identity}'"
            )
        self.callable_definitions[identity] = definition
        if budget_cost is not None:
            self.specialization_budget_costs[identity] = budget_cost
        if record not in self.generic_specializations:
            self.generic_specializations.append(record)
        self.record_use(identity)

    def fork(self) -> "CallableSpecializationCache":
        """Copy isolated mutable state for a non-publishing analysis branch."""

        return CallableSpecializationCache(
            generic_specializations=list(self.generic_specializations),
            function_definitions=dict(self.function_definitions),
            callable_definitions=dict(self.callable_definitions),
            callable_use_counts=dict(self.callable_use_counts),
            specializations_in_progress=set(self.specializations_in_progress),
            specialization_budget_costs=dict(self.specialization_budget_costs),
        )

    def snapshot(self) -> CallableSpecializationSnapshot:
        return CallableSpecializationSnapshot(
            tuple(self.generic_specializations),
            tuple(self.callable_definitions.items()),
            tuple(self.callable_use_counts.items()),
            frozenset(self.specializations_in_progress),
            tuple(self.specialization_budget_costs.items()),
        )

    def restore(self, snapshot: CallableSpecializationSnapshot) -> None:
        self.generic_specializations[:] = snapshot.generic_specializations
        self.callable_definitions.clear()
        self.callable_definitions.update(snapshot.callable_definitions)
        self.callable_use_counts.clear()
        self.callable_use_counts.update(snapshot.callable_use_counts)
        self.specializations_in_progress.clear()
        self.specializations_in_progress.update(snapshot.specializations_in_progress)
        self.specialization_budget_costs.clear()
        self.specialization_budget_costs.update(
            snapshot.specialization_budget_costs
        )

    def release_use(self, identity: str) -> None:
        count = self.callable_use_counts.get(identity, 0)
        if count <= 0:
            if identity not in self.callable_definitions:
                return
            raise SemanticError(
                f"internal callable-use accounting underflow for '{identity}'"
            )
        if count > 1:
            self.callable_use_counts[identity] = count - 1
            return

        self.callable_use_counts.pop(identity, None)
        definition = self.callable_definitions.pop(identity, None)
        self.specialization_budget_costs.pop(identity, None)
        self.generic_specializations[:] = (
            record
            for record in self.generic_specializations
            if record.identity != identity
        )
        if definition is None:
            return
        for use in ir_callables.callable_uses(definition.body, deduplicate=False):
            if use.callee_identity is not None:
                self.release_use(use.callee_identity)


__all__ = ["CallableSpecializationCache", "CallableSpecializationSnapshot"]
