# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned module-instance specialization and signature preparation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass, replace
import hashlib
from typing import Any

from zlang.ast import nodes as ast
from zlang.ir import cdc as ir_cdc
from zlang.ir import csr as ir_csr
from zlang.ir import expressions as ir_expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir.signed_reductions import expression_semantic_identity

from . import callables as semantic_callables
from . import compile_time_evaluation
from . import context as semantic_context
from . import hierarchy as semantic_hierarchy
from . import module_interfaces
from . import module_pipeline
from .errors import SemanticError
from . import expression_support
from . import observations as semantic_observations
from . import type_resolution
from .imports import dependency_identity_for_source


_HIERARCHY_ANALYZER = semantic_hierarchy.HierarchyAnalyzer()


InstanceSpecialization = tuple[
    dict[str, int | str],
    dict[str, ir_types.HardwareType],
    dict[str, ir_expr.Expression],
    dict[str, semantic_callables.StaticCallableBinding],
    tuple[ast.ModuleParameter, ...],
    tuple[ir_module.Specialization, ...],
]


@dataclass(frozen=True)
class InstanceAnalysisContext:
    """Inputs shared by instance specialization and signature predeclaration."""

    module: ast.Module
    expression_context: semantic_context.ExpressionContext
    type_resolver: Any
    value_symbols: dict[str, object]
    symbols: dict[str, ir_module.Port]
    resource_symbols: dict[str, ir_module.Port]
    request_response_symbols: dict[str, ir_module.Port]
    clock_domains: tuple[ir_cdc.ClockDomain, ...]
    clock: str | None
    reset: str | None
    enum_identity_namespace: str | None
    source_unit: str | None
    source_digest: str | None


class InstanceSpecializer:
    """Own child declaration inheritance and specialization-local caches."""

    def __init__(self, context: InstanceAnalysisContext) -> None:
        self.context = context
        self.known_modules = {
            item.name: item for item in context.module.submodules
        }
        self.known_modules[context.module.name] = context.module
        self.constant_locals: dict[str, ir_module.LocalValue] = {}
        self._constant_stack: set[str] = set()
        self._local_declarations = {
            item.target: item
            for item in context.module.assignments
            if "." not in item.target
            and item.target not in context.symbols
            and item.target not in context.resource_symbols
            and item.target not in context.request_response_symbols
        }
        self._specializations: dict[int, InstanceSpecialization] = {}

    @staticmethod
    def _inherit_named_declarations(
        local: tuple[object, ...], inherited: tuple[object, ...]
    ) -> tuple[object, ...]:
        names = {getattr(item, "name") for item in local}
        return tuple(
            (*local, *(item for item in inherited if getattr(item, "name") not in names))
        )

    def child_declaration(self, module_name: str) -> ast.Module:
        parent = self.context.module
        child = self.known_modules[module_name]
        inherit = self._inherit_named_declarations
        child = replace(
            child,
            type_aliases=inherit(child.type_aliases, parent.type_aliases),
            structs=inherit(child.structs, parent.structs),
            enums=inherit(child.enums, parent.enums),
            tagged_unions=inherit(child.tagged_unions, parent.tagged_unions),
            protocols=inherit(child.protocols, parent.protocols),
            module_interfaces=inherit(
                child.module_interfaces, parent.module_interfaces
            ),
            functions=inherit(child.functions, parent.functions),
            operators=child.operators or parent.operators,
            equivalences=parent.equivalences,
            submodules=parent.submodules,
        )
        if child.conforms_to is None:
            return child
        declaration = next(
            (
                item
                for item in child.module_interfaces
                if item.name == child.conforms_to.name
            ),
            None,
        )
        if declaration is None:
            return child
        if child.external_model is not None and declaration.parameters:
            raise SemanticError(
                f"external module '{child.name}' requires a non-parameterized "
                "named interface in this first slice",
                code="ZL-EXTERN-UNSUPPORTED",
            )
        return module_interfaces.inherit_applied_interface_surface(
            child, declaration
        )

    def specialized_child(
        self,
        declaration: ast.InstanceDecl,
        specialized_parameters: tuple[ast.ModuleParameter, ...],
    ) -> ast.Module:
        return replace(
            self.child_declaration(declaration.module),
            parameters=specialized_parameters,
        )

    def resolve_parent_constant(self, name: str) -> ir_expr.Expression:
        context = self.context
        expression_context = context.expression_context
        inherited = expression_context.scope.compile_time_constants.get(name)
        if inherited is not None:
            return inherited
        cached = self.constant_locals.get(name)
        if cached is not None:
            return cached.expression
        declaration = self._local_declarations.get(name)
        if declaration is None:
            raise SemanticError(
                f"compile-time constant argument '{name}' is not a module-local value"
            )
        if name in self._constant_stack:
            raise SemanticError(
                f"compile-time constant dependency cycle contains '{name}'"
            )
        self._constant_stack.add(name)
        try:
            dependencies: set[str] = set()

            def collect(value: object) -> None:
                if isinstance(value, ast.NameExpr):
                    dependencies.add(value.name)
                elif isinstance(value, tuple):
                    for item in value:
                        collect(item)
                elif is_dataclass(value) and not isinstance(value, type):
                    for item in fields(value):
                        if item.name != "origin":
                            collect(getattr(value, item.name))

            collect(declaration.expression)
            for dependency in sorted(dependencies):
                if dependency in self._local_declarations:
                    self.resolve_parent_constant(dependency)
            constant_symbols = {
                **context.value_symbols,
                **self.constant_locals,
            }
            expected_type = (
                context.type_resolver.resolve(declaration.type_name)
                if declaration.type_name is not None
                else None
            )
            value = (
                expression_context.expressions.check_typed_boundary(
                    declaration.expression,
                    constant_symbols,
                    expected_type,
                    expression_context,
                )
                if expected_type is not None
                else expression_context.expressions.check(
                    declaration.expression,
                    constant_symbols,
                    None,
                    expression_context,
                )
            )
            semantic_callables._validate_tuple_destructure_assignment(declaration, value)
            value = semantic_callables._expand_analysis_calls(
                value,
                expression_context,
                purpose=f"compile-time local '{name}'",
            )
            compile_time, value_range = expression_support._local_constant_and_range(
                value, expression_context, name=name
            )
            if not compile_time:
                raise SemanticError(
                    f"compile-time constant argument '{name}' depends on runtime hardware"
                )
            try:
                constant_runtime_value(value)
            except ConstantExpressionError as error:
                raise SemanticError(
                    f"compile-time constant argument '{name}' is not fully "
                    f"constant: {error}"
                ) from error
            local = ir_module.LocalValue(
                name,
                value.type,
                value,
                True,
                value_range,
                expression_semantic_identity(value),
            )
            self.constant_locals[name] = local
            context.value_symbols[name] = local
            expression_context.scope.compile_time_constants[name] = value
            return value
        finally:
            self._constant_stack.discard(name)

    def resolve(self, declaration: ast.InstanceDecl) -> InstanceSpecialization:
        cached = self._specializations.get(id(declaration))
        if cached is not None:
            return cached
        if declaration.module not in self.known_modules:
            raise SemanticError(
                f"unknown instantiated module '{declaration.module}'"
            )
        child = self.child_declaration(declaration.module)
        (
            resolved_arguments,
            resolved_type_arguments,
            deferred_arguments,
        ) = self._resolve_explicit_arguments(declaration, child)
        self._infer_scalar_arguments(
            declaration,
            child,
            resolved_arguments,
            resolved_type_arguments,
        )
        self._apply_parameter_defaults(
            declaration,
            child,
            resolved_arguments,
            resolved_type_arguments,
        )
        result = self._bind_deferred_arguments(
            child,
            resolved_arguments,
            resolved_type_arguments,
            deferred_arguments,
        )
        self._specializations[id(declaration)] = result
        return result

    def _resolve_explicit_arguments(
        self,
        declaration: ast.InstanceDecl,
        child: ast.Module,
    ) -> tuple[
        dict[str, int | str],
        dict[str, ir_types.HardwareType],
        list[ast.SpecializationArgument],
    ]:
        parameters = child.parameters
        parameter_names = {item.name for item in parameters}
        seen: set[str] = set()
        positional = 0
        resolved: dict[str, int | str] = {}
        types: dict[str, ir_types.HardwareType] = {}
        deferred: list[ast.SpecializationArgument] = []
        for argument in declaration.arguments:
            key = argument.name
            if key is None:
                while positional < len(parameters) and parameters[positional].name in seen:
                    positional += 1
                if positional >= len(parameters):
                    raise SemanticError(
                        f"too many specialization arguments for '{declaration.module}'"
                    )
                key = parameters[positional].name
                positional += 1
            if key not in parameter_names:
                raise SemanticError(f"unknown specialization parameter '{key}'")
            if key in seen:
                raise SemanticError(
                    f"specialization parameter '{key}' is assigned more than once"
                )
            seen.add(key)
            parameter = next(item for item in parameters if item.name == key)
            value = argument.value
            if parameter.kind in {"constant", "callable"}:
                if argument.name is None:
                    raise SemanticError(
                        f"compile-time {parameter.kind} parameter '{key}' of "
                        f"'{declaration.module}' requires a named argument"
                    )
                deferred.append(argument)
            elif parameter.kind == "type":
                if isinstance(value, int):
                    raise SemanticError(
                        f"type parameter '{key}' of '{declaration.module}' "
                        "requires a type argument"
                    )
                syntax = value if isinstance(
                    value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)
                ) else ast.TypeName(str(value))
                try:
                    resolved_type = self.context.type_resolver.resolve(syntax)
                except SemanticError as error:
                    raise SemanticError(
                        f"cannot resolve type argument for parameter '{key}' of "
                        f"'{declaration.module}': {error}"
                    ) from error
                types[key] = resolved_type
                resolved[key] = str(resolved_type)
            else:
                resolved[key] = self._resolve_value_argument(
                    declaration, key, value
                )
        return resolved, types, deferred

    def _resolve_value_argument(
        self,
        declaration: ast.InstanceDecl,
        key: str,
        value: int | str | ast.TypeSyntax,
    ) -> int:
        if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)):
            raise SemanticError(
                f"value parameter '{key}' of '{declaration.module}' cannot "
                "receive a type argument; a compile-time integer is required"
            )
        if isinstance(value, int):
            return value
        text = str(value)
        parent = next(
            (
                item
                for item in self.context.module.parameters
                if item.name == text and item.kind == "value"
            ),
            None,
        )
        if parent is not None and isinstance(parent.default, int):
            return parent.default
        try:
            return self.context.type_resolver._eval_constant_integer(
                text,
                description=f"value parameter '{key}'",
                allow_zero=True,
                allow_negative=True,
            )
        except SemanticError as value_error:
            try:
                self.context.type_resolver.resolve(ast.TypeName(text))
            except SemanticError:
                raise value_error
            raise SemanticError(
                f"value parameter '{key}' of '{declaration.module}' cannot "
                "receive a type argument"
            ) from value_error

    def _infer_scalar_arguments(
        self,
        declaration: ast.InstanceDecl,
        child: ast.Module,
        resolved: dict[str, int | str],
        types: dict[str, ir_types.HardwareType],
    ) -> None:
        unresolved = {
            parameter.name
            for parameter in child.parameters
            if parameter.kind in {"type", "value"}
            and parameter.name not in resolved
        }
        if not unresolved or declaration.array_length is not None:
            return
        scalar_inputs: list[tuple[str, ast.TypeSyntax]] = []
        for port in child.ports:
            if port.direction is not ast.Direction.INPUT:
                continue
            syntax = port.type_name
            if isinstance(syntax, ast.InterfaceTypeName):
                if syntax.kind is not ast.InterfaceKind.WIRE:
                    continue
                payload = syntax.payload_type
            else:
                payload = syntax
            scalar_inputs.extend((name, payload) for name in port.names or (port.name,))
        bindings: dict[str, ast.Expression] = {}
        candidates = tuple(
            (*declaration.bindings, *(
                ast.Assignment(
                    assignment.target.split(".", 1)[1], assignment.expression
                )
                for assignment in self.context.module.assignments
                if assignment.target.startswith(declaration.name + ".")
                and assignment.target.count(".") == 1
            ))
        )
        for binding in candidates:
            if binding.target in bindings:
                return
            bindings[binding.target] = binding.expression
        if not scalar_inputs or not all(name in bindings for name, _ in scalar_inputs):
            return
        parameter_map = {item.name: item for item in child.parameters}
        value_bindings = {
            name: value for name, value in resolved.items() if isinstance(value, int)
        }
        inference_resolver = type_resolution.TypeResolver(
            child.type_aliases,
            child.structs,
            child.enums,
            child.parameters,
            {
                **{
                    item.name: item.default
                    for item in child.parameters
                    if item.default is not None
                },
                **value_bindings,
            },
            types,
            self.context.enum_identity_namespace,
            tagged_unions=child.tagged_unions,
        )
        self.context.expression_context.services.callable_specializer.bind_inferred_types(
            parameter_map,
            types,
            value_bindings,
            inference_resolver,
            tuple(
                (
                    formal_type,
                    self.context.expression_context.expressions.check(
                        bindings[port_name],
                        self.context.value_symbols,
                        None,
                        self.context.expression_context,
                    ).type,
                )
                for port_name, formal_type in scalar_inputs
            ),
            module_types=True,
        )
        resolved.update((name, str(type_)) for name, type_ in types.items())
        resolved.update(value_bindings)

    def _apply_parameter_defaults(
        self,
        declaration: ast.InstanceDecl,
        child: ast.Module,
        resolved: dict[str, int | str],
        types: dict[str, ir_types.HardwareType],
    ) -> None:
        for parameter in child.parameters:
            if parameter.name in resolved or parameter.kind in {"constant", "callable"}:
                continue
            if parameter.kind == "type":
                raise SemanticError(
                    f"missing required type argument '{parameter.name}' for "
                    f"'{declaration.module}'; exact specialization inference "
                    "is ambiguous"
                )
            if parameter.default is None:
                raise SemanticError(
                    f"missing required value argument '{parameter.name}' for "
                    f"'{declaration.module}'"
                )
            default = parameter.default
            if isinstance(default, str):
                default = self.context.type_resolver._eval_constant_integer(
                    default,
                    description=f"value parameter '{parameter.name}'",
                    allow_zero=True,
                    allow_negative=True,
                    local_values={
                        name: value
                        for name, value in resolved.items()
                        if isinstance(value, int)
                    },
                    allow_resolver_parameters=False,
                )
            assert isinstance(default, int)
            resolved[parameter.name] = default
        for name in types:
            resolved[name] = str(types[name])

    def _bind_deferred_arguments(
        self,
        child: ast.Module,
        resolved: dict[str, int | str],
        types: dict[str, ir_types.HardwareType],
        deferred: list[ast.SpecializationArgument],
    ) -> InstanceSpecialization:
        for argument in deferred:
            assert argument.name is not None
            parameter = next(
                item for item in child.parameters if item.name == argument.name
            )
            if parameter.kind != "constant":
                continue
            name = (
                argument.value.text
                if isinstance(argument.value, ast.TypeName)
                else argument.value
            )
            if isinstance(name, str):
                self.resolve_parent_constant(name)
        deferred_names = {item.name for item in deferred}
        complete: list[ast.SpecializationArgument] = []
        for parameter in child.parameters:
            if parameter.name in deferred_names:
                complete.append(next(
                    item for item in deferred if item.name == parameter.name
                ))
            elif parameter.kind == "type":
                complete.append(ast.SpecializationArgument(
                    parameter.name, ast.TypeName(str(types[parameter.name]))
                ))
            elif parameter.kind == "value":
                complete.append(ast.SpecializationArgument(
                    parameter.name, int(resolved[parameter.name])
                ))
        binding_declaration = ast.FunctionDecl(
            f"__module_specialization_{child.name}",
            (),
            ast.TypeName("bit"),
            ast.NumberExpr(0),
            child.parameters,
            source_identity=child.source_identity,
        )
        _, _, constants, callables = (
            self.context.expression_context.services.callable_specializer.bindings(
                binding_declaration,
                tuple(complete),
                (),
                self.context.expression_context,
                self.context.value_symbols,
            )
        )
        dependency = (
            self.context.expression_context.environment.generic_dependency_identity
        )
        for name, expression in constants.items():
            binding = self.context.expression_context.services.callable_specializer.specialization_binding(
                name, expression, dependency
            )
            resolved[name] = f"constant:{expression.type}:{binding.content_hash}"
        for name, binding in callables.items():
            metadata = self.context.expression_context.services.callable_specializer.specialization_binding(
                name, binding, dependency
            )
            resolved[name] = f"callable:{metadata.content_hash}"
        specialized_parameters = tuple(
            replace(
                parameter,
                default=(
                    resolved.get(parameter.name, parameter.default)
                    if parameter.kind == "value"
                    else parameter.default
                ),
            )
            for parameter in child.parameters
        )
        specializations = tuple(
            ir_module.Specialization(
                name=parameter.name,
                value=resolved[parameter.name],
            )
            for parameter in child.parameters
        )
        return (
            resolved,
            types,
            constants,
            callables,
            specialized_parameters,
            specializations,
        )


@dataclass(frozen=True)
class InstanceElaborationContext:
    """Recursive-analysis inputs used only while elaborating child modules."""

    specializer: InstanceSpecializer
    recursive_analyze: Callable[..., ir_module.Module]
    analysis: semantic_context.AnalysisContext
    preparation: module_pipeline.DeclarationPreparationProduct
    active_instance_stack: tuple[str, ...]
    hierarchy_cache: object
    analysis_needs: object


@dataclass(frozen=True)
class InstanceElaborationProduct:
    """Typed child modules and physical instance identities."""

    instances: tuple[ir_module.Instance, ...]
    child_irs: tuple[tuple[str, ir_module.Module], ...]
    elaborated_instances: tuple[ir_module.ElaboratedInstance, ...]
    instance_names: frozenset[str]


class InstanceElaborator:
    """Own recursive child analysis and specialized output validation."""

    def __init__(self, context: InstanceElaborationContext) -> None:
        self.context = context
        self.base = context.specializer.context
        self.instances: list[ir_module.Instance] = []
        self.child_irs: dict[str, ir_module.Module] = {}
        self.elaborated: list[ir_module.ElaboratedInstance] = []
        self.names: set[str] = set()

    def analyze(self) -> InstanceElaborationProduct:
        for declaration in self.base.module.instances:
            self._elaborate(declaration)
        return InstanceElaborationProduct(
            tuple(self.instances),
            tuple(self.child_irs.items()),
            tuple(self.elaborated),
            frozenset(self.names),
        )

    def _elaborate(self, declaration: ast.InstanceDecl) -> None:
        if declaration.name in self.names or declaration.name in self.base.symbols:
            raise SemanticError(f"duplicate instance '{declaration.name}'")
        if declaration.module not in self.context.specializer.known_modules:
            raise SemanticError(
                f"unknown instantiated module '{declaration.module}'"
            )
        array_length = self._array_length(declaration)
        self.names.add(declaration.name)
        (
            resolved_arguments,
            resolved_types,
            constants,
            callables,
            specialized_parameters,
            specialization_tuple,
        ) = self.context.specializer.resolve(declaration)
        child_ast = self.context.specializer.specialized_child(
            declaration, specialized_parameters
        )
        semantic_observations.record_module_definition(
            self.base.expression_context, declaration, child_ast
        )
        child_ir = self._analyze_child(
            child_ast, resolved_types, constants, callables
        )
        if array_length is not None:
            self._validate_array_child(declaration.name, child_ir)
        physical_names = (
            tuple(
                f"{declaration.name}[{index}]" for index in range(array_length)
            )
            if array_length is not None
            else (declaration.name,)
        )
        self.child_irs[declaration.name] = child_ir
        for physical_name in physical_names:
            self.names.add(physical_name)
            instance = ir_module.Instance(
                name=physical_name,
                module=declaration.module,
                specializations=specialization_tuple,
                array_length=None,
            )
            self.instances.append(instance)
            self.child_irs[physical_name] = child_ir
        instance_clock, instance_reset = self._matched_domain(
            declaration.name, child_ir
        )
        specialization_identity = hashlib.sha256(
            f"{declaration.module}|{tuple(sorted(resolved_arguments.items()))}".encode()
        ).hexdigest()[:24]
        for physical_name in physical_names:
            instance = next(
                item for item in self.instances if item.name == physical_name
            )
            self.elaborated.append(ir_module.ElaboratedInstance(
                instance=instance,
                child_module=child_ir.name,
                clock=instance_clock,
                reset=instance_reset,
                instance_identity=hashlib.sha256(
                    f"{self.base.module.name}|{physical_name}|"
                    f"{declaration.module}|"
                    f"{tuple(sorted(resolved_arguments.items()))}".encode()
                ).hexdigest()[:24],
                semantic_path=(self.base.module.name, physical_name),
                specialization_identity=specialization_identity,
            ))
            _validate_child_outputs(self.base, physical_name, child_ir)
            _validate_child_protocol_outputs(self.base, physical_name, child_ir)
            _project_child_csr_outputs(self.base, physical_name, child_ir)
        self._publish_child_value(declaration, child_ir, array_length)

    def _array_length(self, declaration: ast.InstanceDecl) -> int | None:
        length = (
            compile_time_evaluation.resolve_range_bound(
                declaration.array_length,
                self.base.expression_context,
                "instance array",
            )
            if declaration.array_length is not None
            else None
        )
        if length is not None and length < 1:
            raise SemanticError("instance array length must be positive")
        if length is not None and declaration.bindings:
            raise SemanticError(
                f"instance array '{declaration.name}' cannot use an inline binding "
                "block; bind each physical element with a compile-time indexed "
                "generate block"
            )
        if length is not None:
            self.base.expression_context.scope.instance_arrays[
                declaration.name
            ] = length
        return length

    def _analyze_child(
        self,
        child: ast.Module,
        resolved_types: dict[str, ir_types.HardwareType],
        constants: dict[str, ir_expr.Expression],
        callables: dict[str, semantic_callables.StaticCallableBinding],
    ) -> ir_module.Module:
        context = self.context
        base = self.base
        analysis = context.analysis
        preparation = context.preparation
        return context.recursive_analyze(
            child,
            exploration_results=analysis.implementation.exploration_results,
            formal_config=analysis.verification.formal_config,
            formal_verifier=analysis.verification.formal_verifier,
            specialization_type_bindings=resolved_types,
            specialization_constant_bindings=constants,
            specialization_callable_bindings=callables,
            compile_time_budget=preparation.compile_time_budget,
            _compile_time_real_quantize_cache=preparation.real_quantize_cache,
            source_unit=base.source_unit,
            source_digest=base.source_digest,
            allow_external_enum_inputs=True,
            enum_identity_namespace=base.enum_identity_namespace,
            resolution_context=preparation.resolution_context,
            root_module_identity=dependency_identity_for_source(
                child.source_identity,
                analysis.resolution.root_module_identity,
                analysis.resolution.dependency_closure,
            ),
            dependency_closure=analysis.resolution.dependency_closure,
            _imports_premerged=True,
            inherited_domain=(
                base.clock_domains[0]
                if len(base.clock_domains) == 1 and _child_needs_domain(child)
                else None
            ),
            _instance_stack=context.active_instance_stack,
            _hierarchy_cache=context.hierarchy_cache,
            analysis_needs=context.analysis_needs,
            definition_resolutions=analysis.tooling.definition_resolutions,
            definition_declarations=analysis.tooling.definition_declarations,
            completion_scopes=analysis.tooling.completion_scopes,
            signature_help_calls=analysis.tooling.signature_help_calls,
        )

    def _validate_array_child(
        self, name: str, child: ir_module.Module
    ) -> None:
        wire_only = all(
            port.protocol is ir_interfaces.InterfaceProtocol.WIRE
            for port in child.ports
        )
        ready_valid = any(
            port.protocol is ir_interfaces.InterfaceProtocol.READY_VALID
            for port in child.ports
        ) and all(
            port.protocol in {
                ir_interfaces.InterfaceProtocol.WIRE,
                ir_interfaces.InterfaceProtocol.READY_VALID,
            }
            for port in child.ports
        )
        if child.aggregate_protocol_endpoints:
            raise SemanticError(
                f"instance array '{name}' does not support aggregate protocol children"
            )
        _validate_request_response_array(name, child, wire_only)
        if not wire_only and not ready_valid:
            raise SemanticError(
                f"instance array '{name}' supports either wire-only children "
                "or mixed scalar/primitive ready-valid children; mixed or "
                "other protocol ports are not supported"
            )
        storage_count = len(child.fifos) + len(child.memories) + len(child.roms)
        scheduled = any(fifo.scheduled for fifo in child.fifos) or any(
            memory.scheduled for memory in child.memories
        )
        if storage_count and (
            child.registers or child.next_assignments or child.rules
        ) and not scheduled:
            raise SemanticError(
                f"instance array '{name}' does not support a legacy globally "
                "controlled storage-owning child combined with user registers, "
                "next-state assignments, or rules; use scheduled storage through "
                "ResolvedTransition"
            )
        if ready_valid and (child.memories or child.roms):
            raise SemanticError(
                f"ready/valid instance array '{name}' currently supports one "
                "FIFO but not synchronous memory or initialized ROM children"
            )
        if ready_valid and len(child.fifos) > 1:
            raise SemanticError(
                f"ready/valid instance array '{name}' currently supports at "
                "most one FIFO resource"
            )
        if wire_only and storage_count > 1:
            raise SemanticError(
                f"instance array '{name}' currently supports exactly one FIFO, "
                "synchronous memory, or initialized ROM resource"
            )
        if child.is_multi_clock:
            raise SemanticError(
                f"instance array '{name}' must share exactly one synchronous "
                "clock/reset domain; CDC children are not supported"
            )
        if ready_valid:
            if len(child.clock_domains) != 1:
                raise SemanticError(
                    f"ready/valid instance array '{name}' requires exactly one "
                    "synchronous clock/reset domain"
                )
            if any(
                connection.buffer_depth
                or connection.adapter is not None
                or connection.crossing is not None
                for connection in child.connections
            ):
                raise SemanticError(
                    f"ready/valid instance array '{name}' does not support child "
                    "buffering, adapters, or crossings"
                )
        if child.instances or child.elaborated_instances:
            _HIERARCHY_ANALYZER.validate_nested_instance_array_child(
                name,
                child,
                hierarchy_cache=self.context.hierarchy_cache,
            )

    def _matched_domain(
        self, name: str, child: ir_module.Module
    ) -> tuple[str | None, str | None]:
        if not child.is_sequential:
            return None, None
        matched: list[ir_cdc.ClockDomain] = []
        for child_domain in child.clock_domains:
            matches = tuple(
                domain for domain in self.base.clock_domains
                if domain == child_domain
            )
            if len(matches) != 1:
                available = ", ".join(
                    item.clock for item in self.base.clock_domains
                )
                raise SemanticError(
                    f"child '{name}' physical clock/reset contract "
                    f"({child_domain.clock}, {child_domain.reset}) does not match "
                    f"one exact parent domain; available domains: {available}"
                )
            matched.append(matches[0])
        return (
            (matched[0].clock, matched[0].reset)
            if len(matched) == 1
            else (None, None)
        )

    def _publish_child_value(
        self,
        declaration: ast.InstanceDecl,
        child: ir_module.Module,
        array_length: int | None,
    ) -> None:
        fields = tuple(
            ir_types.StructField(port.name, port.type) for port in child.outputs
        )
        if not fields or array_length is not None:
            return
        type_ = ir_types.StructType(f"__instance_{declaration.name}", fields)
        self.base.value_symbols[declaration.name] = ir_module.LocalValue(
            declaration.name,
            type_,
            ir_expr.InputRef(declaration.name, type_),
        )


def _child_needs_domain(child: ast.Module) -> bool:
    return bool(
        child.registers
        or child.next_assignments
        or child.rules
        or child.fifos
        or child.memories
        or child.roms
        or child.csr_blocks
        or any(
            isinstance(port.type_name, ast.InterfaceTypeName)
            for port in child.ports
        )
        or child.connections
        or child.connection_chains
    )


def _validate_request_response_array(
    name: str, child: ir_module.Module, wire_only: bool
) -> None:
    if not child.request_responses:
        return
    if len(child.request_responses) != 1:
        raise SemanticError(
            f"instance array '{name}' currently supports exactly one "
            "request/response interface per child"
        )
    if not wire_only:
        raise SemanticError(
            f"instance array '{name}' request/response children may expose "
            "only scalar wire ports beside the request/response interface"
        )
    interface = child.request_responses[0]
    if interface.max_outstanding <= 0 or (
        interface.ordering is not ir_interfaces.RequestResponseOrdering.IN_ORDER
    ):
        raise SemanticError(
            f"instance array '{name}' supports only positive max_outstanding "
            "with ordering in_order"
        )


def _validate_child_outputs(
    base: InstanceAnalysisContext,
    physical_name: str,
    child: ir_module.Module,
) -> None:
    context = base.expression_context
    for port in child.outputs:
        key = (physical_name, port.name)
        context.scope.instance_outputs[key] = port.type
        context.scope.instance_output_protocols[key] = port.protocol
        context.scope.instance_output_domains[key] = port.domain


def _validate_child_protocol_outputs(
    base: InstanceAnalysisContext,
    physical_name: str,
    child: ir_module.Module,
) -> None:
    for port in child.ports:
        if port.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID:
            continue
        logical_bases = [port.name]
        logical_bases.extend(
            f"{aggregate.name}.{member.name}"
            for aggregate in child.aggregate_protocol_endpoints
            for member in aggregate.members
            if port.name == f"{aggregate.name}__{member.name}"
        )
        outgoing = (
            (
                ir_interfaces.ReadyValidSignal.PAYLOAD,
                ir_interfaces.ReadyValidSignal.VALID,
            )
            if port.direction is ir_module.PortDirection.OUTPUT
            else (ir_interfaces.ReadyValidSignal.READY,)
        )
        for signal in outgoing:
            scalar_name = ir_interfaces.ready_valid_field_name(port.name, signal)
            signal_type = (
                port.type
                if signal is ir_interfaces.ReadyValidSignal.PAYLOAD
                else ir_types.BitType()
            )
            projection = (scalar_name, signal_type, port.domain)
            for logical_base in logical_bases:
                key = (physical_name, f"{logical_base}.{signal.value}")
                base.expression_context.scope.instance_protocol_outputs[key] = projection


def _project_child_csr_outputs(
    base: InstanceAnalysisContext,
    physical_name: str,
    child: ir_module.Module,
) -> None:
    projections = base.expression_context.scope.instance_csr_state_paths
    for block in child.csr_blocks:
        _project_csr_bindings(physical_name, block, projections)
        _project_csr_observations(physical_name, block, projections)
        for register in block.registers:
            for event in register.events:
                projections[
                    (
                        physical_name,
                        block.name,
                        "events",
                        register.name,
                        event.name,
                    )
                ] = (
                    ir_csr.csr_event_port_name(event),
                    event.canonical_type,
                    block.domain,
                )
        _project_split_views(physical_name, block, projections)


def _project_csr_bindings(
    physical_name: str, block: Any, projections: dict[tuple[str, ...], tuple]
) -> None:
    for binding in block.state_bindings:
        block_name, register_name, field_name = ir_csr.csr_named_state_path(
            block, binding
        )
        projection = (
            ir_csr.csr_state_port_name(binding),
            binding.canonical_type,
            block.domain,
        )
        key = (physical_name, block_name, register_name, field_name)
        projections[key] = projection
        projections[
            (physical_name, block_name, "state", register_name, field_name)
        ] = projection
        register = block.registers[
            binding.csr_field_id.register.declaration_ordinal
        ]
        if register.projection_path:
            projections[
                (physical_name, block_name, *register.projection_path, field_name)
            ] = projection
            projections[
                (
                    physical_name,
                    block_name,
                    "state",
                    *register.projection_path,
                    field_name,
                )
            ] = projection


def _project_csr_observations(
    physical_name: str, block: Any, projections: dict[tuple[str, ...], tuple]
) -> None:
    fields_by_identity = {
        field.identity: (register, field)
        for register in block.registers
        for field in register.fields
    }
    for observation in block.access_observations:
        register, field = fields_by_identity[observation.csr_field_id]
        entries = (
            ("read_hit", ir_csr.csr_read_hit_port_name(observation), ir_types.BitType()),
            (
                "write_hit",
                ir_csr.csr_observation_write_hit_port_name(observation),
                ir_types.BitType(),
            ),
            (
                "write_value",
                ir_csr.csr_observation_write_value_port_name(observation),
                observation.canonical_type,
            ),
            (
                "value",
                ir_csr.csr_observation_value_port_name(observation),
                observation.canonical_type,
            ),
        )
        for leaf, port_name, type_ in entries:
            projections[
                (
                    physical_name,
                    block.name,
                    "events",
                    register.name,
                    field.name,
                    leaf,
                )
            ] = (port_name, type_, block.domain)
        if field.access is ir_csr.CsrAccess.READ_ONLY:
            projections[
                (
                    physical_name,
                    block.name,
                    "status",
                    register.name,
                    field.name,
                )
            ] = (
                ir_csr.csr_observation_value_port_name(observation),
                observation.canonical_type,
                block.domain,
            )


def _project_split_views(
    physical_name: str, block: Any, projections: dict[tuple[str, ...], tuple]
) -> None:
    for view in block.split_views:
        projection = (
            ir_csr.csr_split_port_name(block, view),
            view.canonical_type,
            block.domain,
        )
        projections[
            (physical_name, block.name, view.name, view.field_name)
        ] = projection
        if view.projection_path:
            projections[
                (
                    physical_name,
                    block.name,
                    *view.projection_path,
                    view.field_name,
                )
            ] = projection
            projections[
                (
                    physical_name,
                    block.name,
                    "state",
                    *view.projection_path,
                    view.field_name,
                )
            ] = projection
        projections[
            (
                physical_name,
                block.name,
                "state",
                view.name,
                view.field_name,
            )
        ] = projection
