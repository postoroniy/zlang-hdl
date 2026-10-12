"""Typed scalar/protocol port preparation for semantic analysis."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ast import nodes as ast
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin
from . import observations
from . import module_interfaces
from . import type_resolution
from .context import ExpressionContext
from .errors import SemanticError


@dataclass(frozen=True)
class PortAnalysisProduct:
    symbols: dict[str, ir_module.Port]
    ports: tuple[ir_module.Port, ...]
    origins: dict[str, SourceOrigin | None]


def analyze_ports(
    module: ast.Module,
    *,
    type_resolver: type_resolution.TypeResolver,
    expression_context: ExpressionContext,
    default_clock: str | None,
    clock_domain_count: int,
    allow_external_enum_inputs: bool,
) -> PortAnalysisProduct:
    """Resolve exact public ports without owning other hardware resources."""

    symbols: dict[str, ir_module.Port] = {}
    ports: list[ir_module.Port] = []
    origins: dict[str, SourceOrigin | None] = {}
    for declaration in module.ports:
        if declaration.name in symbols:
            raise SemanticError(f"duplicate port '{declaration.name}'")
        direction = (
            ir_module.PortDirection.INPUT
            if declaration.direction is ast.Direction.INPUT
            else ir_module.PortDirection.OUTPUT
        )
        syntax = declaration.type_name
        if isinstance(syntax, ast.InterfaceTypeName):
            protocol = module_interfaces.interface_protocol(syntax.kind)
            payload_syntax = syntax.payload_type
            capacity = syntax.capacity
            virtual_channels = syntax.virtual_channels
        else:
            protocol = ir_interfaces.InterfaceProtocol.WIRE
            payload_syntax = syntax
            capacity = None
            virtual_channels = None
        origin = observations.declaration_origin(
            declaration.name_origins[0]
            if declaration.name_origins else declaration.origin,
            f"port {declaration.name}",
            expression_context,
        )
        port = ir_module.Port(
            direction=direction,
            name=declaration.name,
            type=type_resolver.resolve(payload_syntax),
            protocol=protocol,
            capacity=capacity,
            domain=(
                declaration.domain
                if declaration.domain is not None
                else default_clock
            ),
            virtual_channels=virtual_channels,
            registered=declaration.registered,
            source_origin=origin,
        )
        if (
            direction is ir_module.PortDirection.INPUT
            and not allow_external_enum_inputs
            and type_resolution.contains_nominal_type(port.type, ir_types.EnumType)
        ):
            message = (
                f"top-level input '{port.name}' cannot expose enum type "
                f"{port.type.name}; use an internal child interface"
                if isinstance(port.type, ir_types.EnumType)
                else f"top-level input '{port.name}' cannot expose type "
                f"{port.type} because it contains an enum-valued field; "
                "use an internal child interface"
            )
            raise SemanticError(message)
        if protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT:
            if (
                virtual_channels is None
                or virtual_channels < 2
                or virtual_channels & (virtual_channels - 1)
            ):
                raise SemanticError(
                    f"vc_credit interface '{port.name}' virtual-channel count "
                    "must be a power of two and at least 2"
                )
            if capacity is None or capacity < 1:
                raise SemanticError(
                    f"vc_credit interface '{port.name}' requires at least one "
                    "credit per virtual channel"
                )
        if declaration.domain is not None and declaration.domain not in module.clocks:
            raise SemanticError(
                f"port '{declaration.name}' references unknown clock domain "
                f"'{declaration.domain}'"
            )
        if clock_domain_count > 1 and port.domain is None:
            raise SemanticError(
                f"port '{declaration.name}' requires an explicit clock domain"
            )
        symbols[port.name] = port
        origins[port.name] = origin
        observations.remember_definition_target(
            expression_context,
            port,
            origin,
            name=declaration.name,
            kind="port",
        )
        ports.append(port)
    return PortAnalysisProduct(symbols, tuple(ports), origins)


__all__ = ["PortAnalysisProduct", "analyze_ports"]
