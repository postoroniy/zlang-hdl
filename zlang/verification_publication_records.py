# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Owned construction of one published formal goal and its executable job."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ir.cdc import ClockDomain
from zlang.ir.comparison_window import ComparisonWindow
from zlang.ir import formal_planning
from zlang.source import SourceOrigin
from zlang.verification_bundle_codec import VerificationJob


@dataclass(frozen=True)
class GoalPublication:
    """Immutable fields shared by one plan record and one bundle job.

    The formal planner and bundle codec remain the validation authorities.
    This owner only prevents publication orchestration from spelling the same
    semantic identity, domain, assumptions and physical path independently in
    two records.
    """

    identity: str
    kind: formal_planning.FormalPlanGoalKind
    job_kind: str
    top: str
    clock_domain: str | None
    reset_domain: str | None
    assumption_ids: tuple[str, ...]
    required_observations: tuple[str, ...]
    selected_ir_identity: str
    source_origin: SourceOrigin | None
    scope_id: str | None
    physical_instance_path: tuple[str, ...]
    clock_domain_contract: ClockDomain

    def plan(
        self,
        *,
        physical_domain_identity: str | None,
        route: formal_planning.FormalExecutableRoute | None = None,
        skip_reason: formal_planning.FormalSkipReason | None = None,
    ) -> formal_planning.FormalGoalPlan:
        return formal_planning.FormalGoalPlan(
            self.identity,
            self.identity,
            self.kind,
            self.clock_domain,
            self.reset_domain,
            self.assumption_ids,
            self.required_observations,
            self.selected_ir_identity,
            ComparisonWindow.same_cycle(),
            1,
            route=route,
            skip_reason=skip_reason,
            source_origin=self.source_origin,
            clock_domain_contract=self.clock_domain_contract,
            physical_domain_identity=physical_domain_identity,
        )

    def job(
        self,
        *,
        physical_domain_identity: str | None,
        source_files: tuple[str, ...] = (),
        source_map_files: tuple[str, ...] = (),
        executable: bool = True,
        reason: str | None = None,
        route: str | None = None,
        backend: str | None = None,
        artifact_hash: str | None = None,
        binding_identity: str | None = None,
    ) -> VerificationJob:
        return VerificationJob(
            self.identity,
            self.job_kind,
            self.top,
            source_files,
            source_map_files=source_map_files,
            executable=executable,
            reason=reason,
            source_origin=self.source_origin,
            route=route,
            backend=backend,
            artifact_hash=artifact_hash,
            binding_identity=binding_identity,
            selected_ir_identity=self.selected_ir_identity,
            scope_id=self.scope_id,
            assumption_ids=self.assumption_ids,
            clock_domain=self.clock_domain,
            reset_domain=self.reset_domain,
            physical_instance_path=self.physical_instance_path,
            clock_domain_contract=self.clock_domain_contract,
            physical_domain_identity=physical_domain_identity,
        )


__all__ = ["GoalPublication"]
