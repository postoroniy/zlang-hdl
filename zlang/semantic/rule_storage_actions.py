# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Type and admit one scheduled FIFO or memory action in its rule domain."""

from __future__ import annotations

from dataclasses import replace
from zlang.ast import nodes as ast
from zlang.ir import (
    expressions as ir_expr,
    state as ir_state,
    storage as ir_storage,
    types as ir_types,
)
from zlang.source import SourceOrigin

from .context import ExpressionContext
from .errors import SemanticError
from .storage_symbols import FifoSymbol, MemorySymbol
from .storage_validation import StorageDeclarationProduct


def lower_storage_action(
    action: ast.ResourceAction,
    action_origin: SourceOrigin | None,
    rule_name: str,
    rule_domain: str | None,
    action_context: ExpressionContext,
    value_symbols: dict[str, object],
    storage_declarations: StorageDeclarationProduct,
    scheduled_fifo_names: frozenset[str],
) -> tuple[
    ir_state.StateActionKind, str, tuple[ir_expr.Expression, ...], SourceOrigin | None
]:
    resource = storage_declarations.resource_symbols.get(action.resource)
    if resource is None:
        raise SemanticError(
            f"rule '{rule_name}' references unknown state resource '{action.resource}'",
            primary=action_origin,
        )
    if resource.domain != rule_domain:
        raise SemanticError(
            f"clock-domain mismatch in rule '{rule_name}': "
            f"rule domain is '{rule_domain}', resource "
            f"'{resource.name}' belongs to '{resource.domain}'",
            code="ZL-DOMAIN-CROSSING",
            primary=action_origin,
        )
    resource_kind = "FIFO" if isinstance(resource, FifoSymbol) else "memory"
    origin = (
        replace(
            action_origin,
            construct=f"{resource_kind} {resource.name}.{action.operation}",
        )
        if action_origin is not None
        else None
    )
    if isinstance(resource, FifoSymbol):
        if resource.name not in scheduled_fifo_names:
            raise SemanticError(
                f"FIFO '{resource.name}' is not owned by scheduled rule actions",
                primary=action_origin,
            )
        if action.operation == "push":
            if len(action.operands) != 1:
                raise SemanticError(
                    "FIFO push requires exactly one payload", primary=action_origin
                )
            operand = action_context.expressions.check_typed_boundary(
                action.operands[0],
                value_symbols,
                resource.element_type,
                action_context,
            )
            if operand.type != resource.element_type:
                raise SemanticError(
                    f"FIFO '{resource.name}' push has type {operand.type}, expected {resource.element_type}",
                    primary=action_origin,
                )
            kind = ir_state.StateActionKind.FIFO_PUSH
            operands = (operand,)
        elif action.operation == "pop":
            if action.operands:
                raise SemanticError(
                    "FIFO pop does not accept operands", primary=action_origin
                )
            kind = ir_state.StateActionKind.FIFO_POP
            operands = ()
        else:
            raise SemanticError(
                f"FIFO '{resource.name}' has no rule action '{action.operation}'",
                primary=action_origin,
            )
    elif isinstance(resource, MemorySymbol):
        if resource.name not in storage_declarations.scheduled_memory_names:
            raise SemanticError(
                f"memory '{resource.name}' is not owned by scheduled rule actions",
                primary=action_origin,
            )
        address_type = ir_types.UIntType(resource.address_width)
        if action.operation == "read":
            if len(action.operands) != 1:
                raise SemanticError(
                    "memory read requires exactly one address operand",
                    primary=action_origin,
                )
            address = action_context.expressions.check(
                action.operands[0],
                value_symbols,
                address_type,
                action_context,
            )
            if address.type != address_type:
                raise SemanticError(
                    f"memory '{resource.name}' read address has type {address.type}, expected {address_type}",
                    primary=action_origin,
                )
            kind = ir_state.StateActionKind.MEMORY_READ_REQUEST
            operands = (address,)
        elif action.operation == "write":
            if len(action.operands) not in {2, 3}:
                raise SemanticError(
                    "memory write requires exactly address and data operands, "
                    "with an optional byte mask",
                    primary=action_origin,
                )
            address = action_context.expressions.check(
                action.operands[0],
                value_symbols,
                address_type,
                action_context,
            )
            data = action_context.expressions.check_typed_boundary(
                action.operands[1],
                value_symbols,
                resource.element_type,
                action_context,
            )
            if address.type != address_type:
                raise SemanticError(
                    f"memory '{resource.name}' write address has type {address.type}, expected {address_type}",
                    primary=action_origin,
                )
            if data.type != resource.element_type:
                raise SemanticError(
                    f"memory '{resource.name}' write data has type {data.type}, expected {resource.element_type}",
                    primary=action_origin,
                )
            memory_masked = (
                resource.name in storage_declarations.scheduled_masked_memory_names
            )
            if memory_masked:
                mask_type = ir_types.BitsType(
                    ir_storage.memory_byte_mask_width(resource.element_type.width)
                )
                if len(action.operands) == 3:
                    mask = action_context.expressions.check(
                        action.operands[2],
                        value_symbols,
                        mask_type,
                        action_context,
                    )
                    if mask.type != mask_type:
                        raise SemanticError(
                            f"memory '{resource.name}' write mask has type "
                            f"{mask.type}, expected {mask_type}",
                            primary=action_origin,
                        )
                else:
                    mask = ir_expr.Constant((1 << mask_type.width) - 1, mask_type)
            kind = ir_state.StateActionKind.MEMORY_WRITE
            operands = (address, data, mask) if memory_masked else (address, data)
        else:
            raise SemanticError(
                f"memory '{resource.name}' has no rule action '{action.operation}'",
                primary=action_origin,
            )
    else:
        raise SemanticError(
            f"state resource '{resource.name}' does not support rule actions",
            primary=action_origin,
        )
    return kind, resource.name, operands, origin
