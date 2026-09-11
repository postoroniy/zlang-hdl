"""Public, backend-neutral products returned by compiler orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field

from zlang.ast.nodes import Module as AstModule
from zlang.compilation_inputs import PhysicalCompilationInputs
from zlang.candidate_sites import CandidateSiteLedger
from zlang.exploration import ExplorationResult
from zlang.formal_artifact_provider import FormalArtifactProvider
from zlang.formal_tooling import FormalToolResolver
from zlang.formal_exploration import FormalExplorationRecord
from zlang.implementation_plans import BackendImplementationPlanningResult
from zlang.implementation_policy import ModuleImplementationPolicy
from zlang.implementation_regions import ImplementationRegion
from zlang.implementation_request import ImplementationRequest
from zlang.ir.formal import FormalDesign
from zlang.ir.module import Module as IrModule
from zlang.opt import CanonicalModule, canonical_ir_identity
from zlang.target_planner import TargetPlanningResult


@dataclass(frozen=True)
class CompilationResult:
    ast: AstModule
    ir: IrModule
    optimization_ir: CanonicalModule
    clash: str
    csr_markdown: str
    csr_json: str
    contracts_sva: str
    implementation_report: str
    cost_report: str
    pipeline_report: str
    architecture_report: str
    high_level_ir: CanonicalModule
    exploration_report: str
    exploration_results: tuple[ExplorationResult, ...]
    formal_design: FormalDesign
    formal_harness: str
    recursive_formal_design: object | None = None
    recursive_formal_harness: str = ""
    target_instance: object | None = None
    implementation_graph: object | None = None
    target_planning_result: TargetPlanningResult | None = None
    target_planner_report: str = ""
    implementation_policy: ModuleImplementationPolicy | None = None
    implementation_request: ImplementationRequest | None = None
    implementation_regions: tuple[ImplementationRegion, ...] = ()
    implementation_policy_report: str = ""
    backend_implementation_plans: BackendImplementationPlanningResult | None = None
    backend_implementation_report: str = ""
    physical_inputs: PhysicalCompilationInputs = field(
        default_factory=PhysicalCompilationInputs,
        compare=False,
    )
    formal_artifact_provider: FormalArtifactProvider | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    formal_tool_resolver: FormalToolResolver | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    candidate_site_ledger: CandidateSiteLedger | None = field(
        default=None,
        compare=False,
    )
    physical_formal_records: tuple[FormalExplorationRecord, ...] = field(
        default=(),
        compare=False,
    )

    @property
    def high_level_ir_identity(self) -> str:
        return canonical_ir_identity(self.high_level_ir)

    @property
    def selected_ir_identity(self) -> str:
        return canonical_ir_identity(self.optimization_ir)


@dataclass(frozen=True)
class SemanticCheckResult:
    """A semantic-only file check and its nonsemantic physical inputs."""

    ast: AstModule
    ir: IrModule
    physical_inputs: PhysicalCompilationInputs = field(
        default_factory=PhysicalCompilationInputs,
        compare=False,
    )


__all__ = ["CompilationResult", "SemanticCheckResult"]
