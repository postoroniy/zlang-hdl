"""Public, backend-neutral products returned by compiler orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from zlang.ast.nodes import Module as AstModule
from zlang.compilation_inputs import PhysicalCompilationInputs
from zlang.candidate_sites import CandidateSiteLedger
from zlang.completion_resolution import CompletionScope
from zlang.definition_resolution import DefinitionResolution, DefinitionTarget
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
from zlang.opt.identity import canonical_ir_identity
from zlang.opt.ir import CanonicalModule
from zlang.target_planner import TargetPlanningResult
from zlang.signature_help_resolution import SignatureHelpCall


_Product = TypeVar("_Product")


@dataclass(frozen=True)
class CompilationProductKey(Generic[_Product]):
    """Typed identity for one node in the compilation demand graph."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("compilation product name must not be empty")


@dataclass(frozen=True)
class CompilationSnapshot:
    """Read-only view of products already computed by one session."""

    source_identity: str
    computed_products: tuple[str, ...]
    syntax: AstModule | None
    semantic: IrModule | None
    selected: IrModule | None
    planned: IrModule | None
    simulation_plan: object | None
    semantic_identity: str | None
    selected_identity: str | None
    planned_identity: str | None


@dataclass(frozen=True)
class AnalysisProduct:
    """Semantic analysis output consumed by later compilation phases."""

    module: IrModule
    exploration_results: tuple[ExplorationResult, ...]
    definition_resolutions: tuple[DefinitionResolution, ...] = ()
    definition_declarations: tuple[DefinitionTarget, ...] = ()
    completion_scopes: tuple[CompletionScope, ...] = ()
    signature_help_calls: tuple[SignatureHelpCall, ...] = ()


@dataclass(frozen=True)
class SelectionProduct:
    """Canonical selection output and its deterministic evidence."""

    module: IrModule
    high_level_ir: CanonicalModule
    optimization_ir: CanonicalModule
    extraction: object
    implementation_policy: ModuleImplementationPolicy
    implementation_request: ImplementationRequest
    exploration_results: tuple[ExplorationResult, ...]
    candidate_site_ledger: CandidateSiteLedger


@dataclass(frozen=True)
class FormalProduct:
    design: FormalDesign
    recursive_design: object | None


@dataclass(frozen=True)
class PlanningProduct:
    module: IrModule
    backend_plans: BackendImplementationPlanningResult
    target_planning_result: TargetPlanningResult | None
    implementation_graph: object | None
    physical_formal_records: tuple[FormalExplorationRecord, ...] = ()


@dataclass(frozen=True)
class DocumentProduct:
    csr_markdown: str
    csr_json: str
    contracts_sva: str


@dataclass(frozen=True)
class ReportProduct:
    implementation: str
    cost: str
    pipeline: str
    architecture: str
    exploration: str


@dataclass(frozen=True)
class CompilationResult:
    ast: AstModule
    ir: IrModule
    optimization_ir: CanonicalModule
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
    recursive_formal_design: object | None = None
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
    definition_resolutions: tuple[DefinitionResolution, ...] = field(
        default=(),
        compare=False,
        repr=False,
    )
    definition_declarations: tuple[DefinitionTarget, ...] = field(
        default=(),
        compare=False,
        repr=False,
    )
    completion_scopes: tuple[CompletionScope, ...] = field(
        default=(),
        compare=False,
        repr=False,
    )
    signature_help_calls: tuple[SignatureHelpCall, ...] = field(
        default=(),
        compare=False,
        repr=False,
    )


__all__ = [
    "AnalysisProduct",
    "CompilationResult",
    "CompilationProductKey",
    "CompilationSnapshot",
    "DocumentProduct",
    "FormalProduct",
    "PlanningProduct",
    "ReportProduct",
    "SelectionProduct",
    "SemanticCheckResult",
]
