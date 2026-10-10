# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Named module-interface inheritance and declaration validation."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from zlang.ast import nodes as ast
from zlang.ir import cdc as ir_cdc
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import timing as ir_timing
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin

from . import hierarchy as semantic_hierarchy
from . import type_resolution
from .errors import SemanticError


_INTERFACE_PROTOCOLS = {
    ast.InterfaceKind.WIRE: ir_interfaces.InterfaceProtocol.WIRE,
    ast.InterfaceKind.READY_VALID: ir_interfaces.InterfaceProtocol.READY_VALID,
    ast.InterfaceKind.CREDIT: ir_interfaces.InterfaceProtocol.CREDIT,
    ast.InterfaceKind.PACKET: ir_interfaces.InterfaceProtocol.PACKET,
    ast.InterfaceKind.VC_CREDIT: ir_interfaces.InterfaceProtocol.VC_CREDIT,
}


def interface_protocol(kind: ast.InterfaceKind) -> ir_interfaces.InterfaceProtocol:
    """Map one parser-owned interface kind to its canonical IR protocol."""

    return _INTERFACE_PROTOCOLS[kind]


def inherit_applied_interface_surface(
    module: ast.Module,
    declaration: ast.ModuleInterfaceDecl,
) -> ast.Module:
    """Apply a complete named signature to an otherwise surface-free module."""

    if module.conforms_to is None or any((
        module.ports,
        module.clocks,
        module.resets,
        module.reset_domains,
        module.request_responses,
        module.aggregate_interfaces,
        module.timing is not None,
    )):
        return module
    parameters = declaration.parameters
    by_name = {item.name: item for item in parameters}
    applied: dict[str, int | str | ast.TypeSyntax] = {}
    positional = 0
    for argument in module.conforms_to.arguments:
        name = argument.name
        if name is None:
            if positional >= len(parameters):
                return module
            name = parameters[positional].name
            positional += 1
        if name not in by_name or name in applied:
            return module
        applied[name] = argument.value
    for parameter in parameters:
        if parameter.name not in applied and parameter.default is not None:
            applied[parameter.name] = parameter.default

    def render(value: int | str | ast.TypeSyntax) -> str:
        if isinstance(value, ast.TypeName):
            return value.text
        if isinstance(value, ast.VectorTypeName):
            return f"vec<{value.length},{render(value.element_type)}>"
        if isinstance(value, ast.TupleTypeName):
            return f"({','.join(render(item) for item in value.elements)})"
        return str(value)

    def substitute_text(text: str) -> str:
        for name in sorted(applied, key=lambda item: (-len(item), item)):
            text = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                render(applied[name]),
                text,
            )
        return text

    def substitute_type(value: ast.TypeSyntax) -> ast.TypeSyntax:
        if isinstance(value, ast.VectorTypeName):
            length: int | str = value.length
            if isinstance(length, str):
                replaced = substitute_text(length)
                length = int(replaced) if replaced.isdigit() else replaced
            return ast.VectorTypeName(length, substitute_type(value.element_type))
        if isinstance(value, ast.TupleTypeName):
            return ast.TupleTypeName(tuple(
                substitute_type(item) for item in value.elements
            ))
        return ast.TypeName(substitute_text(value.text))

    def substitute_port_type(value: ast.PortTypeSyntax) -> ast.PortTypeSyntax:
        if isinstance(value, ast.InterfaceTypeName):
            return replace(value, payload_type=substitute_type(value.payload_type))
        return substitute_type(value)

    inherited_ports = tuple(
        replace(port, type_name=substitute_port_type(port.type_name))
        for port in declaration.ports
    )
    inherited_aggregates = tuple(
        replace(
            endpoint,
            arguments=tuple(
                replace(argument, value=(
                    substitute_type(argument.value)
                    if isinstance(
                        argument.value,
                        (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName),
                    )
                    else (
                        int(substitute_text(str(argument.value)))
                        if substitute_text(str(argument.value)).isdigit()
                        else substitute_text(str(argument.value))
                    )
                ))
                for argument in endpoint.arguments
            ),
        )
        for endpoint in declaration.aggregate_interfaces
    )
    reset_domains = declaration.reset_domains or tuple(
        (name, None) for name in declaration.resets
    )
    clocks = declaration.clock_physical or tuple(
        ast.ClockPhysicalDecl(name) for name in declaration.clocks
    )
    resets = declaration.reset_physical or tuple(
        ast.ResetPhysicalDecl(name, domain) for name, domain in reset_domains
    )
    ordered_items: tuple[object, ...] = (
        *(("clock", item.name, item) for item in clocks),
        *(("reset", item.name, item.clock, item) for item in resets),
        *inherited_ports,
        *inherited_aggregates,
        *((declaration.timing,) if declaration.timing is not None else ()),
    )
    return replace(
        module,
        ports=inherited_ports,
        clocks=declaration.clocks,
        resets=declaration.resets,
        reset_domains=reset_domains,
        clock_physical=clocks,
        reset_physical=resets,
        request_responses=declaration.request_responses,
        aggregate_interfaces=inherited_aggregates,
        timing=declaration.timing,
        ordered_items=(*ordered_items, *module.ordered_items),
    )


def _applied_interface_parameters(
    source_module: ast.Module,
    declaration: ast.ModuleInterfaceDecl,
    reference: ast.ModuleInterfaceRef,
    resolver: Any,
    specialization_type_bindings: dict[str, ir_types.HardwareType] | None,
) -> tuple[
    tuple[ir_module.ModuleSignatureParameter, ...],
    dict[str, int],
    dict[str, ir_types.HardwareType],
]:
    module_parameters = (
        source_module.declared_parameters or source_module.parameters
    )

    def normalized_defaults(
        parameters: tuple[ast.ModuleParameter, ...],
    ) -> dict[str, int | str | None]:
        """Normalize declaration defaults independently of an application.

        A specialized child carries concrete values in ``parameters`` while
        ``declared_parameters`` retains its public declaration.  Interface ABI
        identity must use the latter and must not distinguish equivalent
        spellings such as ``4`` and ``2 + 2``.
        """

        parameter_values = {
            item.name: item.default
            for item in parameters
            if item.kind == "value" and item.default is not None
        }
        default_resolver = type_resolution.TypeResolver(
            (), (), (), parameters, parameter_values
        )
        result: dict[str, int | str | None] = {}
        for parameter in parameters:
            default = parameter.default
            if parameter.kind == "type" or default is None:
                result[parameter.name] = default
                continue
            result[parameter.name] = (
                default
                if isinstance(default, int)
                else default_resolver._eval_constant_integer(
                    str(default),
                    description=(
                        f"module parameter '{parameter.name}' declared default"
                    ),
                    allow_zero=True,
                    allow_negative=True,
                )
            )
        return result

    interface_defaults = normalized_defaults(declaration.parameters)
    module_defaults = normalized_defaults(module_parameters)
    expected_parameter_shape = tuple(
        (item.name, item.kind, interface_defaults[item.name])
        for item in declaration.parameters
    )
    actual_parameter_shape = tuple(
        (item.name, item.kind, module_defaults[item.name])
        for item in module_parameters
    )
    if actual_parameter_shape != expected_parameter_shape:
        semantic_hierarchy.signature_error(
            f"module '{source_module.name}' parameter contract "
            f"{actual_parameter_shape} does not exactly match interface "
            f"'{declaration.name}' {expected_parameter_shape}",
            code="ZL-INTERFACE-PARAMETERS",
        )

    by_name = {item.name: item for item in declaration.parameters}
    assigned: dict[str, object] = {}
    positional = 0
    for argument in reference.arguments:
        if argument.name is None:
            while (
                positional < len(declaration.parameters)
                and declaration.parameters[positional].name in assigned
            ):
                positional += 1
            if positional >= len(declaration.parameters):
                semantic_hierarchy.signature_error(
                    f"too many interface arguments for '{declaration.name}'",
                    code="ZL-INTERFACE-PARAMETERS",
                )
            name = declaration.parameters[positional].name
            positional += 1
        else:
            name = argument.name
        if name not in by_name:
            semantic_hierarchy.signature_error(
                f"unknown interface parameter '{name}' on '{declaration.name}'",
                code="ZL-INTERFACE-PARAMETERS",
            )
        if name in assigned:
            semantic_hierarchy.signature_error(
                f"interface parameter '{name}' is assigned more than once",
                code="ZL-INTERFACE-PARAMETERS",
            )
        assigned[name] = argument.value

    value_bindings: dict[str, int] = {}
    type_bindings: dict[str, ir_types.HardwareType] = {}
    records: list[ir_module.ModuleSignatureParameter] = []
    for parameter in declaration.parameters:
        value = assigned.get(parameter.name, parameter.default)
        if value is None:
            semantic_hierarchy.signature_error(
                f"missing required interface argument '{parameter.name}' for "
                f"'{declaration.name}'",
                code="ZL-INTERFACE-PARAMETERS",
            )
        if parameter.kind == "type":
            if isinstance(value, int):
                semantic_hierarchy.signature_error(
                    f"interface type parameter '{parameter.name}' requires a type",
                    code="ZL-INTERFACE-PARAMETERS",
                )
            syntax = value if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)) else ast.TypeName(str(value))
            try:
                resolved = resolver.resolve(syntax)
            except SemanticError as error:
                semantic_hierarchy.signature_error(
                    f"cannot resolve interface type argument '{parameter.name}': {error}",
                    code="ZL-INTERFACE-PARAMETERS",
                )
            type_bindings[parameter.name] = resolved
            records.append(
                ir_module.ModuleSignatureParameter(
                    parameter.name, "type", str(resolved), None
                )
            )
            continue
        if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)):
            semantic_hierarchy.signature_error(
                f"interface value parameter '{parameter.name}' requires an integer",
                code="ZL-INTERFACE-PARAMETERS",
            )
        try:
            resolved_value = (
                value
                if isinstance(value, int)
                else resolver._eval_constant_integer(
                    str(value),
                    description=f"interface value parameter '{parameter.name}'",
                    allow_zero=True,
                    allow_negative=True,
                )
            )
        except SemanticError as error:
            semantic_hierarchy.signature_error(
                f"cannot resolve interface value argument '{parameter.name}': {error}",
                code="ZL-INTERFACE-PARAMETERS",
            )
        assert isinstance(resolved_value, int)
        declared_default = interface_defaults[parameter.name]
        value_bindings[parameter.name] = resolved_value
        records.append(
            ir_module.ModuleSignatureParameter(
                parameter.name, "value", resolved_value, declared_default
            )
        )

    effective_types = specialization_type_bindings or {}
    effective_parameters: list[ir_module.ModuleSignatureParameter] = []
    effective_source_parameters = {
        item.name: item for item in source_module.parameters
    }
    for parameter in module_parameters:
        if parameter.kind == "type":
            resolved = effective_types.get(parameter.name)
            if resolved is None:
                try:
                    resolved = resolver.resolve(ast.TypeName(parameter.name))
                except SemanticError:
                    semantic_hierarchy.signature_error(
                        f"module type parameter '{parameter.name}' is not specialized",
                        code="ZL-INTERFACE-PARAMETERS",
                    )
            effective_parameters.append(
                ir_module.ModuleSignatureParameter(
                    parameter.name, "type", str(resolved), None
                )
            )
        else:
            effective = effective_source_parameters.get(parameter.name, parameter)
            if effective.default is None:
                semantic_hierarchy.signature_error(
                    f"module value parameter '{parameter.name}' is not specialized",
                    code="ZL-INTERFACE-PARAMETERS",
                )
            value = effective.default
            assert value is not None
            resolved_value = (
                value
                if isinstance(value, int)
                else resolver._eval_constant_integer(
                    str(value),
                    description=f"module value parameter '{parameter.name}'",
                    allow_zero=True,
                    allow_negative=True,
                )
            )
            effective_parameters.append(
                ir_module.ModuleSignatureParameter(
                    parameter.name,
                    "value",
                    resolved_value,
                    module_defaults[parameter.name],
                )
            )
    if tuple(effective_parameters) != tuple(records):
        semantic_hierarchy.signature_error(
            f"module '{source_module.name}' applied parameter values "
            f"{tuple(effective_parameters)} do not match interface "
            f"'{declaration.name}' {tuple(records)}",
            code="ZL-INTERFACE-PARAMETERS",
        )
    return tuple(records), value_bindings, type_bindings


def _signature_port(
    declaration: ast.PortDecl,
    name: str,
    resolver: Any,
    clock_domains: tuple[ir_cdc.ClockDomain, ...],
) -> ir_module.Port:
    if declaration.registered:
        semantic_hierarchy.signature_error(
            f"module interface port '{name}' cannot declare implementation storage"
        )
    if declaration.initializer is not None:
        semantic_hierarchy.signature_error(
            f"module interface port '{name}' cannot have an initializer"
        )
    syntax = declaration.type_name
    if isinstance(syntax, ast.InterfaceTypeName):
        protocol = interface_protocol(syntax.kind)
        type_syntax = syntax.payload_type
        capacity = syntax.capacity
        virtual_channels = syntax.virtual_channels
    else:
        protocol = ir_interfaces.InterfaceProtocol.WIRE
        type_syntax = syntax
        capacity = None
        virtual_channels = None
    default_domain = clock_domains[0].clock if len(clock_domains) == 1 else None
    domain = declaration.domain or default_domain
    declared_domains = {item.clock for item in clock_domains}
    if declaration.domain is not None and declaration.domain not in declared_domains:
        semantic_hierarchy.signature_error(
            f"module interface port '{name}' references unknown domain "
            f"'{declaration.domain}'",
            code="ZL-INTERFACE-DOMAIN",
        )
    if len(clock_domains) > 1 and domain is None:
        semantic_hierarchy.signature_error(
            f"module interface port '{name}' requires an explicit domain",
            code="ZL-INTERFACE-DOMAIN",
        )
    return ir_module.Port(
        ir_module.PortDirection.INPUT
        if declaration.direction is ast.Direction.INPUT
        else ir_module.PortDirection.OUTPUT,
        name,
        resolver.resolve(type_syntax),
        protocol,
        capacity,
        domain,
        virtual_channels,
    )


def _signature_aggregate_endpoint(
    aggregate: ast.AggregateInterfaceDecl,
    *,
    protocols: tuple[ast.ProtocolDecl, ...],
    source_module: ast.Module,
    resolver: Any,
    clock_domains: tuple[ir_cdc.ClockDomain, ...],
    identity_namespace: str,
) -> ir_module.AggregateProtocolEndpoint:
    declaration = next(
        (item for item in protocols if item.name == aggregate.protocol), None
    )
    if declaration is None:
        semantic_hierarchy.signature_error(
            f"unknown protocol '{aggregate.protocol}' in module interface",
            code="ZL-INTERFACE-PROTOCOL",
        )
    assert declaration is not None
    if aggregate.role not in declaration.roles:
        semantic_hierarchy.signature_error(
            f"protocol '{aggregate.protocol}' has no role '{aggregate.role}'",
            code="ZL-INTERFACE-PROTOCOL",
        )
    arguments: dict[str, int | str] = {
        item.name: item.default
        for item in declaration.parameters
        if item.default is not None
    }
    type_arguments: dict[str, ir_types.HardwareType] = {}
    assigned: set[str] = set()
    positional = 0
    for argument in aggregate.arguments:
        if argument.name is None:
            if positional >= len(declaration.parameters):
                semantic_hierarchy.signature_error(
                    f"too many arguments for protocol '{aggregate.protocol}'",
                    code="ZL-INTERFACE-PROTOCOL",
                )
            parameter = declaration.parameters[positional]
            positional += 1
        else:
            parameter = next(
                (
                    item for item in declaration.parameters
                    if item.name == argument.name
                ),
                None,
            )
            if parameter is None:
                semantic_hierarchy.signature_error(
                    f"unknown protocol parameter '{argument.name}'",
                    code="ZL-INTERFACE-PROTOCOL",
                )
        assert parameter is not None
        if parameter.name in assigned:
            semantic_hierarchy.signature_error(
                f"protocol parameter '{parameter.name}' is assigned more than once",
                code="ZL-INTERFACE-PROTOCOL",
            )
        assigned.add(parameter.name)
        if parameter.kind == "type":
            value = argument.value
            syntax = value if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)) else ast.TypeName(str(value))
            type_arguments[parameter.name] = resolver.resolve(syntax)
        else:
            value = argument.value
            if isinstance(value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)):
                semantic_hierarchy.signature_error(
                    f"protocol value parameter '{parameter.name}' requires an integer",
                    code="ZL-INTERFACE-PROTOCOL",
                )
            arguments[parameter.name] = (
                value
                if isinstance(value, int)
                else resolver._eval_constant_integer(
                    str(value),
                    description=f"protocol parameter '{parameter.name}'",
                    allow_zero=True,
                    allow_negative=True,
                )
            )
    missing = next(
        (
            item for item in declaration.parameters
            if item.kind == "type" and item.name not in type_arguments
            or item.kind == "value" and item.name not in arguments
        ),
        None,
    )
    if missing is not None:
        semantic_hierarchy.signature_error(
            f"missing protocol argument '{missing.name}' for '{aggregate.protocol}'",
            code="ZL-INTERFACE-PROTOCOL",
        )
    specialized = type_resolution.TypeResolver(
        source_module.type_aliases,
        source_module.structs,
        source_module.enums,
        declaration.parameters,
        arguments,
        type_arguments,
        identity_namespace,
        tagged_unions=source_module.tagged_unions,
    )
    default_domain = clock_domains[0].clock if len(clock_domains) == 1 else None
    endpoint_domain = aggregate.domain or default_domain
    declared_domains = {item.clock for item in clock_domains}
    if aggregate.domain is not None and aggregate.domain not in declared_domains:
        semantic_hierarchy.signature_error(
            f"aggregate endpoint '{aggregate.name}' references unknown domain "
            f"'{aggregate.domain}'",
            code="ZL-INTERFACE-DOMAIN",
        )
    if len(clock_domains) > 1 and endpoint_domain is None:
        semantic_hierarchy.signature_error(
            f"aggregate endpoint '{aggregate.name}' requires an explicit domain",
            code="ZL-INTERFACE-DOMAIN",
        )
    members: list[ir_module.ProtocolMember] = []
    seen_members: set[str] = set()
    for channel in declaration.channels:
        if channel.name in seen_members:
            semantic_hierarchy.signature_error(
                f"protocol '{declaration.name}' has duplicate member '{channel.name}'",
                code="ZL-INTERFACE-PROTOCOL",
            )
        seen_members.add(channel.name)
        syntax = channel.type_name
        if isinstance(syntax, ast.InterfaceTypeName):
            protocol = interface_protocol(syntax.kind)
            payload = specialized.resolve(syntax.payload_type)
        else:
            protocol = ir_interfaces.InterfaceProtocol.WIRE
            payload = specialized.resolve(syntax)
        member_domain = channel.domain or endpoint_domain
        if member_domain is not None and member_domain not in declared_domains:
            semantic_hierarchy.signature_error(
                f"protocol member '{aggregate.name}.{channel.name}' references "
                f"unknown domain '{member_domain}'",
                code="ZL-INTERFACE-DOMAIN",
            )
        members.append(
            ir_module.ProtocolMember(
                channel.name,
                protocol,
                payload,
                channel.source_role,
                channel.sink_role,
                member_domain,
            )
        )
    specialization_identity = (
        f"{aggregate.protocol}<"
        + ",".join(
            f"{parameter.name}="
            f"{type_arguments.get(parameter.name, arguments.get(parameter.name, parameter.default))}"
            for parameter in declaration.parameters
        )
        + ">"
    )
    return ir_module.AggregateProtocolEndpoint(
        aggregate.name,
        aggregate.protocol,
        aggregate.role,
        tuple(members),
        endpoint_domain,
        specialization_identity,
    )


def named_module_signature(
    source_module: ast.Module,
    *,
    resolver: Any,
    specialization_type_bindings: dict[str, ir_types.HardwareType] | None,
    actual_ports: tuple[ir_module.Port, ...],
    actual_clock_domains: tuple[ir_cdc.ClockDomain, ...],
    actual_request_responses: tuple[ir_module.RequestResponseInterface, ...],
    actual_aggregate_endpoints: tuple[ir_module.AggregateProtocolEndpoint, ...],
    actual_timing: ir_timing.ModuleTimingContract | None,
    source_unit: str | None,
    source_digest: str | None,
    source_digests: dict[str, str],
) -> ir_module.ModuleSignature | None:
    reference = source_module.conforms_to
    if reference is None:
        return None
    declarations = {
        item.name: item for item in source_module.module_interfaces
    }
    if len(declarations) != len(source_module.module_interfaces):
        duplicate = next(
            item.name for item in source_module.module_interfaces
            if sum(other.name == item.name for other in source_module.module_interfaces) > 1
        )
        semantic_hierarchy.signature_error(
            f"duplicate module interface declaration '{duplicate}'",
            code="ZL-INTERFACE-DUPLICATE",
        )
    declaration = declarations.get(reference.name)
    if declaration is None:
        semantic_hierarchy.signature_error(
            f"unknown module interface '{reference.name}'",
            code="ZL-INTERFACE-UNKNOWN",
        )
    assert declaration is not None
    declaration_digest = (
        source_digests.get(declaration.source_identity)
        if declaration.source_identity is not None
        else source_digest
    )
    if declaration.request_responses:
        semantic_hierarchy.signature_error(
            "named module interfaces do not yet accept request_response members "
            "because the requester/responder role is inferred from behavior",
            code="ZL-INTERFACE-UNSUPPORTED",
        )
    explicit_resets = declaration.reset_domains or tuple(
        (name, None) for name in declaration.resets
    )
    module_resets = source_module.reset_domains or tuple(
        (name, None) for name in source_module.resets
    )
    if declaration.clocks != source_module.clocks or explicit_resets != module_resets:
        semantic_hierarchy.signature_error(
            f"module '{source_module.name}' clock/reset declarations do not "
            f"exactly match interface '{declaration.name}'",
            code="ZL-INTERFACE-DOMAIN",
        )
    expected_clock_physical = declaration.clock_physical or tuple(
        ast.ClockPhysicalDecl(name) for name in declaration.clocks
    )
    expected_reset_physical = declaration.reset_physical or tuple(
        ast.ResetPhysicalDecl(name, domain) for name, domain in explicit_resets
    )
    actual_clock_physical = source_module.clock_physical or tuple(
        ast.ClockPhysicalDecl(name) for name in source_module.clocks
    )
    actual_reset_physical = source_module.reset_physical or tuple(
        ast.ResetPhysicalDecl(name, domain) for name, domain in module_resets
    )
    if (
        expected_clock_physical != actual_clock_physical
        or expected_reset_physical != actual_reset_physical
    ):
        semantic_hierarchy.signature_error(
            f"module '{source_module.name}' physical clock/reset declarations do not "
            f"exactly match interface '{declaration.name}'",
            code="ZL-INTERFACE-DOMAIN",
        )
    clock_domains = semantic_hierarchy.interface_clock_domains(declaration)
    parameters, parameter_values, parameter_types = _applied_interface_parameters(
        source_module,
        declaration,
        reference,
        resolver,
        specialization_type_bindings,
    )
    interface_resolver = type_resolution.TypeResolver(
        source_module.type_aliases,
        source_module.structs,
        source_module.enums,
        declaration.parameters,
        parameter_values,
        parameter_types,
        declaration.source_identity or source_unit or source_module.name,
        tagged_unions=source_module.tagged_unions,
    )
    expected_ports: list[ir_module.Port] = []
    for port in declaration.ports:
        names = port.names or (port.name,)
        if port.initializer is not None:
            semantic_hierarchy.signature_error(
                f"module interface port '{port.name}' cannot have an initializer"
            )
        for name in names:
            expected_ports.append(
                _signature_port(port, name, interface_resolver, clock_domains)
            )
    duplicate_port = next(
        (
            port.name for port in expected_ports
            if sum(other.name == port.name for other in expected_ports) > 1
        ),
        None,
    )
    if duplicate_port is not None:
        semantic_hierarchy.signature_error(
            f"module interface '{declaration.name}' has duplicate port "
            f"'{duplicate_port}'"
        )
    expected_aggregates = tuple(
        _signature_aggregate_endpoint(
            endpoint,
            protocols=source_module.protocols,
            source_module=source_module,
            resolver=interface_resolver,
            clock_domains=clock_domains,
            identity_namespace=(
                declaration.source_identity or source_unit or source_module.name
            ),
        )
        for endpoint in declaration.aggregate_interfaces
    )
    if len({item.name for item in expected_aggregates}) != len(expected_aggregates):
        semantic_hierarchy.signature_error(
            f"module interface '{declaration.name}' has duplicate aggregate endpoints"
        )
    timing: ir_timing.ModuleTimingContract | None = None
    if declaration.timing is not None:
        if declaration.timing.initiation_interval != 1:
            semantic_hierarchy.signature_error(
                "module interface timing currently requires ii 1",
                code="ZL-INTERFACE-TIMING",
            )
        if declaration.timing.latency > 0 and len(clock_domains) != 1:
            semantic_hierarchy.signature_error(
                "positive module interface latency requires one clock/reset domain",
                code="ZL-INTERFACE-TIMING",
            )
        domain = clock_domains[0] if len(clock_domains) == 1 else None
        origin = (
            SourceOrigin(
                declaration.timing.origin,
                f"module interface timing {declaration.name}",
                declaration.source_identity or source_unit,
                declaration_digest,
            )
            if declaration.timing.origin is not None
            else None
        )
        timing = ir_timing.ModuleTimingContract(
            declaration.timing.latency,
            declaration.timing.initiation_interval,
            None if domain is None else domain.clock,
            None if domain is None else domain.reset,
            origin,
        )
    source_origin = (
        SourceOrigin(
            declaration.origin,
            f"module interface {declaration.name}",
            declaration.source_identity or source_unit,
            declaration_digest,
        )
        if declaration.origin is not None
        else None
    )
    expected = ir_module.ModuleSignature(
        declaration.name,
        parameters,
        tuple(expected_ports),
        clock_domains,
        (),
        expected_aggregates,
        timing,
        (
            f"{declaration.source_identity or source_unit or 'compilation:' + source_module.name}"
            f"::interface::{declaration.name}"
        ),
        source_origin,
    )
    actual = ir_module.ModuleSignature(
        declaration.name,
        parameters,
        actual_ports,
        actual_clock_domains,
        actual_request_responses,
        actual_aggregate_endpoints,
        actual_timing,
        expected.declaration_identity,
    )
    if actual.ports != expected.ports:
        expected_by_name = {item.name: item for item in expected.ports}
        actual_by_name = {item.name: item for item in actual.ports}
        missing = tuple(name for name in expected_by_name if name not in actual_by_name)
        extra = tuple(name for name in actual_by_name if name not in expected_by_name)
        if missing or extra:
            semantic_hierarchy.signature_error(
                f"module '{source_module.name}' port set differs from interface "
                f"'{declaration.name}': missing={missing}, extra={extra}"
            )
        expected_order = tuple(item.name for item in expected.ports)
        actual_order = tuple(item.name for item in actual.ports)
        if actual_order != expected_order:
            semantic_hierarchy.signature_error(
                f"module '{source_module.name}' port order {actual_order} does "
                f"not match interface '{declaration.name}' {expected_order}"
            )
        mismatch = next(
            name for name in expected_by_name
            if expected_by_name[name] != actual_by_name[name]
        )
        semantic_hierarchy.signature_error(
            f"module '{source_module.name}' port '{mismatch}' is "
            f"{actual_by_name[mismatch]}, expected exact {expected_by_name[mismatch]}"
        )
    if actual.clock_domains != expected.clock_domains:
        semantic_hierarchy.signature_error(
            f"module '{source_module.name}' clock/reset domains do not match "
            f"interface '{declaration.name}'",
            code="ZL-INTERFACE-DOMAIN",
        )
    if actual.request_responses != expected.request_responses:
        semantic_hierarchy.signature_error(
            f"module '{source_module.name}' request_response ABI is not declared "
            f"by interface '{declaration.name}'",
            code="ZL-INTERFACE-PROTOCOL",
        )
    if actual.aggregate_protocol_endpoints != expected.aggregate_protocol_endpoints:
        semantic_hierarchy.signature_error(
            f"module '{source_module.name}' aggregate protocol endpoints do not "
            f"exactly match interface '{declaration.name}'",
            code="ZL-INTERFACE-PROTOCOL",
        )
    if actual.timing_contract != expected.timing_contract:
        semantic_hierarchy.signature_error(
            f"module '{source_module.name}' timing contract "
            f"{actual.timing_contract} does not match interface "
            f"'{declaration.name}' {expected.timing_contract}",
            code="ZL-INTERFACE-TIMING",
        )
    return expected


class NamedInterfaceAnalyzer:
    """Validate named interfaces and apply one selected public surface."""

    def analyze(self, module: ast.Module) -> ast.Module:
        names: set[str] = set()
        for declaration in module.module_interfaces:
            if declaration.name in names:
                raise SemanticError(
                    f"duplicate module interface declaration '{declaration.name}'",
                    code="ZL-INTERFACE-DUPLICATE",
                )
            names.add(declaration.name)
            initialized = next(
                (port.name for port in declaration.ports if port.initializer is not None),
                None,
            )
            if initialized is not None:
                raise SemanticError(
                    f"module interface port '{initialized}' cannot have an initializer",
                    code="ZL-INTERFACE-CONFORMANCE",
                )
            if declaration.request_responses:
                raise SemanticError(
                    "named module interfaces do not yet accept request_response "
                    "members because the requester/responder role is inferred "
                    "from behavior",
                    code="ZL-INTERFACE-UNSUPPORTED",
                )
            semantic_hierarchy.interface_clock_domains(declaration)
        if module.conforms_to is None:
            return module
        active = next(
            (
                item for item in module.module_interfaces
                if item.name == module.conforms_to.name
            ),
            None,
        )
        if active is None:
            raise SemanticError(
                f"unknown module interface '{module.conforms_to.name}'",
                code="ZL-INTERFACE-UNKNOWN",
            )
        module = inherit_applied_interface_surface(module, active)
        if module.external_model is None:
            return module
        if active.parameters or module.conforms_to.arguments:
            raise SemanticError(
                f"external module '{module.name}' requires a non-parameterized "
                "named interface in this first slice",
                code="ZL-EXTERN-UNSUPPORTED",
            )
        if active.clocks or active.resets:
            raise SemanticError(
                f"external module '{module.name}' cannot expose clock/reset "
                "in this first slice",
                code="ZL-EXTERN-UNSUPPORTED",
            )
        non_scalar = (
            active.request_responses
            or active.aggregate_interfaces
            or any(
                isinstance(port.type_name, ast.InterfaceTypeName)
                and port.type_name.kind is not ast.InterfaceKind.WIRE
                for port in active.ports
            )
        )
        if non_scalar:
            raise SemanticError(
                f"external module '{module.name}' supports scalar wire ports only",
                code="ZL-EXTERN-UNSUPPORTED",
            )
        timing = active.timing
        if timing is not None and (
            timing.latency != 0 or timing.initiation_interval != 1
        ):
            raise SemanticError(
                f"external module '{module.name}' requires timing latency 0 ii 1",
                code="ZL-EXTERN-UNSUPPORTED",
            )
        inputs = tuple(
            port for port in module.ports if port.direction is ast.Direction.INPUT
        )
        outputs = tuple(
            port for port in module.ports if port.direction is ast.Direction.OUTPUT
        )
        if not inputs or len(outputs) != 1:
            raise SemanticError(
                f"external module '{module.name}' requires one or more inputs "
                "and exactly one output",
                code="ZL-EXTERN-SIGNATURE",
            )
        model_call = ast.CallExpr(
            module.external_model,
            tuple(ast.NameExpr(port.name) for port in inputs),
            origin=module.external_origin,
        )
        model_assignment = ast.Assignment(
            outputs[0].name, model_call, origin=module.external_origin
        )
        return replace(
            module,
            assignments=(model_assignment,),
            ordered_items=(*module.ordered_items, model_assignment),
        )
