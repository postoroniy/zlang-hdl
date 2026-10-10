"""Name resolution and type checking for ZLang HDL."""

from __future__ import annotations

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import cdc as ir_cdc
from zlang.ir import hierarchy as ir_hierarchy
from zlang.analysis_needs import AnalysisNeeds
from zlang.ir import types as ir_types
from zlang.completion_resolution import CompletionScope
from zlang.definition_resolution import DefinitionResolution, DefinitionTarget
from zlang.implementation_limits import IntentExplorationLimits
from zlang.intent_structural_exploration import IntentStructuralExplorationCache
from zlang.signature_help_resolution import SignatureHelpCall
from .errors import SemanticError
from . import context as semantic_context
from . import callables as semantic_callables
from . import compile_time_evaluation
from . import module_behavior
from . import module_declarations
from . import module_finalization
from . import module_hardware
from . import module_hierarchy_analysis
from . import module_state_storage
from zlang.dependencies import DependencyClosure, DependencyModuleIdentity
from zlang import module_resolver as module_resolution


class SemanticAnalyzer:
    """Orchestrate ordered semantic services over one analysis context."""

    def analyze_module(
        self,
        module: ast.Module,
        **options: object,
    ) -> ir_module.Module:
        """Recursively analyze a child through this analyzer instance."""

        return analyze(module, _semantic_analyzer=self, **options)

    def analyze(
        self, context: semantic_context.AnalysisContext
    ) -> ir_module.Module:
        """Run the established semantic phase order through extracted owners."""

        module = context.source.module
        _instance_stack = context.hierarchy.instance_stack
        analysis_needs = context.tooling.analysis_needs

        try:
            analysis_needs = AnalysisNeeds(analysis_needs)
        except (TypeError, ValueError) as error:
            raise TypeError("analysis_needs must be an AnalysisNeeds value") from error

        if module.name in _instance_stack:
            cycle = " -> ".join((*_instance_stack, module.name))
            raise SemanticError(f"cyclic module hierarchy is not allowed: {cycle}")
        active_instance_stack = (*_instance_stack, module.name)
        selected_hierarchy_cache = (
            context.hierarchy.cache or ir_hierarchy.HierarchyTraversalCache()
        )

        preparation = (
            module_declarations.DeclarationAndCallablePreparer().prepare(context)
        )
        hardware = module_hardware.HardwareInterfacePreparer().prepare(
            context,
            preparation,
        )
        state_storage = module_state_storage.StateStoragePreparer().prepare(
            preparation, hardware
        )
        behavior = module_behavior.ModuleBehaviorAnalyzer(self).analyze(
            context,
            preparation,
            hardware,
            state_storage,
            analysis_needs=analysis_needs,
            active_instance_stack=active_instance_stack,
            hierarchy_cache=selected_hierarchy_cache,
        )
        hierarchy = module_hierarchy_analysis.ModuleHierarchyAnalyzer().analyze(
            context,
            preparation,
            hardware,
            state_storage,
            behavior,
        )
        return module_finalization.SemanticModuleFinalizer().finalize(
            context,
            preparation,
            hardware,
            state_storage,
            behavior,
            hierarchy,
            hierarchy_cache=selected_hierarchy_cache,
        )


def analyze(
    module: ast.Module,
    *,
    exploration_results: list[object] | None = None,
    intent_structural_cache: IntentStructuralExplorationCache | None = None,
    intent_exploration_limits: IntentExplorationLimits | None = None,
    formal_config: object | None = None,
    formal_verifier: object | None = None,
    inherited_domain: tuple[str, str] | ir_cdc.ClockDomain | None = None,
    specialization_type_bindings: dict[str, ir_types.HardwareType] | None = None,
    specialization_constant_bindings: dict[str, ir_expr.Expression] | None = None,
    specialization_callable_bindings: dict[str, semantic_callables.StaticCallableBinding] | None = None,
    compile_time_budget: compile_time_evaluation.CompileTimeBudget | None = None,
    _compile_time_real_quantize_cache: dict[
        tuple[object, ...], tuple[int, int]
    ] | None = None,
    source_unit: str | None = None,
    source_digest: str | None = None,
    allow_external_enum_inputs: bool = False,
    enum_identity_namespace: str | None = None,
    module_resolver: module_resolution.ModuleResolver | None = None,
    resolution_context: module_resolution.ModuleResolutionContext | None = None,
    root_module_identity: DependencyModuleIdentity | None = None,
    dependency_closure: DependencyClosure | None = None,
    _imports_premerged: bool = False,
    _instance_stack: tuple[str, ...] = (),
    _hierarchy_cache: ir_hierarchy.HierarchyTraversalCache | None = None,
    analysis_needs: AnalysisNeeds = AnalysisNeeds.NONE,
    definition_resolutions: list[DefinitionResolution] | None = None,
    definition_declarations: list[DefinitionTarget] | None = None,
    completion_scopes: list[CompletionScope] | None = None,
    signature_help_calls: list[SignatureHelpCall] | None = None,
    _semantic_analyzer: SemanticAnalyzer | None = None,
) -> ir_module.Module:
    """Resolve and type-check an AST module into backend-independent IR."""

    context = semantic_context.AnalysisContext(
        source=semantic_context.SourceAnalysisContext(
            module=module,
            source_unit=source_unit,
            source_digest=source_digest,
            allow_external_enum_inputs=allow_external_enum_inputs,
            enum_identity_namespace=enum_identity_namespace,
        ),
        resolution=semantic_context.ResolutionAnalysisContext(
            module_resolver=module_resolver,
            resolution_context=resolution_context,
            root_module_identity=root_module_identity,
            dependency_closure=dependency_closure,
            imports_premerged=_imports_premerged,
        ),
        hierarchy=semantic_context.HierarchyAnalysisContext(
            inherited_domain=inherited_domain,
            instance_stack=_instance_stack,
            cache=_hierarchy_cache,
        ),
        specialization=semantic_context.SpecializationAnalysisContext(
            type_bindings=specialization_type_bindings,
            constant_bindings=specialization_constant_bindings,
            callable_bindings=specialization_callable_bindings,
        ),
        compile_time=semantic_context.CompileTimeAnalysisContext(
            budget=compile_time_budget,
            real_quantize_cache=_compile_time_real_quantize_cache,
        ),
        implementation=semantic_context.ImplementationAnalysisContext(
            exploration_results=exploration_results,
            structural_cache=intent_structural_cache,
            exploration_limits=intent_exploration_limits,
        ),
        verification=semantic_context.VerificationAnalysisContext(
            formal_config=formal_config,
            formal_verifier=formal_verifier,
        ),
        tooling=semantic_context.ToolingObservationContext(
            analysis_needs=analysis_needs,
            definition_resolutions=definition_resolutions,
            definition_declarations=definition_declarations,
            completion_scopes=completion_scopes,
            signature_help_calls=signature_help_calls,
        ),
    )
    return (_semantic_analyzer or SemanticAnalyzer()).analyze(context)
