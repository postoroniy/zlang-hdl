# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Canonicalize concise module declarations before semantic checking.

Declaration classification and binding expansion retain different mutable
facts.  Keeping them in separate owners makes each source-item domain
extensible without recreating one large normalization function.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.ir import cdc as ir_cdc
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin

from .errors import SemanticError

if TYPE_CHECKING:
    from .type_resolution import TypeResolver


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


class _ConciseDeclarationResolver:
    """Own declaration classification and the resulting ABI collections."""

    def __init__(
        self,
        module: ast.Module,
        type_resolver: TypeResolver,
        inherited_domain: tuple[str, str] | ir_cdc.ClockDomain | None,
        *,
        source_unit: str | None,
        source_digest: str | None,
    ) -> None:
        self.module = module
        self.type_resolver = type_resolver
        self.inherited_domain = inherited_domain
        self.source_unit = source_unit
        self.source_digest = source_digest
        self.known_modules = {item.name for item in (*module.submodules, module)}
        self.known_protocols = {item.name for item in module.protocols}
        self.instances = list(module.instances)
        self.interfaces = list(module.aggregate_interfaces)

    def declaration_origin(
        self,
        declaration: ast.GenericDeclaration,
        *,
        type_reference: bool = False,
    ) -> SourceOrigin | None:
        span = (
            declaration.type_origin
            if type_reference and declaration.type_origin is not None
            else declaration.name_origin or declaration.origin
        )
        if span is None:
            return None
        construct = (
            f"type of concise declaration {declaration.name}"
            if type_reference
            else f"concise declaration {declaration.name}"
        )
        return SourceOrigin(
            span,
            construct,
            self.module.source_identity or self.source_unit,
            self.module.source_hash or self.source_digest,
        )

    def expand_port(self, declaration: ast.PortDecl) -> tuple[object, ...]:
        names = declaration.names or (declaration.name,)
        origin = (
            SourceOrigin(
                declaration.origin,
                f"port {declaration.name}",
                self.module.source_identity,
                self.module.source_hash,
            )
            if declaration.origin is not None
            else None
        )
        if declaration.registered:
            if declaration.direction is not ast.Direction.OUTPUT:
                raise SemanticError(
                    f"registered port '{declaration.name}' must be an output",
                    code="ZL-SEMANTIC-REGISTERED-OUTPUT",
                    primary=origin,
                )
            if len(names) != 1:
                raise SemanticError(
                    "registered output declarations must contain exactly one name",
                    code="ZL-SEMANTIC-REGISTERED-OUTPUT",
                    primary=origin,
                )
            if isinstance(declaration.type_name, ast.InterfaceTypeName):
                raise SemanticError(
                    f"protocol port '{declaration.name}' cannot be a registered output",
                    code="ZL-SEMANTIC-REGISTERED-OUTPUT",
                    primary=origin,
                )
            return (replace(declaration, names=()),)
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
            expanded.append(
                replace(
                    declaration,
                    name=name,
                    names=(),
                    initializer=None,
                    name_origins=(() if name_origin is None else (name_origin,)),
                )
            )
        if declaration.initializer is not None:
            expanded.append(
                ast.Assignment(
                    declaration.name,
                    declaration.initializer,
                    origin=declaration.origin,
                    name_origin=(
                        declaration.name_origins[0]
                        if declaration.name_origins
                        else None
                    ),
                )
            )
        return tuple(expanded)

    def normalize_generic(self, declaration: ast.GenericDeclaration) -> object:
        reference, parsed_arguments = _reference_parts(declaration.type_name)
        arguments = declaration.specializations or parsed_arguments
        is_module = reference in self.known_modules
        is_protocol = reference in self.known_protocols
        try:
            self.type_resolver.resolve(declaration.type_name)
            is_value_type = True
        except SemanticError:
            is_value_type = False
        if declaration.role is not None:
            if not is_protocol:
                raise SemanticError(
                    f"concise declaration '{declaration.name}' references unknown "
                    f"protocol '{reference}'",
                    primary=self.declaration_origin(
                        declaration,
                        type_reference=True,
                    ),
                )
            if declaration.array_length is not None or declaration.bindings:
                raise SemanticError(
                    "protocol endpoints cannot have instance arrays or inline bindings",
                    primary=self.declaration_origin(declaration),
                )
            result = ast.AggregateInterfaceDecl(
                declaration.name,
                reference,
                arguments,
                declaration.role,
                self._inferred_domain(declaration.domain),
            )
            self.interfaces.append(result)
            return result
        categories = sum((is_module, is_value_type, is_protocol))
        if categories > 1:
            raise SemanticError(
                f"concise declaration '{declaration.name}' is ambiguous for "
                f"'{reference}'; use explicit 'inst' for a module instance",
                primary=self.declaration_origin(declaration, type_reference=True),
            )
        if is_module:
            if declaration.initializer is not None:
                raise SemanticError(
                    f"module instance '{declaration.name}' cannot have a value initializer",
                    primary=self.declaration_origin(declaration),
                )
            if declaration.domain is not None:
                raise SemanticError(
                    "module instance declarations do not accept @domain",
                    primary=self.declaration_origin(declaration),
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
            self.instances.append(result)
            return result
        if is_value_type:
            if declaration.array_length is not None or declaration.bindings:
                raise SemanticError(
                    f"immutable value '{declaration.name}' cannot have instance options",
                    primary=self.declaration_origin(declaration),
                )
            if declaration.initializer is None:
                raise SemanticError(
                    f"immutable value '{declaration.name}' requires an initializer",
                    primary=self.declaration_origin(declaration),
                )
            return ast.Assignment(
                declaration.name,
                declaration.initializer,
                declaration.type_name,
                origin=declaration.origin,
                name_origin=declaration.name_origin,
            )
        if is_protocol:
            raise SemanticError(
                f"protocol declaration '{declaration.name}' requires an explicit role",
                primary=self.declaration_origin(declaration),
            )
        raise SemanticError(
            f"concise declaration '{declaration.name}' has unknown type, module, "
            f"or protocol '{reference or declaration.type_name}'",
            primary=self.declaration_origin(declaration, type_reference=True),
        )

    def _inferred_domain(self, declared: str | None) -> str | None:
        if len(self.module.clocks) == 1:
            return self.module.clocks[0]
        if not self.module.clocks and self.inherited_domain is not None:
            if isinstance(self.inherited_domain, ir_cdc.ClockDomain):
                return self.inherited_domain.clock
            return self.inherited_domain[0]
        return declared

    def normalized_interfaces(self) -> tuple[ast.AggregateInterfaceDecl, ...]:
        default_domain = self._inferred_domain(None)
        normalized: list[ast.AggregateInterfaceDecl] = []
        for declaration in self.interfaces:
            domain = declaration.domain
            if domain is None:
                if len(self.module.clocks) > 1:
                    raise SemanticError(
                        f"protocol endpoint '{declaration.name}' requires an explicit domain"
                    )
                domain = default_domain
            normalized.append(replace(declaration, domain=domain))
        return tuple(normalized)


class _ConciseBindingNormalizer:
    """Own destructuring identities and anonymous-rule expansion state."""

    def __init__(
        self,
        module: ast.Module,
        declarations: _ConciseDeclarationResolver,
    ) -> None:
        self.module = module
        self.declarations = declarations
        self.destructure_ordinal = 0
        self.anonymous_ordinal = 0
        self.anonymous_rules: dict[int, ast.RuleDecl] = {}
        self.occupied_value_names = {
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

    def register_anonymous(self, items: tuple[object, ...]) -> None:
        for item in items:
            if isinstance(item, ast.AnonymousRuleDecl):
                if id(item) in self.anonymous_rules:
                    continue
                span = (
                    item.origin.render()
                    if item.origin is not None
                    else f"ordinal:{self.anonymous_ordinal}"
                )
                parameters = tuple(
                    (parameter.name, parameter.kind, parameter.default)
                    for parameter in self.module.parameters
                )
                payload = (
                    f"{self.module.source_identity or self.module.name}|"
                    f"{self.module.source_hash or ''}|{parameters}|{span}|"
                    f"{self.anonymous_ordinal}"
                )
                identity = hashlib.sha256(payload.encode()).hexdigest()[:16]
                self.anonymous_rules[id(item)] = ast.RuleDecl(
                    f"__anonymous_rule_{identity}",
                    item.guard,
                    item.actions,
                    item.origin,
                )
                self.anonymous_ordinal += 1
            elif isinstance(item, ast.CompileTimeIfDecl):
                self.register_anonymous(item.when_true)
                self.register_anonymous(item.when_false)
            elif isinstance(item, ast.GenerateBlock):
                self.register_anonymous(item.items)

    def normalize_items(self, items: tuple[object, ...]) -> tuple[object, ...]:
        normalized: list[object] = []
        for item in items:
            if isinstance(item, ast.PortDecl):
                normalized.extend(self.declarations.expand_port(item))
            elif isinstance(item, ast.GenericDeclaration):
                normalized.append(self.declarations.normalize_generic(item))
            elif isinstance(item, ast.StructDestructureDecl):
                resolved = self.declarations.type_resolver.resolve(item.type_name)
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
                normalized.extend(
                    self.destructure_bindings(
                        item.expression,
                        item.origin,
                        item.fields,
                        item.type_name,
                    )
                )
            elif isinstance(item, ast.TupleDestructureDecl):
                normalized.extend(
                    self.destructure_bindings(
                        item.expression,
                        item.origin,
                        item.names,
                        None,
                    )
                )
            elif isinstance(item, ast.AnonymousRuleDecl):
                normalized.append(self.anonymous_rules.get(id(item), item))
            elif isinstance(item, ast.CompileTimeIfDecl):
                normalized.append(
                    replace(
                        item,
                        when_true=self.normalize_items(item.when_true),
                        when_false=self.normalize_items(item.when_false),
                    )
                )
            elif isinstance(item, ast.GenerateBlock):
                normalized.append(
                    replace(item, items=self.normalize_items(item.items))
                )
            else:
                normalized.append(item)
        return tuple(normalized)

    def destructure_bindings(
        self,
        expression: ast.Expression,
        origin: object | None,
        names: tuple[str, ...],
        type_name: ast.TypeSyntax | None,
    ) -> tuple[ast.Assignment, ...]:
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
            (name for name in names if name in self.occupied_value_names),
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
            origin.render()
            if origin is not None
            else str(self.destructure_ordinal)
        )
        suffix = "" if is_struct else "|tuple"
        hidden_hash = hashlib.sha256(
            (
                f"{self.module.source_identity or self.module.name}|{span}|"
                f"{self.destructure_ordinal}{suffix}"
            ).encode()
        ).hexdigest()[:16]
        hidden_name = (
            f"__destructure_{hidden_hash}"
            if is_struct
            else f"__tuple_destructure_{hidden_hash}"
        )
        bindings = [
            ast.Assignment(
                hidden_name,
                expression,
                type_name,
                origin=origin,
                tuple_destructure_arity=None if is_struct else len(names),
            )
        ]
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
            self.occupied_value_names.add(name)
        self.destructure_ordinal += 1
        return tuple(bindings)


def normalize_concise_module_items(
    module: ast.Module,
    type_resolver: TypeResolver,
    inherited_domain: tuple[str, str] | ir_cdc.ClockDomain | None,
    *,
    source_unit: str | None = None,
    source_digest: str | None = None,
) -> ast.Module:
    """Resolve concise declarations using declaration and binding owners."""

    declarations = _ConciseDeclarationResolver(
        module,
        type_resolver,
        inherited_domain,
        source_unit=source_unit,
        source_digest=source_digest,
    )
    bindings = _ConciseBindingNormalizer(module, declarations)
    bindings.register_anonymous(module.ordered_items)
    ordered_items = bindings.normalize_items(module.ordered_items)
    return replace(
        module,
        instances=tuple(declarations.instances),
        ports=tuple(
            item for item in ordered_items if isinstance(item, ast.PortDecl)
        ),
        assignments=tuple(
            item for item in ordered_items if isinstance(item, ast.Assignment)
        ),
        aggregate_interfaces=declarations.normalized_interfaces(),
        generic_declarations=(),
        rules=tuple(
            declaration
            if isinstance(declaration, ast.RuleDecl)
            else bindings.anonymous_rules.get(id(declaration), declaration)
            for declaration in module.rules
        ),
        ordered_items=ordered_items,
    )


__all__ = ["normalize_concise_module_items"]
