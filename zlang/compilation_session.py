"""Demand-driven orchestration for one isolated ZLang compilation.

The session owns every computed value and failure.  It deliberately contains
no process-global cache: two sessions compiling identical source are still
independent compilation attempts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Callable, Iterable, Mapping, TypeVar, cast

from zlang.analysis_needs import AnalysisNeeds
from zlang.ast.nodes import Module as AstModule
from zlang import costs as costs
from zlang import candidate_sites as candidate_sites
from zlang.compilation_inputs import PhysicalCompilationInputs
from zlang import compilation_selection
from zlang import compilation_outputs
from zlang.compilation_selection import (
    inline_locals,
)
from zlang.compilation_products import (
    AnalysisProduct as _AnalysisProduct,
    CompilationResult,
    CompilationProductKey,
    CompilationSnapshot,
    DocumentProduct as _DocumentProduct,
    FormalProduct as _FormalProduct,
    PlanningProduct as _PlanningProduct,
    ReportProduct as _ReportProduct,
    SelectionProduct as _SelectionProduct,
)
from zlang.intent_structural_exploration import IntentStructuralExplorationCache
from zlang.completion_resolution import CompletionScope
from zlang.exploration import ExplorationResult
from zlang.formal_artifact_provider import FormalArtifactProvider
from zlang.formal_tooling import FormalToolResolver
from zlang import formal_exploration as formal_exploration
from zlang import implementation_plans as implementation_plans
from zlang import implementation_policy as implementation_policy_api
from zlang import implementation_request as implementation_request_api
from zlang.ir.module import Module as IrModule, dependency_context_identity
from zlang.opt.identity import canonical_ir_identity
from zlang.opt.ir import CanonicalModule, OptimizationStage
from zlang.opt.module_lowering import lower
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.definition_resolution import DefinitionResolution, DefinitionTarget
from zlang.target_catalog import ArchitectureSelectionMode
from zlang.stdlib import track_resolved_stdlib_source_paths
from zlang.signature_help_resolution import SignatureHelpCall


_Product = TypeVar("_Product")


class SessionTopSelectionError(ValueError):
    """A requested source top does not exist.

    The public compiler facade translates this internal exception to its
    longstanding ``TopSelectionError`` type.
    """


@dataclass(frozen=True)
class CompilationSessionOptions:
    """Immutable snapshot of every option which can affect a session product."""

    formal_policy: formal_exploration.FormalPolicy | str | None
    formal_depth: int
    formal_max_candidates: int
    formal_timeout: int
    formal_cache: Path | None
    formal_work_directory: Path | None
    formal_verifier: object | None
    top: str | None
    target: str | None
    architecture: str | None
    architecture_mode: ArchitectureSelectionMode | str | None
    target_evidence_policy: costs.SourcePolicy | str | None
    target_evidence: tuple[object, ...] | None
    target_evidence_path: Path | None
    target_tool: str
    target_tool_version: str
    target_clock_period_ns: float
    source_unit: str | None
    module_resolver: object | None
    root_module_identity: object | None
    dependency_closure: object | None
    implementation_backend: implementation_request_api.BackendKind | str | None
    implementation_backend_mode: implementation_request_api.RequirementMode | str
    implementation_contributions: tuple[implementation_request_api.ImplementationContribution, ...]
    # Editor/tooling may analyze a project module in its legal child context;
    # production compilation keeps the public top boundary closed by default.
    allow_external_enum_inputs: bool


SYNTAX_PRODUCT = CompilationProductKey[AstModule]("syntax")
SEMANTIC_PRODUCT = CompilationProductKey[_AnalysisProduct]("semantic")
SELECTION_PRODUCT = CompilationProductKey[_SelectionProduct]("selection")
FORMAL_PRODUCT = CompilationProductKey[_FormalProduct]("formal")
PLANNING_PRODUCT = CompilationProductKey[_PlanningProduct]("planning")
SIMULATION_PLAN_PRODUCT = CompilationProductKey[object]("simulation_plan")
TARGET_INSTANCE_PRODUCT = CompilationProductKey[object | None]("target_instance")
DOCUMENT_PRODUCT = CompilationProductKey[_DocumentProduct]("documents")
REPORT_PRODUCT = CompilationProductKey[_ReportProduct]("reports")
MATERIALIZED_PRODUCT = CompilationProductKey[CompilationResult]("materialized")


_TYPED_PRODUCT_DEPENDENCIES: Mapping[
    CompilationProductKey[object], tuple[CompilationProductKey[object], ...]
] = {
    SYNTAX_PRODUCT: (),
    SEMANTIC_PRODUCT: (SYNTAX_PRODUCT,),
    SELECTION_PRODUCT: (SEMANTIC_PRODUCT,),
    FORMAL_PRODUCT: (SELECTION_PRODUCT,),
    PLANNING_PRODUCT: (SELECTION_PRODUCT,),
    SIMULATION_PLAN_PRODUCT: (PLANNING_PRODUCT,),
    TARGET_INSTANCE_PRODUCT: (SELECTION_PRODUCT,),
    DOCUMENT_PRODUCT: (SELECTION_PRODUCT,),
    REPORT_PRODUCT: (SELECTION_PRODUCT, PLANNING_PRODUCT),
    MATERIALIZED_PRODUCT: (
        SYNTAX_PRODUCT,
        SELECTION_PRODUCT,
        FORMAL_PRODUCT,
        PLANNING_PRODUCT,
        TARGET_INSTANCE_PRODUCT,
        DOCUMENT_PRODUCT,
        REPORT_PRODUCT,
    ),
}


# This table documents the stable orchestration DAG.  Conditional reuse (the
# configured semantic product aliases the check product when formal policy is
# off) never adds an undeclared downstream dependency.
COMPILATION_PRODUCT_DEPENDENCIES: Mapping[str, tuple[str, ...]] = {
    key.name: tuple(dependency.name for dependency in dependencies)
    for key, dependencies in _TYPED_PRODUCT_DEPENDENCIES.items()
}


class CompilationSession:
    """One lazy, deterministic compilation dependency graph.

    ``semantic_ir`` and :meth:`check` intentionally use formal policy ``off``.
    They validate source semantics without executing formal candidate gates or
    constructing selected/backend/report products.  :meth:`materialize` uses
    the configured policy and reproduces the eager ``compile_source`` facade.
    """

    def __init__(
        self,
        source: str,
        *,
        formal_policy: formal_exploration.FormalPolicy | str | None = None,
        formal_depth: int = 32,
        formal_max_candidates: int = 8,
        formal_timeout: int = 120,
        formal_cache: Path | str | None = None,
        formal_work_directory: Path | str | None = None,
        formal_verifier=None,
        top: str | None = None,
        target: str | None = None,
        architecture: str | None = None,
        architecture_mode: ArchitectureSelectionMode | str | None = None,
        target_evidence_policy: costs.SourcePolicy | str | None = None,
        target_evidence: Iterable[object] | None = None,
        target_evidence_path: Path | str | None = None,
        target_tool: str = "Vivado",
        target_tool_version: str = "2024.2",
        target_clock_period_ns: float = 10.0,
        source_unit: str | None = None,
        module_resolver=None,
        root_module_identity=None,
        dependency_closure=None,
        implementation_backend: implementation_request_api.BackendKind | str | None = None,
        implementation_backend_mode: implementation_request_api.RequirementMode | str = implementation_request_api.RequirementMode.REQUIRED,
        implementation_contributions: tuple[implementation_request_api.ImplementationContribution, ...] = (),
        allow_external_enum_inputs: bool = False,
        physical_inputs: PhysicalCompilationInputs | None = None,
        analysis_needs: AnalysisNeeds = AnalysisNeeds.NONE,
        intent_structural_cache: IntentStructuralExplorationCache | None = None,
    ) -> None:
        if not isinstance(source, str):
            raise TypeError("source must be text")
        self._source = source
        try:
            self._analysis_needs = AnalysisNeeds(analysis_needs)
        except (TypeError, ValueError) as error:
            raise TypeError("analysis_needs must be an AnalysisNeeds value") from error
        self._options = CompilationSessionOptions(
            formal_policy=formal_policy,
            formal_depth=formal_depth,
            formal_max_candidates=formal_max_candidates,
            formal_timeout=formal_timeout,
            formal_cache=None if formal_cache is None else Path(formal_cache),
            formal_work_directory=(
                None
                if formal_work_directory is None
                else Path(formal_work_directory)
            ),
            formal_verifier=formal_verifier,
            top=top,
            target=target,
            architecture=architecture,
            architecture_mode=architecture_mode,
            target_evidence_policy=target_evidence_policy,
            target_evidence=(
                None if target_evidence is None else tuple(target_evidence)
            ),
            target_evidence_path=(
                None if target_evidence_path is None else Path(target_evidence_path)
            ),
            target_tool=target_tool,
            target_tool_version=target_tool_version,
            target_clock_period_ns=target_clock_period_ns,
            source_unit=source_unit,
            module_resolver=module_resolver,
            root_module_identity=root_module_identity,
            dependency_closure=dependency_closure,
            implementation_backend=implementation_backend,
            implementation_backend_mode=implementation_backend_mode,
            implementation_contributions=tuple(implementation_contributions),
            allow_external_enum_inputs=allow_external_enum_inputs,
        )

        self._values: dict[CompilationProductKey[object], object] = {}
        self._failures: dict[CompilationProductKey[object], Exception] = {}
        self._evaluating: set[CompilationProductKey[object]] = set()
        self._lock = RLock()
        if intent_structural_cache is not None and not isinstance(
            intent_structural_cache, IntentStructuralExplorationCache
        ):
            raise TypeError("intent structural cache must be typed")
        self._intent_structural_cache = (
            intent_structural_cache or IntentStructuralExplorationCache()
        )
        self._physical_inputs = physical_inputs or PhysicalCompilationInputs()
        self._formal_artifact_provider = FormalArtifactProvider(
            None
            if self._options.formal_cache is None
            else self._options.formal_cache / "artifacts"
        )
        # Construction is side-effect free.  Host tools are inspected only
        # when a requested formal-aware selection route first needs them.
        self._formal_tool_resolver = FormalToolResolver()

    @property
    def source(self) -> str:
        return self._source

    @property
    def options(self) -> CompilationSessionOptions:
        return self._options

    @property
    def physical_inputs(self) -> PhysicalCompilationInputs:
        """Physical inputs discovered by products demanded so far."""

        return self._physical_inputs

    @property
    def intent_structural_cache(self) -> IntentStructuralExplorationCache:
        """Return the session-owned exact structural exploration cache."""

        return self._intent_structural_cache

    @property
    def formal_artifact_provider(self) -> FormalArtifactProvider:
        """Session-owned memoization for formal preparation recipes."""

        return self._formal_artifact_provider

    @property
    def formal_tool_resolver(self) -> FormalToolResolver:
        """Lazy compiler-session-owned formal/backend discovery."""

        return self._formal_tool_resolver

    def __setattr__(self, name: str, value) -> None:
        if "_options" in self.__dict__ and (
            name in CompilationSessionOptions.__dataclass_fields__
            or name in {
                "source",
                "options",
                "physical_inputs",
                "formal_artifact_provider",
                "formal_tool_resolver",
                "semantic_candidate_site_ledger",
                "candidate_site_ledger",
            }
        ):
            raise AttributeError(f"compilation session option '{name}' is read-only")
        object.__setattr__(self, name, value)

    @property
    def computed_products(self) -> tuple[str, ...]:
        """Products successfully computed so far, in dependency-table order."""

        return tuple(
            key.name for key in _TYPED_PRODUCT_DEPENDENCIES if key in self._values
        )

    @property
    def failed_products(self) -> tuple[str, ...]:
        """Products whose original exception is cached by this session."""

        return tuple(
            key.name for key in _TYPED_PRODUCT_DEPENDENCIES if key in self._failures
        )

    def snapshot(self) -> CompilationSnapshot:
        """Return exact object references for products computed so far.

        The method never demands another session product. Canonical identities
        are derived only for already-retained phase objects.
        """

        with self._lock:
            syntax = cast(AstModule | None, self._values.get(SYNTAX_PRODUCT))
            analysis = cast(
                _AnalysisProduct | None, self._values.get(SEMANTIC_PRODUCT)
            )
            selection = cast(
                _SelectionProduct | None, self._values.get(SELECTION_PRODUCT)
            )
            planning = cast(
                _PlanningProduct | None, self._values.get(PLANNING_PRODUCT)
            )
            simulation_plan = self._values.get(SIMULATION_PLAN_PRODUCT)
            semantic_identity = (
                canonical_ir_identity(
                    lower(analysis.module, stage=OptimizationStage.HIGH_LEVEL)
                )
                if analysis is not None
                else None
            )
            selected_identity = (
                canonical_ir_identity(selection.optimization_ir)
                if selection is not None
                else None
            )
            planned_identity = (
                canonical_ir_identity(
                    lower(
                        planning.module,
                        stage=OptimizationStage.SELECTED_ARCHITECTURE,
                    )
                )
                if planning is not None
                else None
            )
            return CompilationSnapshot(
                source_identity=hashlib.sha256(self.source.encode()).hexdigest(),
                computed_products=self.computed_products,
                syntax=syntax,
                semantic=None if analysis is None else analysis.module,
                selected=None if selection is None else selection.module,
                planned=None if planning is None else planning.module,
                simulation_plan=simulation_plan,
                semantic_identity=semantic_identity,
                selected_identity=selected_identity,
                planned_identity=planned_identity,
            )

    def _demand(
        self,
        key: CompilationProductKey[_Product],
        builder: Callable[[], _Product],
    ) -> _Product:
        if key not in _TYPED_PRODUCT_DEPENDENCIES:
            raise KeyError(f"unknown compilation product '{key.name}'")
        with self._lock:
            if key in self._values:
                return cast(_Product, self._values[key])
            if key in self._failures:
                raise self._failures[key]
            if key in self._evaluating:
                raise RuntimeError(
                    f"compilation product dependency cycle at '{key.name}'"
                )
            self._evaluating.add(key)
            try:
                value = builder()
            except Exception as error:
                self._failures[key] = error
                raise
            finally:
                self._evaluating.remove(key)
            self._values[key] = value
            return value

    @property
    def syntax(self) -> AstModule:
        return self._demand(SYNTAX_PRODUCT, self._build_syntax)

    def _build_syntax(self) -> AstModule:
        syntax = parse(self.source)
        if self._options.top is None:
            return syntax
        candidates = (syntax, *syntax.submodules)
        selected = next(
            (item for item in candidates if item.name == self._options.top),
            None,
        )
        if selected is None:
            raise SessionTopSelectionError(
                f"top module '{self._options.top}' was not found"
            )
        return replace(
            selected,
            type_aliases=syntax.type_aliases,
            enums=syntax.enums,
            structs=syntax.structs,
            functions=syntax.functions,
            operators=syntax.operators,
            equivalences=syntax.equivalences,
            imports=syntax.imports,
            protocols=syntax.protocols,
            module_interfaces=syntax.module_interfaces,
            submodules=tuple(
                item for item in candidates if item.name != self._options.top
            ),
        )

    def _contributions(self) -> tuple[implementation_request_api.ImplementationContribution, ...]:
        explicit = implementation_request_api.ImplementationContribution(
            implementation_request_api.PolicyOrigin("explicit compiler options"),
            backend=(
                None
                if self._options.implementation_backend is None
                else implementation_request_api.BackendRequest(
                    implementation_request_api.BackendKind(
                        self._options.implementation_backend
                    ),
                    implementation_request_api.RequirementMode(
                        self._options.implementation_backend_mode
                    ),
                )
            ),
            target=self._options.target,
            architecture=(
                None
                if (
                    self._options.architecture is None
                    and self._options.architecture_mode is None
                )
                else implementation_request_api.ArchitectureRequest(
                    self._options.architecture,
                    ArchitectureSelectionMode(
                        self._options.architecture_mode
                        if self._options.architecture_mode is not None
                        else ArchitectureSelectionMode.PREFERRED
                    ),
                )
            ),
            evidence_policy=(
                None
                if self._options.target_evidence_policy is None
                else costs.SourcePolicy(self._options.target_evidence_policy)
            ),
            formal_policy=(
                None
                if self._options.formal_policy is None
                else formal_exploration.FormalPolicy(
                    self._options.formal_policy
                )
            ),
        )
        return (*self._options.implementation_contributions, explicit)

    def _formal_config(self, *, check_only: bool) -> formal_exploration.FormalExplorationConfig:
        preliminary = implementation_request_api.merge_implementation_contributions(*self._contributions())
        dependency_identity = dependency_context_identity(
            SimpleNamespace(
                root_module_identity=self._options.root_module_identity,
                dependency_closure=self._options.dependency_closure,
            )
        )
        return formal_exploration.FormalExplorationConfig(
            policy=(formal_exploration.FormalPolicy.OFF if check_only else preliminary.formal_policy),
            bmc_depth=self._options.formal_depth,
            max_formal_candidates=self._options.formal_max_candidates,
            timeout_seconds=self._options.formal_timeout,
            cache_directory=self._options.formal_cache,
            work_directory=self._options.formal_work_directory,
            dependency_identity=dependency_identity,
            artifact_provider=self.formal_artifact_provider,
            tool_resolver=self.formal_tool_resolver,
            backend="direct_systemverilog",
        )

    def _analyze(self) -> _AnalysisProduct:
        exploration_results: list[ExplorationResult] = []
        definition_resolutions: list[DefinitionResolution] | None = (
            [] if self._analysis_needs.wants(AnalysisNeeds.DEFINITIONS) else None
        )
        definition_declarations: list[DefinitionTarget] | None = (
            [] if self._analysis_needs.wants(AnalysisNeeds.DEFINITIONS) else None
        )
        completion_scopes: list[CompletionScope] | None = (
            [] if self._analysis_needs.wants(AnalysisNeeds.COMPLETION) else None
        )
        signature_help_calls: list[SignatureHelpCall] | None = (
            [] if self._analysis_needs.wants(AnalysisNeeds.SIGNATURE_HELP) else None
        )
        stdlib_sources: set[Path] = set()
        try:
            with track_resolved_stdlib_source_paths() as tracked_sources:
                stdlib_sources = tracked_sources
                module = analyze(
                    self.syntax,
                    exploration_results=exploration_results,
                    intent_structural_cache=self._intent_structural_cache,
                    intent_exploration_limits=(
                        implementation_request_api.merge_implementation_contributions(
                            *self._contributions()
                        ).exploration_limits
                    ),
                    # Semantic typing always generates and ranks candidates
                    # statically.  formal-aware selection execution is owned by selection below.
                    formal_config=self._formal_config(check_only=True),
                    formal_verifier=None,
                    source_unit=self._options.source_unit,
                    source_digest=(
                        hashlib.sha256(self.source.encode()).hexdigest()
                        if self._options.source_unit is not None
                        else None
                    ),
                    module_resolver=self._options.module_resolver,
                    root_module_identity=self._options.root_module_identity,
                    dependency_closure=self._options.dependency_closure,
                    analysis_needs=self._analysis_needs,
                    definition_resolutions=definition_resolutions,
                    definition_declarations=definition_declarations,
                    completion_scopes=completion_scopes,
                    signature_help_calls=signature_help_calls,
                    allow_external_enum_inputs=(
                        self._options.allow_external_enum_inputs
                    ),
                )
        finally:
            self._physical_inputs = self._physical_inputs.with_stdlib_sources(
                stdlib_sources
            )
        if (
            self._options.root_module_identity is not None
            or self._options.dependency_closure is not None
        ):
            module = replace(
                module,
                root_module_identity=self._options.root_module_identity,
                dependency_closure=self._options.dependency_closure,
            )
        return _AnalysisProduct(
            module,
            tuple(exploration_results),
            tuple(definition_resolutions or ()),
            tuple(definition_declarations or ()),
            tuple(completion_scopes or ()),
            tuple(signature_help_calls or ()),
        )

    @property
    def semantic_ir(self) -> IrModule:
        """Typed semantic IR without formal execution or later products."""

        product = self._demand(SEMANTIC_PRODUCT, self._analyze)
        return product.module

    @property
    def semantic_definition_resolutions(self) -> tuple[DefinitionResolution, ...]:
        """Compiler-owned definition records for editor tooling only."""

        product = self._demand(SEMANTIC_PRODUCT, self._analyze)
        return product.definition_resolutions

    @property
    def semantic_definition_declarations(self) -> tuple[DefinitionTarget, ...]:
        """Compiler-owned declaration targets for editor tooling only."""

        product = self._demand(SEMANTIC_PRODUCT, self._analyze)
        return product.definition_declarations

    @property
    def semantic_completion_scopes(self) -> tuple[CompletionScope, ...]:
        """Compiler-owned visible-scope records for editor completion."""

        product = self._demand(SEMANTIC_PRODUCT, self._analyze)
        return product.completion_scopes

    @property
    def semantic_signature_help_calls(self) -> tuple[SignatureHelpCall, ...]:
        """Compiler-owned resolved calls for editor signature help."""

        product = self._demand(SEMANTIC_PRODUCT, self._analyze)
        return product.signature_help_calls

    def check(self) -> IrModule:
        """Validate syntax and semantics, demanding no downstream product."""

        return self.semantic_ir

    @property
    def semantic_candidate_site_ledger(self) -> candidate_sites.CandidateSiteLedger:
        """OFF-policy candidate catalog without selection or verifier work."""

        product = self._demand(SEMANTIC_PRODUCT, self._analyze)
        return compilation_selection.candidate_site_ledger(
            product.module, product.exploration_results
        )

    @property
    def _selection(self) -> _SelectionProduct:
        return self._demand(SELECTION_PRODUCT, self._build_selection)

    def _build_selection(self) -> _SelectionProduct:
        analysis = self._demand(SEMANTIC_PRODUCT, self._analyze)
        return compilation_selection.SelectionBuilder().build(
            analysis,
            self._formal_config(check_only=False),
            self._options.formal_verifier,
            self.options.target,
            self._contributions(),
            self._intent_structural_cache,
        )

    @property
    def selected_ir(self) -> IrModule:
        return self._selection.module

    @property
    def selection(self) -> _SelectionProduct:
        """Return the configured immutable selection product on demand."""

        return self._selection

    @property
    def high_level_ir(self) -> CanonicalModule:
        return self._selection.high_level_ir

    @property
    def optimization_ir(self) -> CanonicalModule:
        return self._selection.optimization_ir

    @property
    def implementation_policy(self) -> implementation_policy_api.ModuleImplementationPolicy:
        return self._selection.implementation_policy

    @property
    def implementation_request(self) -> implementation_request_api.ImplementationRequest:
        return self._selection.implementation_request

    @property
    def candidate_site_ledger(self) -> candidate_sites.CandidateSiteLedger:
        """Deterministic retained candidate sites after configured selection."""

        return self._selection.candidate_site_ledger

    @property
    def formal_products(self) -> _FormalProduct:
        return self._demand(FORMAL_PRODUCT, self._build_formal)

    def _build_formal(self) -> _FormalProduct:
        return compilation_outputs.build_formal(self.selected_ir)

    @property
    def planning(self) -> _PlanningProduct:
        return self._demand(PLANNING_PRODUCT, self._build_planning)

    @property
    def simulation_plan(self):
        """Return the deterministic plan for the exact post-scheduling module."""

        return self._demand(SIMULATION_PLAN_PRODUCT, self._build_simulation_plan)

    def cached_simulation_plan(self):
        """Inspect or seed a strictly restored plan without demanding lowering.

        A caller may supply a validated plan only after demanding the planned
        module and checking its exact compilation recipe externally.
        """

        with self._lock:
            return self._values.get(SIMULATION_PLAN_PRODUCT)

    def accept_simulation_plan(self, plan):
        from zlang.simulation_plan_model import SimulationPlan

        if not isinstance(plan, SimulationPlan):
            raise TypeError("restored simulation plan must be strictly decoded")
        checked = SimulationPlan.from_bytes(plan.canonical_bytes)
        if checked.identity != plan.identity:
            raise ValueError("restored simulation plan identity changed")
        if checked.payload["module"] != self.planning.module.name:
            raise ValueError("restored simulation plan top does not match planning")
        return self._demand(SIMULATION_PLAN_PRODUCT, lambda: checked)

    def _build_simulation_plan(self):
        from zlang.simulation_plan_build import build_simulation_plan

        return build_simulation_plan(self.planning.module)

    @property
    def backend_implementation_plans(self) -> implementation_plans.BackendImplementationPlanningResult:
        return self.planning.backend_plans

    @property
    def implementation_graph(self):
        return self.planning.implementation_graph

    @property
    def target_planning_result(self):
        return self.planning.target_planning_result

    @property
    def physical_formal_records(self) -> tuple[formal_exploration.FormalExplorationRecord, ...]:
        return self.planning.physical_formal_records

    def _build_planning(self) -> _PlanningProduct:
        return compilation_selection.PlanningBuilder().build(
            self._selection,
            target_evidence=self._options.target_evidence,
            target_evidence_path=self._options.target_evidence_path,
            target_tool=self._options.target_tool,
            target_tool_version=self._options.target_tool_version,
            target_clock_period_ns=self._options.target_clock_period_ns,
            formal_config=self._formal_config(check_only=False),
            formal_verifier=self._options.formal_verifier,
        )

    @property
    def target_instance(self):
        return self._demand(TARGET_INSTANCE_PRODUCT, self._build_target_instance)

    def _build_target_instance(self):
        return compilation_outputs.build_target_instance(self._selection)

    @property
    def documents(self) -> _DocumentProduct:
        return self._demand(DOCUMENT_PRODUCT, self._build_documents)

    def _build_documents(self) -> _DocumentProduct:
        return compilation_outputs.build_documents(self.selected_ir)

    @property
    def reports(self) -> _ReportProduct:
        return self._demand(REPORT_PRODUCT, self._build_reports)

    def _build_reports(self) -> _ReportProduct:
        return compilation_outputs.build_reports(
            self._selection,
            self.planning,
        )

    def materialize(self) -> CompilationResult:
        """Demand every product needed by the eager compatibility facade."""

        return self._demand(MATERIALIZED_PRODUCT, self._build_materialized)

    def _build_materialized(self) -> CompilationResult:
        return compilation_outputs.build_materialized(
            syntax=self.syntax,
            selection=self._selection,
            formal=self.formal_products,
            planning=self.planning,
            target_instance=self.target_instance,
            documents=self.documents,
            reports=self.reports,
            physical_inputs=self.physical_inputs,
            formal_artifact_provider=self.formal_artifact_provider,
            formal_tool_resolver=self.formal_tool_resolver,
        )


__all__ = [
    "COMPILATION_PRODUCT_DEPENDENCIES",
    "CompilationProductKey",
    "CompilationSession",
    "CompilationSessionOptions",
    "CompilationSnapshot",
    "SessionTopSelectionError",
    "inline_locals",
]
