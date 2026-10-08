# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Source-module normalization performed before semantic analysis."""

from __future__ import annotations

import hashlib
import re
from dataclasses import fields, is_dataclass, replace
from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.ir import cdc as ir_cdc
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin

from . import actions as semantic_actions
from . import compile_time_evaluation
from . import context as semantic_context
from .errors import SemanticError

if TYPE_CHECKING:
    from .type_resolution import TypeResolver


_QUALIFIED_IMPORT_MEMBER = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\."
    r"(?P<member>[A-Za-z_][A-Za-z0-9_]*)"
)


def select_compile_time_module_items(
    module: ast.Module,
    parameter_values: dict[str, int],
    unresolved_parameters: frozenset[str],
    type_resolver: TypeResolver,
    compile_time_budget: compile_time_evaluation.CompileTimeBudget,
    *,
    functional_range_limit: int,
    total_generated_limit: int,
) -> ast.Module:
    """Select compile-time module branches before ordinary module checking."""

    public = (
        ast.PortDecl,
        ast.RequestResponseDecl,
        ast.AggregateInterfaceDecl,
        ast.ProtocolDecl,
        ast.TypeAlias,
        ast.StructDecl,
        ast.ImportDecl,
        ast.ModuleTimingDecl,
    )

    def validate_branch(items: tuple[object, ...]) -> None:
        for item in items:
            if isinstance(item, public) or (
                isinstance(item, tuple) and item and item[0] in {"clock", "reset"}
            ):
                raise SemanticError(
                    "compile-time module if cannot contain public ABI declarations "
                    "(ports, clocks, resets, protocols, aliases, structs, or imports)"
                )
            if isinstance(item, ast.CompileTimeIfDecl):
                validate_branch(item.when_true)
                validate_branch(item.when_false)
            elif isinstance(item, ast.GenerateBlock):
                validate_branch(item.items)

    context = semantic_context.ExpressionContext(
        semantic_context.AnalysisEnvironment(
            {},
            parameters=parameter_values,
            unresolved_parameters=unresolved_parameters,
            type_resolver=type_resolver,
        ),
        semantic_context.AnalysisServices(
            compile_time_budget=compile_time_budget,
        ),
        semantic_context.ExpressionScope(allow_delay=False),
    )
    selected: list[object] = []
    visible_names = {
        *[item.name for item in module.ports],
        *[item.target.split(".", 1)[0] for item in module.assignments],
        *[item.name for item in module.registers],
        *[item.name for item in module.fsms],
        *[item.target for item in module.next_assignments],
        *[item.name for item in module.request_responses],
        *[item.name for item in module.fifos],
        *[item.name for item in module.memories],
        *[item.name for item in module.roms],
        *[item.name for item in module.instances],
        *[item.name for item in module.aggregate_interfaces],
        *[item.name for item in module.generic_declarations],
        *[item.name for item in module.rules if isinstance(item, ast.RuleDecl)],
        *[item.name for item in module.protocols],
        *[item.name for item in module.type_aliases],
        *[item.name for item in module.structs],
        *module.clocks,
        *module.resets,
    }

    def substitute_path(text: str, index: str, replacement: int) -> str:
        pattern = re.compile(
            r"^(?P<head>[A-Za-z_][A-Za-z0-9_]*)"
            r"(?P<tail>(?:\[[^\]]+\]|\.[A-Za-z_][A-Za-z0-9_]*)*)$"
        )
        match = pattern.fullmatch(text)
        if match is None:
            return text
        return re.sub(
            rf"\[{re.escape(index)}\]",
            f"[{replacement}]",
            match.group("head") + match.group("tail"),
        )

    def substitute_index(value: object, index: str, replacement: int) -> object:
        if isinstance(value, ast.NameExpr) and value.name == index:
            return ast.NumberExpr(replacement, origin=value.origin)
        if isinstance(value, tuple):
            return tuple(substitute_index(item, index, replacement) for item in value)
        if is_dataclass(value):
            changes: dict[str, object] = {}
            for field in fields(value):
                if not field.init or field.name == "origin":
                    continue
                item = getattr(value, field.name)
                if field.name in {"target", "source", "destination"} and isinstance(item, str):
                    changes[field.name] = substitute_path(item, index, replacement)
                else:
                    changes[field.name] = substitute_index(item, index, replacement)
            return replace(value, **changes)
        return value

    def select(items: tuple[object, ...], active_binders: tuple[str, ...] = ()) -> None:
        for item in items:
            if isinstance(item, ast.CompileTimeIfDecl):
                validate_branch(item.when_true)
                validate_branch(item.when_false)
                branch = (
                    item.when_true
                    if compile_time_evaluation.compile_time_condition(item.condition, {}, context)
                    else item.when_false
                )
                select(branch, active_binders)
            elif isinstance(item, ast.GenerateBlock):
                if item.index in visible_names or item.index in active_binders:
                    raise SemanticError(
                        f"compile-time generation binder '{item.index}' shadows an existing visible symbol"
                    )
                start = compile_time_evaluation.resolve_range_bound(item.start, context, "generated instance")
                stop = compile_time_evaluation.resolve_range_bound(item.stop, context, "generated instance")
                if stop < start:
                    raise SemanticError(f"generated range {start}..{stop} is reversed")
                length = stop - start
                if length > functional_range_limit:
                    raise SemanticError(
                        f"generated range expands to {length} elements; "
                        f"the compile-time generation limit is {functional_range_limit}"
                    )
                if length:
                    compile_time_budget.generated_elements += length
                    if compile_time_budget.generated_elements > total_generated_limit:
                        raise SemanticError(
                            f"compile-time generation exceeds {total_generated_limit} elements"
                        )
                for value in range(start, stop):
                    select(
                        tuple(
                            substitute_index(child, item.index, value)
                            for child in item.items
                        ),
                        (*active_binders, item.index),
                    )
            else:
                selected.append(item)

    select(module.ordered_items)
    return normalize_selected_module_items(module, tuple(selected), type_resolver)


def _reference_parts(
    type_name: ast.TypeSyntax,
) -> tuple[str, tuple[ast.SpecializationArgument, ...]]:
    if not isinstance(type_name, ast.TypeName):
        return ("", ())
    text = type_name.text
    if "<" not in text:
        return (text, ())
    base, body = text.split("<", 1)
    body = body[:-1]
    values: list[str] = []
    start = 0
    angle_depth = 0
    paren_depth = 0
    for index, character in enumerate(body):
        if character == "<":
            angle_depth += 1
        elif character == ">":
            angle_depth -= 1
        elif character == "(":
            paren_depth += 1
        elif character == ")":
            paren_depth -= 1
        elif character == "," and angle_depth == 0 and paren_depth == 0:
            values.append(body[start:index])
            start = index + 1
    values.append(body[start:])
    return (
        base,
        tuple(
            ast.SpecializationArgument(
                None,
                int(value) if value.isdigit() else value,
            )
            for value in values
        ),
    )


def normalize_concise_module_items(
    module: ast.Module,
    type_resolver: TypeResolver,
    inherited_domain: tuple[str, str] | ir_cdc.ClockDomain | None,
) -> ast.Module:
    """Resolve concise surface declarations into canonical AST kinds."""

    known_modules = {item.name for item in (*module.submodules, module)}
    known_protocols = {item.name for item in module.protocols}
    instances = list(module.instances)
    interfaces = list(module.aggregate_interfaces)
    destructure_ordinal = 0

    occupied_value_names = {
        *[item.name for item in module.ports],
        *[item.name for item in module.registers],
        *[item.name for item in module.instances],
        *[item.name for item in module.fifos],
        *[item.name for item in module.memories],
        *[item.name for item in module.roms],
        *[item.name for item in module.aggregate_interfaces],
        *[item.name for item in module.generic_declarations],
        *[item.target for item in module.assignments if "." not in item.target],
        *module.clocks,
        *module.resets,
    }

    def destructure_bindings(
        expression: ast.Expression,
        origin: object | None,
        names: tuple[str, ...],
        type_name: ast.TypeSyntax | None,
    ) -> tuple[ast.Assignment, ...]:
        nonlocal destructure_ordinal
        is_struct = type_name is not None
        duplicate = next(
            (name for name in names if names.count(name) > 1),
            None,
        )
        if duplicate is not None:
            description = (
                f"struct destructuring repeats field '{duplicate}'"
                if is_struct
                else f"tuple destructuring repeats binding '{duplicate}'"
            )
            raise SemanticError(description)
        collision = next(
            (name for name in names if name in occupied_value_names),
            None,
        )
        if collision is not None:
            description = (
                f"destructured field '{collision}' shadows an existing symbol"
                if is_struct
                else f"tuple binding '{collision}' shadows an existing symbol"
            )
            raise SemanticError(description)
        span = (
            origin.render() if origin is not None else str(destructure_ordinal)
        )
        suffix = "" if is_struct else "|tuple"
        hidden_hash = hashlib.sha256(
            (
                f"{module.source_identity or module.name}|{span}|"
                f"{destructure_ordinal}{suffix}"
            ).encode()
        ).hexdigest()[:16]
        hidden_name = (
            f"__destructure_{hidden_hash}"
            if is_struct
            else f"__tuple_destructure_{hidden_hash}"
        )
        bindings = [ast.Assignment(
            hidden_name,
            expression,
            type_name,
            origin=origin,
            tuple_destructure_arity=None if is_struct else len(names),
        )]
        for index, name in enumerate(names):
            projection: ast.Expression = (
                ast.FieldExpr(
                    ast.NameExpr(hidden_name, origin=origin),
                    name,
                    origin=origin,
                )
                if is_struct
                else ast.IndexExpr(
                    ast.NameExpr(hidden_name, origin=origin),
                    index,
                    origin=origin,
                )
            )
            bindings.append(ast.Assignment(name, projection, origin=origin))
            occupied_value_names.add(name)
        destructure_ordinal += 1
        return tuple(bindings)

    def expand_port(declaration: ast.PortDecl) -> tuple[object, ...]:
        names = declaration.names or (declaration.name,)
        if declaration.initializer is not None and len(names) > 1:
            raise SemanticError(
                "grouped port declarations cannot have an initializer; "
                "use one inline output declaration per driven output"
            )
        if declaration.initializer is not None:
            if declaration.direction is ast.Direction.INPUT:
                raise SemanticError(
                    f"input port '{declaration.name}' cannot have an initializer; "
                    "inputs are driven by the parent/environment"
                )
            if isinstance(declaration.type_name, ast.InterfaceTypeName):
                raise SemanticError(
                    f"protocol port '{declaration.name}' cannot have an initializer"
                )
        expanded: list[object] = []
        for index, name in enumerate(names):
            name_origin = (
                declaration.name_origins[index]
                if index < len(declaration.name_origins)
                else None
            )
            expanded.append(replace(
                declaration,
                name=name,
                names=(),
                initializer=None,
                name_origins=(() if name_origin is None else (name_origin,)),
            ))
        if declaration.initializer is not None:
            expanded.append(ast.Assignment(
                declaration.name,
                declaration.initializer,
                origin=declaration.origin,
                name_origin=(
                    declaration.name_origins[0]
                    if declaration.name_origins else None
                ),
            ))
        return tuple(expanded)

    def normalize_generic(declaration: ast.GenericDeclaration) -> object:
        reference, parsed_arguments = _reference_parts(declaration.type_name)
        arguments = declaration.specializations or parsed_arguments
        is_module = reference in known_modules
        is_protocol = reference in known_protocols
        try:
            type_resolver.resolve(declaration.type_name)
            is_value_type = True
        except SemanticError:
            is_value_type = False
        if declaration.role is not None:
            if not is_protocol:
                raise SemanticError(
                    f"concise declaration '{declaration.name}' references unknown "
                    f"protocol '{reference}'"
                )
            if declaration.array_length is not None or declaration.bindings:
                raise SemanticError(
                    "protocol endpoints cannot have instance arrays or inline bindings"
                )
            inferred_domain = (
                module.clocks[0]
                if len(module.clocks) == 1
                else (
                    inherited_domain.clock
                    if isinstance(inherited_domain, ir_cdc.ClockDomain)
                    else inherited_domain[0]
                )
                if not module.clocks and inherited_domain is not None
                else declaration.domain
            )
            result = ast.AggregateInterfaceDecl(
                declaration.name,
                reference,
                arguments,
                declaration.role,
                inferred_domain,
            )
            interfaces.append(result)
            return result
        categories = sum((is_module, is_value_type, is_protocol))
        if categories > 1:
            raise SemanticError(
                f"concise declaration '{declaration.name}' is ambiguous for "
                f"'{reference}'; use explicit 'inst' for a module instance"
            )
        if is_module:
            if declaration.initializer is not None:
                raise SemanticError(
                    f"module instance '{declaration.name}' cannot have a value initializer"
                )
            if declaration.domain is not None:
                raise SemanticError(
                    "module instance declarations do not accept @domain"
                )
            result = ast.InstanceDecl(
                declaration.name,
                reference,
                arguments,
                declaration.array_length,
                declaration.bindings,
                declaration.origin,
                declaration.name_origin,
                declaration.type_origin,
            )
            instances.append(result)
            return result
        if is_value_type:
            if declaration.array_length is not None or declaration.bindings:
                raise SemanticError(
                    f"immutable value '{declaration.name}' cannot have instance options"
                )
            if declaration.initializer is None:
                raise SemanticError(
                    f"immutable value '{declaration.name}' requires an initializer"
                )
            result = ast.Assignment(
                declaration.name,
                declaration.initializer,
                declaration.type_name,
                origin=declaration.origin,
                name_origin=declaration.name_origin,
            )
            return result
        if is_protocol:
            raise SemanticError(
                f"protocol declaration '{declaration.name}' requires an explicit role"
            )
        raise SemanticError(
            f"concise declaration '{declaration.name}' has unknown type, module, "
            f"or protocol '{reference or declaration.type_name}'"
        )

    anonymous_rules: dict[int, ast.RuleDecl] = {}
    anonymous_ordinal = 0

    def register_anonymous(items: tuple[object, ...]) -> None:
        nonlocal anonymous_ordinal
        for item in items:
            if isinstance(item, ast.AnonymousRuleDecl):
                if id(item) in anonymous_rules:
                    continue
                span = (
                    item.origin.render()
                    if item.origin is not None
                    else f"ordinal:{anonymous_ordinal}"
                )
                parameters = tuple(
                    (parameter.name, parameter.kind, parameter.default)
                    for parameter in module.parameters
                )
                payload = (
                    f"{module.source_identity or module.name}|"
                    f"{module.source_hash or ''}|{parameters}|{span}|"
                    f"{anonymous_ordinal}"
                )
                identity = hashlib.sha256(payload.encode()).hexdigest()[:16]
                anonymous_rules[id(item)] = ast.RuleDecl(
                    f"__anonymous_rule_{identity}",
                    item.guard,
                    item.actions,
                    item.origin,
                )
                anonymous_ordinal += 1
            elif isinstance(item, ast.CompileTimeIfDecl):
                register_anonymous(item.when_true)
                register_anonymous(item.when_false)
            elif isinstance(item, ast.GenerateBlock):
                register_anonymous(item.items)

    register_anonymous(module.ordered_items)

    def normalize_items(items: tuple[object, ...]) -> tuple[object, ...]:
        nonlocal destructure_ordinal
        normalized: list[object] = []
        for item in items:
            if isinstance(item, ast.PortDecl):
                normalized.extend(expand_port(item))
            elif isinstance(item, ast.GenericDeclaration):
                normalized.append(normalize_generic(item))
            elif isinstance(item, ast.StructDestructureDecl):
                resolved = type_resolver.resolve(item.type_name)
                if not isinstance(resolved, ir_types.StructType):
                    raise SemanticError(
                        "immutable destructuring requires a nominal struct type, "
                        f"got {resolved}"
                    )
                declared_fields = tuple(field.name for field in resolved.fields)
                unknown = tuple(
                    name for name in item.fields if name not in declared_fields
                )
                missing = tuple(
                    name for name in declared_fields if name not in item.fields
                )
                if unknown or missing:
                    details: list[str] = []
                    if unknown:
                        details.append("unknown: " + ", ".join(unknown))
                    if missing:
                        details.append("missing: " + ", ".join(missing))
                    raise SemanticError(
                        f"destructuring of '{resolved.name}' must name every field "
                        f"exactly once ({'; '.join(details)})"
                    )
                normalized.extend(destructure_bindings(
                    item.expression,
                    item.origin,
                    item.fields,
                    item.type_name,
                ))
            elif isinstance(item, ast.TupleDestructureDecl):
                normalized.extend(destructure_bindings(
                    item.expression,
                    item.origin,
                    item.names,
                    None,
                ))
            elif isinstance(item, ast.AnonymousRuleDecl):
                normalized.append(anonymous_rules.get(id(item), item))
            elif isinstance(item, ast.CompileTimeIfDecl):
                normalized.append(replace(
                    item,
                    when_true=normalize_items(item.when_true),
                    when_false=normalize_items(item.when_false),
                ))
            elif isinstance(item, ast.GenerateBlock):
                normalized.append(replace(
                    item,
                    items=normalize_items(item.items),
                ))
            else:
                normalized.append(item)
        return tuple(normalized)

    ordered_items = normalize_items(module.ordered_items)

    default_domain = (
        module.clocks[0]
        if len(module.clocks) == 1
        else (
            inherited_domain.clock
            if isinstance(inherited_domain, ir_cdc.ClockDomain)
            else inherited_domain[0]
        )
        if not module.clocks and inherited_domain is not None
        else None
    )
    normalized_interfaces: list[ast.AggregateInterfaceDecl] = []
    for declaration in interfaces:
        domain = declaration.domain
        if domain is None:
            if len(module.clocks) > 1:
                raise SemanticError(
                    f"protocol endpoint '{declaration.name}' requires an explicit domain"
                )
            domain = default_domain
        normalized_interfaces.append(replace(declaration, domain=domain))

    normalized_ports = tuple(
        item for item in ordered_items if isinstance(item, ast.PortDecl)
    )
    normalized_assignments = tuple(
        item for item in ordered_items if isinstance(item, ast.Assignment)
    )
    return replace(
        module,
        instances=tuple(instances),
        ports=normalized_ports,
        assignments=normalized_assignments,
        aggregate_interfaces=tuple(normalized_interfaces),
        generic_declarations=(),
        rules=tuple(
            declaration
            if isinstance(declaration, ast.RuleDecl)
            else anonymous_rules.get(id(declaration), declaration)
            for declaration in module.rules
        ),
        ordered_items=ordered_items,
    )


def normalize_qualified_imports(
    module: ast.Module,
    resolved_by_path: dict[str, object],
    *,
    source_unit: str | None,
    source_digest: str | None,
) -> ast.Module:
    """Erase source-local import qualifiers before ordinary semantic typing."""

    alias_declarations = tuple(item for item in module.imports if item.alias)
    if not alias_declarations:
        return module

    aliases = tuple(item.alias for item in alias_declarations)
    duplicate = next(
        (alias for alias in aliases if aliases.count(alias) > 1),
        None,
    )
    if duplicate is not None:
        declaration = next(
            item for item in alias_declarations if item.alias == duplicate
        )
        raise SemanticError(
            f"duplicate import alias '{duplicate}'",
            code="ZL-IMPORT-ALIAS-DUPLICATE",
            primary=(
                SourceOrigin(
                    declaration.origin,
                    "import alias",
                    source_unit,
                    source_digest,
                )
                if declaration.origin is not None
                else None
            ),
            fixes=("choose a unique import alias",),
        )

    def source_exports(source: ast.Module) -> dict[str, frozenset[str]]:
        return {
            "type": frozenset(
                item.name
                for group in (
                    source.type_aliases,
                    source.structs,
                    source.enums,
                    source.tagged_unions,
                )
                for item in group
            ),
            "function": frozenset(item.name for item in source.functions),
            "constructor": frozenset(item.name for item in source.structs),
        }

    exports: dict[str, dict[str, frozenset[str]]] = {}
    for declaration in alias_declarations:
        assert declaration.alias is not None
        record = resolved_by_path[declaration.path]
        exports[declaration.alias] = source_exports(record.ast)

    # An ordinary direct import retains the historical unqualified surface.
    # Only names available exclusively through aliased imports are hidden.
    unqualified_types: set[str] = set()
    unqualified_functions: set[str] = set()
    unqualified_constructors: set[str] = set()
    for declaration in module.imports:
        if declaration.alias is not None:
            continue
        visible = source_exports(resolved_by_path[declaration.path].ast)
        unqualified_types.update(visible["type"])
        unqualified_functions.update(visible["function"])
        unqualified_constructors.update(visible["constructor"])

    def hidden_names(namespace: str, visible: set[str]) -> set[str]:
        hidden = set().union(*(item[namespace] for item in exports.values()))
        hidden.difference_update(visible)
        return hidden

    hidden_types = hidden_names("type", unqualified_types)
    hidden_functions = hidden_names("function", unqualified_functions)
    hidden_constructors = hidden_names("constructor", unqualified_constructors)

    def fail_unknown_alias(alias: str, origin: object | None = None) -> None:
        raise SemanticError(
            f"unknown import alias '{alias}'",
            code="ZL-IMPORT-ALIAS-UNKNOWN",
            primary=(
                SourceOrigin(origin, "qualified import", source_unit, source_digest)
                if origin is not None
                else None
            ),
            fixes=("declare the alias with 'import <logical.module> as <alias>'",),
        )

    def resolve_member(
        alias: str,
        member: str,
        namespace: str,
        origin: object | None = None,
    ) -> str:
        if alias not in exports:
            fail_unknown_alias(alias, origin)
        if member not in exports[alias][namespace]:
            raise SemanticError(
                f"logical module alias '{alias}' has no {namespace} member '{member}'",
                code="ZL-IMPORT-MEMBER-UNKNOWN",
                primary=(
                    SourceOrigin(
                        origin,
                        "qualified import",
                        source_unit,
                        source_digest,
                    )
                    if origin is not None
                    else None
                ),
            )
        return member

    def normalize_type_name(value: ast.TypeName) -> ast.TypeName:
        original = value.text

        def replace_member(match: re.Match[str]) -> str:
            return resolve_member(
                match.group("alias"), match.group("member"), "type"
            )

        normalized = _QUALIFIED_IMPORT_MEMBER.sub(replace_member, original)
        unqualified_probe = _QUALIFIED_IMPORT_MEMBER.sub("", original)
        hidden = next(
            (
                name
                for name in sorted(hidden_types)
                if re.search(
                    rf"(?<![A-Za-z0-9_.]){re.escape(name)}\b",
                    unqualified_probe,
                )
            ),
            None,
        )
        if hidden is not None:
            raise SemanticError(
                f"type '{hidden}' requires its import alias",
                code="ZL-IMPORT-ALIAS-REQUIRED",
            )
        # Preserve parser-owned source spans for definition tooling.
        normalized_origins = tuple(
            (_QUALIFIED_IMPORT_MEMBER.sub(replace_member, name), origin)
            for name, origin in value.named_origins
        )
        return replace(
            value,
            text=normalized,
            named_origins=normalized_origins,
        )

    def walk(value: object) -> object:
        if isinstance(value, ast.ImportDecl):
            return value
        if isinstance(value, ast.TypeName):
            return normalize_type_name(value)
        if isinstance(value, ast.CallExpr):
            function = value.function
            if "." in function:
                alias, member = function.split(".", 1)
                function = resolve_member(
                    alias, member, "function", value.origin
                )
            elif function in hidden_functions:
                raise SemanticError(
                    f"function '{function}' requires its import alias",
                    code="ZL-IMPORT-ALIAS-REQUIRED",
                    primary=(
                        SourceOrigin(
                            value.origin,
                            "function call",
                            source_unit,
                            source_digest,
                        )
                        if value.origin is not None
                        else None
                    ),
                )
            return replace(
                value,
                function=function,
                arguments=tuple(walk(item) for item in value.arguments),
                specializations=tuple(walk(item) for item in value.specializations),
            )
        if isinstance(value, ast.StructConstructExpr):
            name = value.struct_name
            if "." in name:
                alias, member = name.split(".", 1)
                name = resolve_member(alias, member, "constructor", value.origin)
            elif name in hidden_constructors:
                raise SemanticError(
                    f"struct constructor '{name}' requires its import alias",
                    code="ZL-IMPORT-ALIAS-REQUIRED",
                )
            return replace(
                value,
                struct_name=name,
                fields=tuple(walk(item) for item in value.fields),
            )
        if isinstance(value, tuple):
            return tuple(walk(item) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            changes = {
                item.name: walk(getattr(value, item.name))
                for item in fields(value)
                if item.init and item.name != "origin"
            }
            return replace(value, **changes)
        return value

    normalized_module = walk(module)
    assert isinstance(normalized_module, ast.Module)
    return normalized_module


def _actions_in(
    declaration: object,
) -> tuple[ast.NextAssignment | ast.ResourceAction, ...]:
    if isinstance(declaration, (ast.RuleDecl, ast.AnonymousRuleDecl)):
        return tuple(
            leaf.action
            for leaf in semantic_actions.conditional_action_leaves(
                declaration.actions
            )
        )
    if isinstance(declaration, ast.PriorityBlockDecl):
        actions: list[ast.NextAssignment | ast.ResourceAction] = []
        for arm in declaration.arms:
            if isinstance(arm, ast.PriorityRuleArm):
                actions.extend(
                    leaf.action
                    for leaf in semantic_actions.conditional_action_leaves(
                        arm.actions
                    )
                )
            elif isinstance(arm, ast.PriorityBlockDecl):
                actions.extend(_actions_in(arm))
        return tuple(actions)
    return ()


def _expand_fsms(
    module: ast.Module,
    selected: tuple[object, ...],
    type_resolver: TypeResolver,
) -> tuple[object, ...]:
    """Lower syntax-only FSM declarations to the existing rule model."""

    expanded: list[object] = []
    seen_names: set[str] = set()
    for item in selected:
        if not isinstance(item, ast.FsmDecl):
            expanded.append(item)
            continue
        if item.name in seen_names:
            raise SemanticError(f"duplicate FSM '{item.name}'")
        seen_names.add(item.name)
        enum_type = type_resolver.resolve(item.type_name)
        if not isinstance(enum_type, ir_types.EnumType):
            raise SemanticError(
                f"FSM '{item.name}' state type must be an enum, got {enum_type}"
            )
        if item.initial_member not in enum_type.members:
            raise SemanticError(
                f"FSM '{item.name}' initial state '{item.initial_member}' is not "
                f"a member of enum '{enum_type.name}'"
            )
        state_names = tuple(state.member for state in item.states)
        duplicate_state = next(
            (name for name in state_names if state_names.count(name) > 1), None
        )
        if duplicate_state is not None:
            raise SemanticError(
                f"FSM '{item.name}' defines state '{duplicate_state}' more than once"
            )
        unknown_state = next(
            (name for name in state_names if name not in enum_type.members), None
        )
        if unknown_state is not None:
            raise SemanticError(
                f"FSM '{item.name}' state '{unknown_state}' is not a member "
                f"of enum '{enum_type.name}'"
            )
        missing_states = tuple(
            member for member in enum_type.members if member not in state_names
        )
        if missing_states:
            raise SemanticError(
                f"FSM '{item.name}' is missing enum state(s): "
                + ", ".join(missing_states)
            )
        if any(
            isinstance(other, ast.RegisterDecl) and other.name == item.name
            for other in selected
        ):
            raise SemanticError(
                f"FSM '{item.name}' conflicts with an explicit register of the same name"
            )
        for other in selected:
            if isinstance(other, ast.NextAssignment) and other.target == item.name:
                raise SemanticError(
                    f"FSM register '{item.name}' cannot be written outside its transitions"
                )
            if any(
                isinstance(action, ast.NextAssignment)
                and action.target == item.name
                for action in _actions_in(other)
            ):
                raise SemanticError(
                    f"FSM register '{item.name}' cannot be written outside its transitions"
                )

        initial = ast.FieldExpr(
            ast.NameExpr(enum_type.name, origin=item.origin),
            item.initial_member,
            origin=item.origin,
        )
        expanded.append(ast.RegisterDecl(
            item.name,
            item.type_name,
            initial,
            item.domain,
            item.origin,
        ))
        identity_prefix = (
            f"{module.source_identity or module.name}|{module.source_hash or ''}|"
            f"{tuple((parameter.name, parameter.kind, parameter.default) for parameter in module.parameters)}|"
            f"{item.origin.render() if item.origin is not None else item.name}"
        )
        for state_ordinal, state in enumerate(item.states):
            if state.hold:
                if state.transitions or state.priority:
                    raise SemanticError(
                        f"FSM '{item.name}' state '{state.member}' cannot combine hold and transitions"
                    )
                continue
            if not state.transitions:
                raise SemanticError(
                    f"FSM '{item.name}' state '{state.member}' requires hold or a transition"
                )
            if state.priority and len(state.transitions) < 2:
                raise SemanticError(
                    f"FSM '{item.name}' priority state '{state.member}' requires at least two transitions"
                )
            if not state.priority and len(state.transitions) != 1:
                raise SemanticError(
                    f"FSM '{item.name}' state '{state.member}' has multiple transitions; use priority"
                )
            previous_rule: str | None = None
            for transition_ordinal, transition in enumerate(state.transitions):
                if transition.target not in enum_type.members:
                    raise SemanticError(
                        f"FSM '{item.name}' transition target '{transition.target}' "
                        f"is not a member of enum '{enum_type.name}'"
                    )
                if state.priority and transition.guard is None:
                    raise SemanticError(
                        f"FSM '{item.name}' priority transitions require explicit when guards"
                    )
                if any(
                    isinstance(leaf.action, ast.NextAssignment)
                    and leaf.action.target == item.name
                    for leaf in semantic_actions.conditional_action_leaves(
                        transition.actions
                    )
                ):
                    raise SemanticError(
                        f"FSM register '{item.name}' cannot be written explicitly inside a transition"
                    )
                state_value = ast.FieldExpr(
                    ast.NameExpr(enum_type.name, origin=state.origin),
                    state.member,
                    origin=state.origin,
                )
                state_guard: ast.Expression = ast.BinaryExpr(
                    ast.BinaryOperator.EQUAL,
                    ast.NameExpr(item.name, origin=state.origin),
                    state_value,
                    origin=state.origin,
                )
                if transition.guard is not None:
                    state_guard = ast.BinaryExpr(
                        ast.BinaryOperator.BIT_AND,
                        state_guard,
                        transition.guard,
                        origin=transition.origin,
                    )
                target_value = ast.FieldExpr(
                    ast.NameExpr(enum_type.name, origin=transition.origin),
                    transition.target,
                    origin=transition.origin,
                )
                payload = (
                    f"{identity_prefix}|state:{state.member}:{state_ordinal}|"
                    f"transition:{transition_ordinal}"
                )
                rule_name = "__fsm_" + hashlib.sha256(
                    payload.encode()
                ).hexdigest()[:20]
                expanded.append(ast.RuleDecl(
                    rule_name,
                    state_guard,
                    (
                        ast.NextAssignment(item.name, target_value),
                        *transition.actions,
                    ),
                    transition.origin,
                    item.domain,
                ))
                if previous_rule is not None:
                    expanded.append(ast.RulePriority(previous_rule, rule_name))
                previous_rule = rule_name
    return tuple(expanded)


def _expand_priority_blocks(
    module: ast.Module,
    selected: tuple[object, ...],
) -> tuple[object, ...]:
    """Lower selected priority blocks without allowing dead arms to leak."""

    expanded: list[object] = []
    generated_priorities: list[ast.RulePriority] = []
    generated_anonymous_names: set[str] = set()
    for item in selected:
        if not isinstance(item, ast.PriorityBlockDecl):
            expanded.append(item)
            continue
        if len(item.arms) < 2:
            raise SemanticError(
                "priority block requires at least two arms; use a single when rule instead"
            )
        block_identity = (
            f"{module.source_identity or module.name}|{module.source_hash or ''}|"
            f"{item.origin.render() if item.origin is not None else 'priority'}"
        )
        previous: str | None = None
        labels: set[str] = set()
        for ordinal, arm in enumerate(item.arms):
            if isinstance(arm, ast.PriorityBlockDecl):
                raise SemanticError("nested priority blocks are not supported")
            if not isinstance(arm, ast.PriorityRuleArm):
                raise SemanticError("invalid priority block arm")
            if arm.guard is None:
                raise SemanticError("priority block arm requires a when guard")
            if not semantic_actions.conditional_action_leaves(arm.actions):
                raise SemanticError("priority block arm cannot be empty")
            if arm.label is None:
                payload = f"{block_identity}|arm:{ordinal}"
                name = "__priority_rule_" + hashlib.sha256(
                    payload.encode()
                ).hexdigest()[:16]
                generated_anonymous_names.add(name)
            else:
                name = arm.label
            if name in labels:
                raise SemanticError(
                    f"duplicate priority block rule label '{name}'"
                )
            labels.add(name)
            expanded.append(ast.RuleDecl(name, arm.guard, arm.actions, arm.origin))
            if previous is not None:
                generated_priorities.append(ast.RulePriority(previous, name))
            previous = name
    expanded.extend(generated_priorities)
    generated_edges = set(generated_priorities)
    for item in expanded:
        if (
            isinstance(item, ast.RulePriority)
            and item not in generated_edges
            and (
                item.higher in generated_anonymous_names
                or item.lower in generated_anonymous_names
            )
        ):
            raise SemanticError(
                "anonymous priority-block arms cannot be referenced by explicit priority declarations"
            )
    return tuple(expanded)


def _rebuild_category_views(
    module: ast.Module,
    selected: tuple[object, ...],
) -> ast.Module:
    fields_to_extend: dict[str, list[object]] = {
        name: []
        for name in (
            "ports", "assignments", "clocks", "resets", "reset_domains",
            "clock_physical", "reset_physical", "registers", "next_assignments",
            "request_responses", "connections", "csr_blocks", "csr_groups",
            "rules", "rule_priorities", "fifos", "connection_chains", "memories",
            "roms", "arbiters", "contracts", "instances", "verification_goals",
            "verification_scopes", "aggregate_interfaces", "generic_declarations",
            "generate_blocks",
        )
    }
    categories = (
        (ast.PortDecl, "ports"),
        (ast.AggregateInterfaceDecl, "aggregate_interfaces"),
        (ast.Assignment, "assignments"),
        (ast.InstanceDecl, "instances"),
        (ast.GenericDeclaration, "generic_declarations"),
        (ast.RegisterDecl, "registers"),
        (ast.NextAssignment, "next_assignments"),
        (ast.RequestResponseDecl, "request_responses"),
        (ast.ConnectionDecl, "connections"),
        (ast.ConnectionChainDecl, "connection_chains"),
        (ast.CsrBlockDecl, "csr_blocks"),
        (ast.CsrGroupDecl, "csr_groups"),
        ((ast.RuleDecl, ast.AnonymousRuleDecl), "rules"),
        (ast.RulePriority, "rule_priorities"),
        (ast.FifoDecl, "fifos"),
        (ast.MemoryDecl, "memories"),
        (ast.RomDecl, "roms"),
        (ast.ArbiterDecl, "arbiters"),
        (ast.ContractDecl, "contracts"),
        (ast.VerificationGoalDecl, "verification_goals"),
        (ast.VerificationScopeDecl, "verification_scopes"),
    )
    for item in selected:
        category = next(
            (name for type_, name in categories if isinstance(item, type_)), None
        )
        if category is not None:
            fields_to_extend[category].append(item)
        elif isinstance(item, ast.ModuleTimingDecl):
            continue
        elif isinstance(item, tuple) and item and item[0] == "clock":
            fields_to_extend["clocks"].append(item[1])
            fields_to_extend["clock_physical"].append(item[2])
        elif isinstance(item, tuple) and item and item[0] == "reset":
            fields_to_extend["resets"].append(item[1])
            fields_to_extend["reset_domains"].append((item[1], item[2]))
            fields_to_extend["reset_physical"].append(item[3])
        else:
            raise SemanticError(
                f"unsupported declaration in compile-time module if: {type(item).__name__}"
            )
    fields_to_extend["clocks"] = list(dict.fromkeys(fields_to_extend["clocks"]))
    fields_to_extend["resets"] = list(dict.fromkeys(fields_to_extend["resets"]))
    return replace(
        module,
        compile_time_ifs=(),
        fsms=(),
        ordered_items=selected,
        **{name: tuple(values) for name, values in fields_to_extend.items()},
    )


def normalize_selected_module_items(
    module: ast.Module,
    selected: tuple[object, ...],
    type_resolver: TypeResolver,
) -> ast.Module:
    """Normalize selected module items into authoritative compatibility views."""

    selected = _expand_fsms(module, selected, type_resolver)
    selected = _expand_priority_blocks(module, selected)
    return _rebuild_category_views(module, selected)
