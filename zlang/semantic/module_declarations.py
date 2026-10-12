"""Name resolution and type checking for ZLang HDL."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import hierarchy as ir_hierarchy
from zlang.ir.constants import constant_runtime_value
from zlang.ir import callables as ir_callables
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import types as ir_types
from zlang.definition_resolution import DefinitionTarget
from .errors import SemanticError
from . import context as semantic_context
from . import callables as semantic_callables
from . import compile_time_evaluation
from . import concise_module_items
from . import imports as semantic_imports
from . import limits as semantic_limits
from . import module_preparation
from . import module_pipeline
from . import module_interfaces as semantic_module_interfaces
from . import module_validation
from . import observations as _observations
from . import expression_origins
from . import expression_operators
from . import type_resolution


def _resolved_module_value_parameters(
    module: ast.Module, resolver: type_resolution.TypeResolver
) -> tuple[dict[str, int], frozenset[str]]:
    """Resolve the module's one canonical compile-time value environment."""

    # Defaults are declaration-ordered.  The resolver is initially populated
    # with the source defaults so type syntax can be collected before this
    # pass; do not let that bootstrap environment make a later parameter
    # visible to an earlier default.  Concrete values are installed again as
    # each declaration is resolved.
    value_parameter_names = {
        parameter.name
        for parameter in module.parameters
        if parameter.kind == "value"
    }
    for name in value_parameter_names:
        resolver._parameter_values.pop(name, None)

    values: dict[str, int] = {}
    unresolved: set[str] = set()
    for parameter in module.parameters:
        if parameter.kind != "value":
            continue
        if parameter.default is None:
            unresolved.add(parameter.name)
            continue
        if isinstance(parameter.default, int):
            value = parameter.default
        else:
            value = resolver._eval_constant_integer(
                str(parameter.default),
                description=f"value parameter '{parameter.name}'",
                allow_zero=True,
                allow_negative=True,
            )
        values[parameter.name] = value
        resolver._parameter_values[parameter.name] = value
    return values, frozenset(unresolved)


def _check_module_parameter_constraint(
    module: ast.Module,
    type_resolver: type_resolution.TypeResolver,
    parameter_values: dict[str, int],
    unresolved_parameter_names: frozenset[str],
    source_unit: str | None,
    source_digest: str | None,
) -> None:
    """Reject invalid specializations before resolving dependent port types."""

    if module.parameter_constraint is None:
        return
    constraint_context = semantic_context.ExpressionContext(
        semantic_context.AnalysisEnvironment(
            {},
            parameters=parameter_values,
            unresolved_parameters=unresolved_parameter_names,
            type_resolver=type_resolver,
        ),
        semantic_context.AnalysisServices(),
        semantic_context.ExpressionScope(
            allow_delay=False,
            source_unit=source_unit,
            source_digest=source_digest,
        ),
    )
    try:
        constraint_satisfied = compile_time_evaluation.compile_time_condition(
            module.parameter_constraint, {}, constraint_context
        )
    except SemanticError as error:
        raise SemanticError(
            f"module '{module.name}' parameter constraint cannot be "
            f"discharged: {error}",
            code="ZL-SEMANTIC-PARAMETER-CONSTRAINT",
            primary=expression_origins.semantic_origin(module.parameter_constraint, constraint_context),
            fixes=("provide concrete type/value specialization arguments",),
        ) from error
    if not constraint_satisfied:
        raise SemanticError(
            f"module '{module.name}' parameter constraint is not satisfied",
            code="ZL-SEMANTIC-PARAMETER-CONSTRAINT",
            primary=expression_origins.semantic_origin(module.parameter_constraint, constraint_context),
            notes=(
                "resolved values: "
                + ", ".join(
                    f"{name}={value}"
                    for name, value in sorted(parameter_values.items())
                ),
            ),
        )


def _resolved_module_parameter_records(
    module: ast.Module,
    specialization_type_bindings: dict[str, ir_types.HardwareType] | None,
    specialization_constant_bindings: dict[str, ir_expr.Expression] | None,
    specialization_callable_bindings: dict[str, semantic_callables.StaticCallableBinding] | None,
) -> tuple[tuple[str, str, int | str | None], ...]:
    """Freeze the exact compile-time specialization arguments once.

    Candidate-site ownership and the final typed ``Module.parameters`` must use
    the same payload.  Keeping that payload in one helper prevents formal-aware selection from
    falling back to a source-module name when two child specializations coexist.
    """

    return tuple(
        (
            parameter.name,
            parameter.kind,
            (
                str(specialization_type_bindings[parameter.name])
                if parameter.kind == "type"
                and specialization_type_bindings is not None
                and parameter.name in specialization_type_bindings
                else parameter.default
                if parameter.kind in {"type", "value"}
                else (
                    "constant:"
                    + str(specialization_constant_bindings[parameter.name].type)
                    + ":"
                    + hashlib.sha256(
                        repr((
                            str(specialization_constant_bindings[parameter.name].type),
                            constant_runtime_value(
                                specialization_constant_bindings[parameter.name]
                            ),
                        )).encode("utf-8")
                    ).hexdigest()
                    if parameter.kind == "constant"
                    and specialization_constant_bindings is not None
                    and parameter.name in specialization_constant_bindings
                    else (
                        "callable:"
                        + specialization_callable_bindings[
                            parameter.name
                        ].callee_identity
                        if parameter.kind == "callable"
                        and specialization_callable_bindings is not None
                        and parameter.name in specialization_callable_bindings
                        else (
                            str(parameter.type_name)
                            if parameter.kind == "constant"
                            else "fn("
                            + ",".join(map(str, parameter.callable_parameters))
                            + ")->"
                            + str(parameter.callable_return_type)
                        )
                    )
                )
            ),
        )
        for parameter in module.parameters
    )


_CALLABLE_DEPENDENCY_VALIDATOR = semantic_callables.CallableDependencyValidator()

class DeclarationAndCallablePreparer:
    """Own declaration/type/callable preparation before hardware analysis."""

    def prepare(
        self,
        context: semantic_context.AnalysisContext,
    ) -> module_pipeline.DeclarationPreparationProduct:
        specialization_type_bindings = context.specialization.type_bindings
        specialization_constant_bindings = context.specialization.constant_bindings
        specialization_callable_bindings = context.specialization.callable_bindings
        _compile_time_real_quantize_cache = context.compile_time.real_quantize_cache
        enum_identity_namespace = context.source.enum_identity_namespace
        import_product = semantic_imports.ImportAnalyzer().analyze(context)
        module = import_product.module
        resolved_imports = import_product.resolved_imports
        source_digests = dict(import_product.source_digests)
        generic_dependency_identity = import_product.generic_dependency_identity
        effective_source_unit = import_product.source_unit
        effective_source_digest = import_product.source_digest
        enum_identity_namespace = import_product.enum_identity_namespace
        module = semantic_module_interfaces.NamedInterfaceAnalyzer().analyze(module)

        module_validation.validate_compile_time_parameter_declarations(module)
        protocol_schemas: list[ir_module.ProtocolSchema] = []
        type_resolver = type_resolution.TypeResolver(
            module.type_aliases,
            module.structs,
            module.enums,
            module.parameters,
            {
                parameter.name: parameter.default
                for parameter in module.parameters
                if parameter.default is not None
            },
            specialization_type_bindings,
            enum_identity_namespace,
            tagged_unions=module.tagged_unions,
        )
        module = concise_module_items.normalize_concise_module_items(
            module,
            type_resolver,
            context.hierarchy.inherited_domain,
            source_unit=effective_source_unit,
            source_digest=effective_source_digest,
        )
        module_validation.validate_value_parameter_shadowing(module)
        parameter_values, unresolved_parameter_names = _resolved_module_value_parameters(
            module, type_resolver
        )
        # Subsequent structural elaboration sees concrete values, including
        # values that were themselves defined by parameter expressions.
        type_resolver._parameter_values.update(parameter_values)
        _check_module_parameter_constraint(
            module, type_resolver, parameter_values, unresolved_parameter_names,
            effective_source_unit, effective_source_digest,
        )
        module = replace(
            module,
            parameters=tuple(
                replace(parameter, default=parameter_values[parameter.name])
                if parameter.kind == "value" and parameter.name in parameter_values
                else parameter
                for parameter in module.parameters
            ),
        )
        resolved_module_parameters = _resolved_module_parameter_records(
            module,
            specialization_type_bindings,
            specialization_constant_bindings,
            specialization_callable_bindings,
        )
        candidate_site_owner = ir_hierarchy.candidate_specialization_identity(
            module.name, resolved_module_parameters
        )
        selected_budget = context.compile_time.budget or compile_time_evaluation.CompileTimeBudget()
        selected_real_quantize_cache = (
            _compile_time_real_quantize_cache
            if _compile_time_real_quantize_cache is not None
            else {}
        )
        module = module_preparation.select_compile_time_module_items(
            module,
            parameter_values,
            unresolved_parameter_names,
            type_resolver,
            selected_budget,
            functional_range_limit=semantic_limits.FUNCTIONAL_RANGE,
            total_generated_limit=semantic_limits.TOTAL_GENERATED,
        )
        schema_names = {schema.name for schema in protocol_schemas}
        for declaration in module.protocols:
            if declaration.name in schema_names:
                raise SemanticError(f"protocol name conflict '{declaration.name}'")
            roles = tuple(dict.fromkeys(declaration.roles))
            if len(roles) != len(declaration.roles) or len(roles) < 2:
                raise SemanticError(f"protocol '{declaration.name}' must declare at least two distinct roles")
            members: list[ir_module.ProtocolMember] = []
            member_names: set[str] = set()
            protocol_type_resolver = type_resolution.TypeResolver(
                module.type_aliases,
                module.structs,
                module.enums,
                declaration.parameters,
                {
                    parameter.name: parameter.default
                    for parameter in declaration.parameters
                    if parameter.default is not None
                },
                identity_namespace=(
                    effective_source_unit or module.source_identity or module.name
                ),
                tagged_unions=module.tagged_unions,
            )
            for channel in declaration.channels:
                if channel.name in member_names:
                    raise SemanticError(f"duplicate protocol channel '{channel.name}'")
                if channel.source_role not in roles or channel.sink_role not in roles or channel.source_role == channel.sink_role:
                    raise SemanticError(f"invalid ownership for protocol channel '{channel.name}'")
                if isinstance(channel.type_name, ast.InterfaceTypeName):
                    protocol = semantic_module_interfaces.interface_protocol(
                        channel.type_name.kind
                    )
                    try:
                        payload = protocol_type_resolver.resolve(channel.type_name.payload_type)
                    except SemanticError:
                        if isinstance(channel.type_name.payload_type, ast.TypeName) and any(
                            parameter.kind == "type" and parameter.name == channel.type_name.payload_type.text
                            for parameter in declaration.parameters
                        ):
                            payload = ir_types.BitType()
                        else:
                            raise
                else:
                    protocol = ir_interfaces.InterfaceProtocol.WIRE
                    try:
                        payload = protocol_type_resolver.resolve(channel.type_name)
                    except SemanticError:
                        if isinstance(channel.type_name, ast.TypeName) and any(
                            parameter.kind == "type" and parameter.name == channel.type_name.text
                            for parameter in declaration.parameters
                        ):
                            payload = ir_types.BitType()
                        else:
                            raise
                members.append(ir_module.ProtocolMember(channel.name, protocol, payload, channel.source_role, channel.sink_role, channel.domain))
                member_names.add(channel.name)
            library_path = dict(import_product.protocol_sources).get(declaration.name)
            protocol_schemas.append(
                ir_module.ProtocolSchema(
                    declaration.name,
                    roles,
                    tuple(members),
                    library_path,
                    tuple((parameter.name, parameter.kind, parameter.default) for parameter in declaration.parameters),
                )
            )
            schema_names.add(declaration.name)
        struct_types = type_resolver.resolve_all()
        expression_operators.validate_operator_declarations(module.operators, module.structs)
        reserved_intrinsics = {
            "length", "floor_log2", "ceil_log2", "index_width", "is_power_of_two",
            "pi", "sin", "cos", "log2", "log", "exp", "sqrt", "parity",
            "enum_encode", "enum_valid", "enum_decode",
        }
        for declaration in module.functions:
            if declaration.name in reserved_intrinsics:
                raise SemanticError(
                    f"function name '{declaration.name}' is reserved for a compiler intrinsic"
                )
        for declaration in module.operators:
            if declaration.operator in reserved_intrinsics:
                raise SemanticError(
                    f"operator name '{declaration.operator}' conflicts with a compiler intrinsic"
                )
        function_signatures: dict[str, semantic_callables.FunctionSignature] = {}
        function_prototypes: dict[str, semantic_callables.FunctionPrototype] = {}
        generic_functions: dict[str, ast.FunctionDecl] = {}
        for declaration in module.functions:
            if declaration.name in function_prototypes or declaration.name in generic_functions:
                raise SemanticError(f"duplicate function '{declaration.name}'")
            if declaration.generic_parameters:
                generic_functions[declaration.name] = declaration
                continue
            parameter_names: set[str] = set()
            parameters: list[ir_module.FunctionParameter] = []
            for parameter in declaration.parameters:
                if parameter.name in parameter_names:
                    raise SemanticError(
                        f"duplicate parameter '{parameter.name}' in function "
                        f"'{declaration.name}'"
                    )
                parameter_names.add(parameter.name)
                parameters.append(
                    ir_module.FunctionParameter(
                        parameter.name, type_resolver.resolve(parameter.type_name)
                    )
                )
            declared_return_type = (
                type_resolver.resolve(declaration.return_type)
                if declaration.return_type is not None
                else None
            )
            prototype = semantic_callables.FunctionPrototype(
                declaration,
                tuple(parameters),
                declared_return_type,
            )
            function_prototypes[declaration.name] = prototype
            if declared_return_type is not None:
                function_signatures[declaration.name] = semantic_callables.FunctionSignature(
                    declaration,
                    prototype.parameters,
                    declared_return_type,
                )

        function_catalog = semantic_callables.CallableRepository(
            function_prototypes,
            function_signatures,
        )

        pure_context = semantic_context.ExpressionContext(
            semantic_context.AnalysisEnvironment(
                function_signatures,
                generic_functions=generic_functions,
                function_catalog=function_catalog,
                operator_declarations=module.operators,
                struct_declarations=module.structs,
                structs=struct_types,
                parameters=parameter_values,
                unresolved_parameters=unresolved_parameter_names,
                type_resolver=type_resolver,
                generic_dependency_identity=generic_dependency_identity,
                formal_config=context.verification.formal_config,
                formal_verifier=context.verification.formal_verifier,
                source_digests=source_digests,
            ),
            semantic_context.AnalysisServices(
                callables=semantic_callables.CallableSpecializationCache(
                    callable_definitions=semantic_callables._inherited_static_callable_definitions(
                        specialization_callable_bindings
                    )
                ),
                compile_time_budget=selected_budget,
                compile_time_real_quantize_cache=selected_real_quantize_cache,
                exploration_results=context.implementation.exploration_results,
                intent_structural_cache=context.implementation.structural_cache,
                intent_exploration_limits=context.implementation.exploration_limits,
                tooling=context.tooling,
            ),
            semantic_context.ExpressionScope(
                allow_delay=False,
                compile_time_constants=dict(specialization_constant_bindings or {}),
                static_callables=dict(specialization_callable_bindings or {}),
                candidate_site_owner=candidate_site_owner,
                source_unit=effective_source_unit,
                source_digest=effective_source_digest,
            ),
        )
        # Named type occurrences are recorded only for definition-aware analyses;
        # ordinary compiler checks keep this side channel completely disabled.
        type_resolver.set_definition_context(pure_context)
        if pure_context.services.tooling.definition_declarations is not None:
            _observations.record_local_resource_definitions(pure_context, module)
            _observations.remember_definition_target(
                pure_context,
                module,
                _observations.module_declaration_origin(module, pure_context),
                name=module.name,
                kind="module",
            )
            for declaration in (*module.functions, *module.operators):
                name = (
                    declaration.name
                    if isinstance(declaration, ast.FunctionDecl)
                    else f"operator{declaration.operator}"
                )
                kind = (
                    "function"
                    if isinstance(declaration, ast.FunctionDecl)
                    else "operator"
                )
                _observations.remember_definition_target(
                    pure_context,
                    declaration,
                    _observations.declaration_origin(
                        declaration.name_origin or declaration.origin,
                        f"{kind} {name}",
                        pure_context,
                        source_unit=declaration.source_identity,
                    ),
                    name=name,
                    kind=kind,
                )
            for declaration in (*module.type_aliases, *module.structs, *module.enums):
                kind = "enum" if isinstance(declaration, ast.EnumDecl) else "type"
                name_origin = getattr(declaration, "name_origin", None)
                declaration_origin = getattr(declaration, "origin", None)
                target = _observations.declaration_origin(
                    name_origin or declaration_origin,
                    f"{kind} {declaration.name}",
                    pure_context,
                    source_unit=getattr(declaration, "source_identity", None),
                )
                if target is not None:
                    _observations.remember_definition_target(
                        pure_context,
                        declaration,
                        target,
                        name=declaration.name,
                        kind=kind,
                    )
                if isinstance(declaration, ast.EnumDecl):
                    for member, member_span in zip(
                        declaration.members,
                        declaration.member_origins,
                        strict=False,
                    ):
                        if member_span is None:
                            continue
                        member_target = _observations.declaration_origin(
                            member_span,
                            f"enum member {declaration.name}.{member}",
                            pure_context,
                            source_unit=declaration.source_identity,
                        )
                        if member_target is None:
                            continue
                        if not any(
                            item.target == member_target
                            and item.name == member
                            and item.kind == "enum_member"
                            for item in pure_context.services.tooling.definition_declarations
                        ):
                            pure_context.services.tooling.definition_declarations.append(
                                DefinitionTarget(member_target, member, "enum_member")
                            )
            # Function/struct/alias signatures are resolved before the expression
            # context exists.  Reuse their parser-owned spans here so definitions
            # are complete without re-running type checking.
            for declaration in module.type_aliases:
                declaration_context = semantic_callables._context_for_source_declaration(
                    pure_context, declaration.source_identity
                )
                type_resolution.record_type_syntax_definitions(
                    declaration_context, declaration.target, type_resolver
                )
            for declaration in module.structs:
                declaration_context = semantic_callables._context_for_source_declaration(
                    pure_context, declaration.source_identity
                )
                for field in declaration.fields:
                    type_resolution.record_type_syntax_definitions(
                        declaration_context, field.type_name, type_resolver
                    )
            for declaration in module.functions:
                declaration_context = semantic_callables._context_for_source_declaration(
                    pure_context, declaration.source_identity
                )
                for parameter in declaration.parameters:
                    type_resolution.record_type_syntax_definitions(
                        declaration_context, parameter.type_name, type_resolver
                    )
                if declaration.return_type is not None:
                    type_resolution.record_type_syntax_definitions(
                        declaration_context, declaration.return_type, type_resolver
                    )
            # Module-interface signatures are resolved independently of whether a
            # source module is selected as the physical top or reached through the
            # selected hierarchy.  Retain those compiler-resolved type occurrences
            # for tooling without analyzing an inactive child body or relaxing any
            # top-level ABI legality rule.
            for child in module.submodules:
                child_context = semantic_callables._context_for_source_declaration(
                    pure_context, child.source_identity
                )
                for port in child.ports:
                    type_resolution.record_type_syntax_definitions(
                        child_context, port.type_name, type_resolver
                    )
        function_catalog.context = pure_context
        for function_name in sorted(function_prototypes):
            function_catalog.resolve(function_name)

        functions: list[ir_module.Function] = []
        for declaration in module.functions:
            if declaration.generic_parameters:
                continue
            signature = function_signatures[declaration.name]
            parameter_symbols = {
                parameter.name: parameter for parameter in signature.parameters
            }
            callable_metadata = ir_callables.source_function_metadata(
                signature.declaration.name,
                signature.declaration.source_identity
                or pure_context.scope.source_unit
                or "<source>",
            )
            callable_identity = ir_callables.stable_callee_identity(
                signature.parameters,
                signature.return_type,
                callable_metadata,
            )
            function_context = semantic_callables._context_for_callable_body(
                pure_context,
                signature.declaration.source_identity,
                callable_identity,
            )
            body = pure_context.services.callable_bodies.check(
                signature.declaration,
                parameter_symbols,
                (
                    signature.return_type
                    if signature.declaration.return_type is not None
                    else None
                ),
                function_context,
                typed_boundary=signature.declaration.return_type is not None,
            )
            if body.type != signature.return_type:
                raise SemanticError(
                    f"function '{signature.declaration.name}' returns {body.type}, "
                    f"expected {signature.return_type}"
                )
            functions.append(
                ir_module.Function(
                    name=signature.declaration.name,
                    parameters=signature.parameters,
                    return_type=signature.return_type,
                    body=body,
                    callee_identity=callable_identity,
                    metadata=callable_metadata,
                )
            )
        # Ordinary function bodies may call monomorphic generic/operator
        # specializations created while those bodies are typed.  Include the
        # concrete definitions in the same recursion graph: considering only the
        # source-named functions leaves a valid ``zlang_spec_*`` edge dangling and
        # can both crash the checker and miss an ordinary<->generic cycle.
        _CALLABLE_DEPENDENCY_VALIDATOR.validate(
            tuple(
                (
                    *functions,
                    *pure_context.services.callables.callable_definitions.values(),
                )
            )
        )
        pure_context.services.callables.function_definitions.update(
            (function.name, function) for function in functions
        )

        return module_pipeline.DeclarationPreparationProduct(
            module,
            import_product.resolution_context,
            tuple(resolved_imports),
            source_digests,
            generic_dependency_identity,
            effective_source_unit,
            effective_source_digest,
            enum_identity_namespace,
            import_product.active_module_identity,
            frozenset(item.logical_path for item in resolved_imports),
            tuple(protocol_schemas),
            type_resolver,
            parameter_values,
            unresolved_parameter_names,
            resolved_module_parameters,
            candidate_site_owner,
            selected_budget,
            selected_real_quantize_cache,
            struct_types,
            type_resolver.resolve_enums(),
            type_resolver.resolve_tagged_unions(),
            function_signatures,
            generic_functions,
            pure_context,
            tuple(functions),
        )
