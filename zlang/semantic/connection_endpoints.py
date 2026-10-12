# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned direct connection lowering and protocol-cycle validation."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from zlang.ast import nodes as ast
from zlang.ir import module as ir_module
from zlang.ir import interfaces as ir_interfaces
from zlang.source import SourceSpan

from . import expression_support
from .errors import SemanticError
from . import observations as semantic_observations

@dataclass
class HierarchicalEndpointResolver:
    """Resolve exact protocol endpoints after child elaboration."""

    module: ast.Module
    expression_context: Any
    symbols: dict[str, ir_module.Port]
    instances: tuple[ir_module.Instance, ...]
    child_irs: dict[str, ir_module.Module]
    protocol_endpoints: list[ir_module.ProtocolEndpoint]

    def concrete(self, path: str) -> str:
        match = re.fullmatch(
            r"(?P<owner>[A-Za-z_][A-Za-z0-9_]*)"
            r"(?:\[(?P<index>[A-Za-z_][A-Za-z0-9_]*|[0-9]+)\])?"
            r"(?P<tail>(?:\.[A-Za-z_][A-Za-z0-9_]*)*)",
            path,
        )
        if match is None:
            raise SemanticError(
                f"invalid hierarchical protocol endpoint '{path}'"
            )
        owner = match.group("owner")
        selector = match.group("index")
        tail = match.group("tail")
        length = self.expression_context.scope.instance_arrays.get(owner)
        if selector is None:
            if length is not None:
                raise SemanticError(
                    f"instance array '{owner}' requires a compile-time index "
                    "before selecting a protocol endpoint"
                )
            return path
        if length is None:
            raise SemanticError(
                f"hierarchical protocol instance '{owner}' is not an instance array"
            )
        syntax_index: int | ast.Expression = (
            int(selector) if selector.isdecimal() else ast.NameExpr(selector)
        )
        index = expression_support._resolve_instance_array_index(
            syntax_index, self.expression_context, array=owner
        )
        if index < 0 or index >= length:
            raise SemanticError(
                f"instance array '{owner}' index {index} is out of range "
                f"0..{length - 1}"
            )
        return f"{owner}[{index}]{tail}"

    def protocol(
        self,
        path: str,
        *,
        source: bool,
        name_origins: tuple[SourceSpan | None, ...] = (),
    ) -> ir_module.ProtocolEndpoint:
        primary = semantic_observations.declaration_origin(
            name_origins[0] if name_origins else None,
            f"protocol endpoint {path}",
            self.expression_context,
        )

        def reject(message: str) -> None:
            raise SemanticError(message, primary=primary)

        path = self.concrete(path)
        parts = path.split(".")
        if len(parts) == 1:
            port = self.symbols.get(parts[0])
            if (
                port is None
                or port.protocol is ir_interfaces.InterfaceProtocol.WIRE
            ):
                reject(f"protocol endpoint '{path}' is not a protocol port")
            assert port is not None
            owner = self.module.name
            name = port.name
            direction = port.direction
            endpoint_type = port.type
            capacity = port.capacity
            domain = port.domain
        elif len(parts) == 2:
            instance = next(
                (item for item in self.instances if item.name == parts[0]), None
            )
            child = self.child_irs.get(parts[0])
            if instance is None or child is None:
                reject(f"unknown hierarchical protocol instance '{parts[0]}'")
            assert instance is not None and child is not None
            port = next(
                (item for item in child.ports if item.name == parts[1]), None
            )
            if port is None:
                reject(f"'{path}' is not a child protocol endpoint")
            assert port is not None
            owner = instance.name
            name = port.name
            direction = port.direction
            endpoint_type = port.type
            capacity = port.capacity
            domain = port.domain
        else:
            reject(f"protocol endpoint path '{path}' is too deep")
            raise AssertionError("unreachable")
        expected = (
            ir_module.PortDirection.INPUT
            if source and len(parts) == 1
            else ir_module.PortDirection.OUTPUT
            if source
            else ir_module.PortDirection.OUTPUT
            if len(parts) == 1
            else ir_module.PortDirection.INPUT
        )
        if direction is not expected:
            reject(f"protocol endpoint '{path}' has wrong direction")
        result = ir_module.ProtocolEndpoint(
            owner,
            name,
            direction,
            port.protocol,
            endpoint_type,
            capacity,
            domain,
        )
        self.protocol_endpoints.append(result)
        return result

    def request_response(
        self,
        path: str,
        *,
        source: bool,
        channel: ir_interfaces.RequestResponseChannel,
    ) -> ir_module.ProtocolEndpoint:
        parts = path.split(".")
        if len(parts) != 2:
            raise SemanticError(
                f"request/response hierarchy endpoint '{path}' must select "
                "an instance interface"
            )
        instance = next(
            (item for item in self.instances if item.name == parts[0]), None
        )
        child = self.child_irs.get(parts[0])
        if instance is None or child is None:
            raise SemanticError(
                f"unknown hierarchical protocol instance '{parts[0]}'"
            )
        interface = next(
            (item for item in child.request_responses if item.name == parts[1]),
            None,
        )
        if interface is None:
            raise SemanticError(
                f"'{path}' is not a child request/response endpoint"
            )
        if interface.max_outstanding <= 0:
            raise SemanticError(
                f"hierarchical request/response endpoint '{path}' requires "
                "a positive max_outstanding"
            )
        if (
            interface.ordering
            is not ir_interfaces.RequestResponseOrdering.IN_ORDER
        ):
            raise SemanticError(
                f"hierarchical request/response endpoint '{path}' requires "
                "ordering in_order"
            )
        requester = (
            interface.role is ir_interfaces.RequestResponseRole.REQUESTER
        )
        is_output = (
            requester
            if channel is ir_interfaces.RequestResponseChannel.REQUEST
            else not requester
        )
        expected = (
            ir_module.PortDirection.OUTPUT
            if source
            else ir_module.PortDirection.INPUT
        )
        if is_output != (expected is ir_module.PortDirection.OUTPUT):
            raise SemanticError(
                f"request/response endpoint '{path}' has wrong "
                "requester/responder direction"
            )
        payload_type = (
            interface.request_type
            if channel is ir_interfaces.RequestResponseChannel.REQUEST
            else interface.response_type
        )
        result = ir_module.ProtocolEndpoint(
            instance.name,
            interface.name,
            expected,
            ir_interfaces.InterfaceProtocol.READY_VALID,
            payload_type,
            None,
            child.clock,
            channel,
        )
        self.protocol_endpoints.append(result)
        return result
