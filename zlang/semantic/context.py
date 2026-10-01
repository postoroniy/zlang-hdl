# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Owned semantic-analysis configuration, mutable services, and lexical scope."""

from __future__ import annotations

from dataclasses import MISSING, dataclass, field, fields, replace

from zlang.analysis_needs import AnalysisNeeds
from zlang.ast import nodes as ast
from zlang.completion_resolution import CompletionScope
from zlang.definition_resolution import DefinitionResolution, DefinitionTarget
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir.expression_arena import SemanticExpressionArena
from zlang.ir.functional_regions import (
    CompileTimeExpr,
    FunctionalSpecializationCertificate,
)
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.types import HardwareType, StructType
from zlang.signature_help_resolution import SignatureHelpCall
from zlang.source import SourceOrigin


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
    analysis_needs: AnalysisNeeds = AnalysisNeeds.NONE
    formal_config: object | None = None
    formal_verifier: object | None = None


@dataclass
class AnalysisServices:
    """Mutable state owned by exactly one top-level semantic analysis."""

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
    compile_time_real_quantize_cache: dict[tuple[object, ...], object] = field(
        default_factory=dict
    )
    compile_time_budget: object | None = None
    exploration_results: list[object] | None = None
    definition_resolutions: list[DefinitionResolution] | None = None
    definition_targets: dict[int, SourceOrigin] = field(default_factory=dict)
    definition_declarations: list[DefinitionTarget] | None = None
    completion_scopes: list[CompletionScope] | None = None
    signature_help_calls: list[SignatureHelpCall] | None = None
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
    functional_symbolic_values: dict[str, CompileTimeExpr] = field(
        default_factory=dict
    )
    functional_specialization_certificates: list[
        FunctionalSpecializationCertificate
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


_CONTEXT_OWNERS = (AnalysisEnvironment, AnalysisServices, ExpressionScope)
_CONTEXT_FIELD_OWNER = {
    descriptor.name: owner
    for owner in _CONTEXT_OWNERS
    for descriptor in fields(owner)
}


def _owner_from_values(owner: type, values: dict[str, object]):
    arguments: dict[str, object] = {}
    for descriptor in fields(owner):
        if descriptor.name in values:
            arguments[descriptor.name] = values.pop(descriptor.name)
        elif descriptor.default is not MISSING:
            arguments[descriptor.name] = descriptor.default
        elif descriptor.default_factory is not MISSING:
            arguments[descriptor.name] = descriptor.default_factory()
        else:
            raise TypeError(f"missing expression-context field '{descriptor.name}'")
    return owner(**arguments)


class ExpressionContext:
    """Compatibility facade over explicit environment/services/scope owners."""

    def __init__(
        self,
        functions: dict[str, object],
        *,
        allow_delay: bool,
        **values: object,
    ) -> None:
        retained = dict(values)
        retained["functions"] = functions
        retained["allow_delay"] = allow_delay
        object.__setattr__(
            self, "_environment", _owner_from_values(AnalysisEnvironment, retained)
        )
        object.__setattr__(
            self, "_services", _owner_from_values(AnalysisServices, retained)
        )
        object.__setattr__(
            self, "_scope", _owner_from_values(ExpressionScope, retained)
        )
        if retained:
            names = ", ".join(sorted(retained))
            raise TypeError(f"unknown expression-context field(s): {names}")

    @classmethod
    def _from_parts(
        cls,
        environment: AnalysisEnvironment,
        services: AnalysisServices,
        scope: ExpressionScope,
    ) -> "ExpressionContext":
        context = object.__new__(cls)
        object.__setattr__(context, "_environment", environment)
        object.__setattr__(context, "_services", services)
        object.__setattr__(context, "_scope", scope)
        return context

    @property
    def environment(self) -> AnalysisEnvironment:
        return self._environment

    @property
    def services(self) -> AnalysisServices:
        return self._services

    @property
    def scope(self) -> ExpressionScope:
        return self._scope

    def __getattr__(self, name: str):
        owner = _CONTEXT_FIELD_OWNER.get(name)
        if owner is AnalysisEnvironment:
            return getattr(self._environment, name)
        if owner is AnalysisServices:
            return getattr(self._services, name)
        if owner is ExpressionScope:
            return getattr(self._scope, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: object) -> None:
        owner = _CONTEXT_FIELD_OWNER.get(name)
        if owner is None:
            object.__setattr__(self, name, value)
            return
        attribute = {
            AnalysisEnvironment: "_environment",
            AnalysisServices: "_services",
            ExpressionScope: "_scope",
        }[owner]
        object.__setattr__(
            self,
            attribute,
            replace(getattr(self, attribute), **{name: value}),
        )

    def derive(self, **changes: object) -> "ExpressionContext":
        unknown = set(changes) - set(_CONTEXT_FIELD_OWNER)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise TypeError(f"unknown expression-context field(s): {names}")
        owners = {
            AnalysisEnvironment: self._environment,
            AnalysisServices: self._services,
            ExpressionScope: self._scope,
        }
        for owner in _CONTEXT_OWNERS:
            updates = {
                name: value
                for name, value in changes.items()
                if _CONTEXT_FIELD_OWNER[name] is owner
            }
            if updates:
                owners[owner] = replace(owners[owner], **updates)
        return self._from_parts(
            owners[AnalysisEnvironment],
            owners[AnalysisServices],
            owners[ExpressionScope],
        )

    def allocate_delay(self) -> int:
        instance = self.next_delay_instance
        self.next_delay_instance += 1
        return instance


__all__ = [
    "AnalysisEnvironment",
    "AnalysisServices",
    "ExpressionContext",
    "ExpressionScope",
]
