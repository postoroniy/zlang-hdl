# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Immutable products crossing the ordered semantic module pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from zlang.ast import nodes as ast
from zlang.dependencies import DependencyModuleIdentity
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.module_resolver import ModuleResolutionContext

if TYPE_CHECKING:
    from . import callables as semantic_callables
    from . import context as semantic_context


@dataclass(frozen=True)
class DeclarationPreparationProduct:
    module: ast.Module
    resolution_context: ModuleResolutionContext | None
    resolved_imports: tuple[object, ...]
    source_digests: dict[str, str]
    generic_dependency_identity: str
    source_unit: str | None
    source_digest: str | None
    enum_identity_namespace: str | None
    active_module_identity: DependencyModuleIdentity | None
    seen_imports: frozenset[str]
    protocol_schemas: tuple[ir_module.ProtocolSchema, ...]
    type_resolver: Any
    parameter_values: dict[str, int]
    unresolved_parameter_names: set[str]
    resolved_module_parameters: tuple[ir_module.ModuleParameter, ...]
    candidate_site_owner: str
    compile_time_budget: Any
    real_quantize_cache: dict[tuple[object, ...], Any]
    struct_types: tuple[ir_types.StructType, ...]
    enum_types: tuple[ir_types.EnumType, ...]
    tagged_union_types: tuple[ir_types.TaggedUnionType, ...]
    function_signatures: dict[str, semantic_callables.FunctionSignature]
    generic_functions: dict[str, ast.FunctionDecl]
    expression_context: semantic_context.ExpressionContext
    functions: tuple[ir_module.Function, ...]


@dataclass(frozen=True)
class HardwareInterfaceProduct:
    clock_domains: tuple[Any, ...]
    clock: str | None
    reset: str | None
    timing_names: frozenset[str]
    state_domains: Any
    symbols: dict[str, ir_module.Port]
    ports: tuple[ir_module.Port, ...]
    port_origins: dict[str, Any]
    source_scalar_ports: tuple[ir_module.Port, ...]
    external_model_function: ir_module.Function | None
    csr_module_identity: str
    csr_blocks: tuple[Any, ...]
    connections: tuple[ir_module.Connection, ...]
    readable_cdc_outputs: frozenset[str]
    arbiters: tuple[Any, ...]
    request_responses: tuple[ir_module.RequestResponseInterface, ...]
    request_response_symbols: dict[str, ir_module.RequestResponseInterface]
    csr_names: frozenset[str]


@dataclass(frozen=True)
class StateStoragePreparationProduct:
    ports: tuple[ir_module.Port, ...]
    symbols: dict[str, ir_module.Port]
    protocol_endpoints: tuple[ir_module.ProtocolEndpoint, ...]
    struct_types: tuple[ir_types.StructType, ...]
    storage_declarations: Any
    outputs: dict[str, ir_module.Port]
    protocol_interfaces: dict[str, ir_module.Port]
    registers: tuple[ir_module.Register, ...]
    register_symbols: dict[str, ir_module.Register]
    expression_context: semantic_context.ExpressionContext
    value_symbols: dict[str, object]
    aggregate_protocol_endpoints: tuple[ir_module.AggregateProtocolEndpoint, ...]
    resource_controls: dict[str, dict[str, ast.Expression]]


@dataclass(frozen=True)
class ModuleBehaviorProduct:
    storage: Any
    instances: Any
    known_modules: dict[str, ast.Module]
    locals: tuple[ir_module.LocalValue, ...]
    next_assignments: tuple[ir_module.NextAssignment, ...]
    rule_analysis: Any
    transition: Any
    elastic_pipeline_regions: tuple[Any, ...]
    assigned_outputs: frozenset[tuple[str, object | None, object | None]]
    equivalences: tuple[Any, ...]


@dataclass(frozen=True)
class ModuleHierarchyProduct:
    connections: Any
    assignments: Any
    protocol_endpoints: tuple[ir_module.ProtocolEndpoint, ...]
    request_responses: tuple[ir_module.RequestResponseInterface, ...]
    contract_symbols: dict[str, object]
    contract_context: semantic_context.ExpressionContext
