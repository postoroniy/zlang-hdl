# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Owned semantic-analysis configuration, mutable services, and lexical scope."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from zlang.analysis_needs import AnalysisNeeds
from zlang.ast import nodes as ast
from zlang.completion_resolution import CompletionScope
from zlang.definition_resolution import DefinitionResolution, DefinitionTarget
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir.expression_arena import SemanticExpressionArena
from zlang.ir import functional_regions as functional_regions
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.types import HardwareType, StructType
from zlang.implementation_limits import IntentExplorationLimits
from zlang.intent_structural_exploration import IntentStructuralExplorationCache
from zlang.signature_help_resolution import SignatureHelpCall
from zlang.source import SourceOrigin

from .callable_state import CallableSpecializationCache
if TYPE_CHECKING:
    from zlang.dependencies import DependencyClosure, DependencyModuleIdentity
    from zlang.ir.cdc import ClockDomain
    from zlang.ir.hierarchy import HierarchyTraversalCache
    from zlang.module_resolver import ModuleResolutionContext, ModuleResolver
    from .callable_bodies import CallableBodyAnalyzer
    from .callable_specialization import CallableSpecializer
    from .expressions import ExpressionAnalyzer


def _new_callable_specializer() -> CallableSpecializer:
    from .callable_specialization import CallableSpecializer

    return CallableSpecializer()


def _new_callable_body_analyzer() -> CallableBodyAnalyzer:
    from .callable_bodies import CallableBodyAnalyzer

    return CallableBodyAnalyzer()


def _new_expression_analyzer() -> ExpressionAnalyzer:
    from .expressions import ExpressionAnalyzer

    return ExpressionAnalyzer()


@dataclass(frozen=True)
class SourceAnalysisContext:
    """Source identity and source-facing policy for one module analysis."""

    module: ast.Module
    source_unit: str | None = None
    source_digest: str | None = None
    allow_external_enum_inputs: bool = False
    enum_identity_namespace: str | None = None


@dataclass(frozen=True)
class ResolutionAnalysisContext:
    """Compiler-owned import and locked dependency resolution inputs."""

    module_resolver: ModuleResolver | None = None
    resolution_context: ModuleResolutionContext | None = None
    root_module_identity: DependencyModuleIdentity | None = None
    dependency_closure: DependencyClosure | None = None
    imports_premerged: bool = False


@dataclass(frozen=True)
class HierarchyAnalysisContext:
    """Shared traversal state and inherited domain for recursive analysis."""

    inherited_domain: tuple[str, str] | ClockDomain | None = None
    instance_stack: tuple[str, ...] = ()
    cache: HierarchyTraversalCache | None = None


@dataclass(frozen=True)
class SpecializationAnalysisContext:
    """Bindings supplied while specializing a module or callable."""

    type_bindings: dict[str, HardwareType] | None = None
    constant_bindings: dict[str, ir_expr.Expression] | None = None
    callable_bindings: dict[str, object] | None = None


@dataclass(frozen=True)
class CompileTimeAnalysisContext:
    """Compile-time evaluation resources shared with recursive analysis."""

    budget: object | None = None
    real_quantize_cache: dict[tuple[object, ...], object] | None = None


@dataclass(frozen=True)
class ImplementationAnalysisContext:
    """Implementation-intent observations produced by semantic analysis."""

    exploration_results: list[object] | None = None
    structural_cache: IntentStructuralExplorationCache | None = None
    exploration_limits: IntentExplorationLimits | None = None


@dataclass(frozen=True)
class VerificationAnalysisContext:
    """Formal policy inputs used only by semantic verification analysis."""

    formal_config: object | None = None
    formal_verifier: object | None = None


@dataclass(frozen=True)
class ToolingObservationContext:
    """Demand-driven compiler observation sinks for editor queries."""

    analysis_needs: AnalysisNeeds = AnalysisNeeds.NONE
    definition_resolutions: list[DefinitionResolution] | None = None
    definition_targets: dict[int, SourceOrigin] = field(default_factory=dict)
    definition_declarations: list[DefinitionTarget] | None = None
    completion_scopes: list[CompletionScope] | None = None
    signature_help_calls: list[SignatureHelpCall] | None = None


@dataclass(frozen=True)
class AnalysisContext:
    """Composed ownership boundary for one top-level semantic analysis."""

    source: SourceAnalysisContext
    resolution: ResolutionAnalysisContext = field(
        default_factory=ResolutionAnalysisContext
    )
    hierarchy: HierarchyAnalysisContext = field(
        default_factory=HierarchyAnalysisContext
    )
    specialization: SpecializationAnalysisContext = field(
        default_factory=SpecializationAnalysisContext
    )
    compile_time: CompileTimeAnalysisContext = field(
        default_factory=CompileTimeAnalysisContext
    )
    implementation: ImplementationAnalysisContext = field(
        default_factory=ImplementationAnalysisContext
    )
    verification: VerificationAnalysisContext = field(
        default_factory=VerificationAnalysisContext
    )
    tooling: ToolingObservationContext = field(
        default_factory=ToolingObservationContext
    )


@dataclass(frozen=True)
class AnalysisEnvironment:
    """Immutable catalog and configuration shared by expression scopes."""

    functions: dict[str, object]
    clock_domains: tuple[str, ...] = ()
    default_clock_domain: str | None = None
    generic_functions: dict[str, ast.FunctionDecl] = field(default_factory=dict)
    function_catalog: object | None = None
    operator_declarations: tuple[ast.OperatorDecl, ...] = ()
    struct_declarations: tuple[ast.StructDecl, ...] = ()
    generic_dependency_identity: tuple[tuple[str, str], ...] = ()
    structs: tuple[StructType, ...] = ()
    parameters: dict[str, int] = field(default_factory=dict)
    unresolved_parameters: frozenset[str] = frozenset()
    type_resolver: object | None = None
    source_digests: dict[str, str] = field(default_factory=dict)
    formal_config: object | None = None
    formal_verifier: object | None = None


@dataclass
class AnalysisServices:
    """Mutable state owned by exactly one top-level semantic analysis."""

    callables: CallableSpecializationCache = field(
        default_factory=CallableSpecializationCache
    )
    callable_specializer: CallableSpecializer = field(
        default_factory=_new_callable_specializer
    )
    callable_bodies: CallableBodyAnalyzer = field(
        default_factory=_new_callable_body_analyzer
    )
    expression_analysis: ExpressionAnalyzer = field(
        default_factory=_new_expression_analyzer
    )
    compile_time_real_quantize_cache: dict[tuple[object, ...], object] = field(
        default_factory=dict
    )
    compile_time_budget: object | None = None
    exploration_results: list[object] | None = None
    intent_structural_cache: IntentStructuralExplorationCache | None = None
    intent_exploration_limits: IntentExplorationLimits | None = None
    tooling: ToolingObservationContext = field(default_factory=ToolingObservationContext)
    expression_arena: SemanticExpressionArena = field(
        default_factory=SemanticExpressionArena
    )
    local_expansion_nodes: int = 0


@dataclass(frozen=True)
class ExpressionScope:
    """Cheap lexical state derived while checking one expression region."""

    allow_delay: bool
    compile_time_constants: dict[str, ir_expr.Expression] = field(default_factory=dict)
    static_callables: dict[str, object] = field(default_factory=dict)
    resolution_stack: tuple[str, ...] = ()
    allow_fixed_target_coercion: bool = True
    instance_outputs: dict[tuple[str, str], HardwareType] = field(default_factory=dict)
    instance_output_protocols: dict[
        tuple[str, str], InterfaceProtocol
    ] = field(default_factory=dict)
    instance_output_domains: dict[tuple[str, str], str | None] = field(
        default_factory=dict
    )
    instance_protocol_outputs: dict[
        tuple[str, str], tuple[str, HardwareType, str | None]
    ] = field(default_factory=dict)
    instance_protocol_transfers: dict[
        tuple[str, str], ir_expr.Expression
    ] = field(default_factory=dict)
    instance_csr_state_paths: dict[
        tuple[str, ...], tuple[str, HardwareType, str | None]
    ] = field(default_factory=dict)
    instance_arrays: dict[str, int] = field(default_factory=dict)
    allow_runtime_instance_projection: bool = False
    write_only_outputs: dict[str, ir_module.Port] = field(default_factory=dict)
    readable_cdc_outputs: set[str] = field(default_factory=set)
    allow_output_reads: bool = False
    allow_implementation_choice: bool = False
    candidate_site_owner: str | None = None
    next_delay_instance: int = 0
    index_bindings: dict[str, int] = field(default_factory=dict)
    functional_symbolic_values: dict[str, functional_regions.CompileTimeExpr] = field(
        default_factory=dict
    )
    functional_specialization_certificates: list[
        functional_regions.FunctionalSpecializationCertificate
    ] = field(default_factory=list)
    range_refinements: dict[str, ir_expr.ValueRange] = field(default_factory=dict)
    index_types: dict[str, HardwareType] = field(default_factory=dict)
    aggregate_paths: dict[str, str] = field(default_factory=dict)
    union_binders: dict[str, ir_expr.Expression] = field(default_factory=dict)
    source_unit: str | None = None
    source_digest: str | None = None
    functional_binder_ordinals: dict[int, int] = field(default_factory=dict)
    next_functional_binder_ordinal: list[int] = field(default_factory=lambda: [0])
    functional_binder_nesting: tuple[int, ...] = ()
    functional_binder_callable_identity: str | None = None


class ExpressionContext:
    """Explicit environment, service, and lexical-scope ownership boundary."""

    def __init__(
        self,
        environment: AnalysisEnvironment,
        services: AnalysisServices,
        scope: ExpressionScope,
    ) -> None:
        self._environment = environment
        self._services = services
        self._scope = scope

    @property
    def environment(self) -> AnalysisEnvironment:
        return self._environment

    @property
    def services(self) -> AnalysisServices:
        return self._services

    @property
    def scope(self) -> ExpressionScope:
        return self._scope

    @property
    def expressions(self) -> ExpressionAnalyzer:
        return self._services.expression_analysis

    def with_environment(self, **changes: object) -> "ExpressionContext":
        return ExpressionContext(
            replace(self._environment, **changes), self._services, self._scope
        )

    def with_services(self, **changes: object) -> "ExpressionContext":
        return ExpressionContext(
            self._environment, replace(self._services, **changes), self._scope
        )

    def with_scope(self, **changes: object) -> "ExpressionContext":
        return ExpressionContext(
            self._environment, self._services, replace(self._scope, **changes)
        )

    def allocate_delay(self) -> int:
        instance = self._scope.next_delay_instance
        object.__setattr__(
            self,
            "_scope",
            replace(self._scope, next_delay_instance=instance + 1),
        )
        return instance

    def advance_delay_allocator(self, next_instance: int) -> None:
        """Advance the shared delay-name allocator without reusing an identity."""

        if next_instance <= self._scope.next_delay_instance:
            return
        object.__setattr__(
            self,
            "_scope",
            replace(self._scope, next_delay_instance=next_instance),
        )
