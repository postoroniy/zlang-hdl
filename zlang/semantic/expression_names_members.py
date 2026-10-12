# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative names members expression semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import storage as ir_storage
from zlang.ir import packing as ir_packing
from zlang.ir import types as ir_types
from zlang.source import SourceSpan
from . import callables as semantic_callables
from . import expression_ranges
from . import expression_origins
from . import expression_operators
from . import observations
from . import symbols as semantic_symbols
from . import type_resolution
from .storage_symbols import FifoSymbol, MemorySymbol, RomSymbol
from .errors import SemanticError
from .expression_support import _expand_immutable_locals, _try_resolve_instance_array_index

if TYPE_CHECKING:
    from .context import ExpressionContext

def _check_name_expression(
    expression: ast.Expression,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    if (
        isinstance(expression, ast.FieldExpr)
        and isinstance(expression.expression, ast.NameExpr)
        and context.environment.type_resolver is not None
    ):
        owner = expression.expression.name
        enum_type = context.environment.type_resolver.enum_type(owner)
        if enum_type is not None:
            # Preserve both the owner enum and the exact member occurrence for
            # compiler-owned navigation.  The AST expression keeps its
            # original full qualified provenance for compilation identity.
            owner_span = expression.expression.origin
            if owner_span is not None and owner_span.start_line == owner_span.end_line:
                owner_span = SourceSpan(
                    owner_span.start_line,
                    owner_span.start_column,
                    owner_span.start_line,
                    owner_span.start_column + len(owner),
                )
            type_resolution.record_named_type_definition(
                context,
                ast.TypeName(owner, origin=owner_span),
                context.environment.type_resolver,
            )
            try:
                code = enum_type.member_code(expression.field)
            except ValueError as error:
                raise SemanticError(
                    f"enum '{enum_type.name}' has no member '{expression.field}'"
                ) from error
            if expected is not None and expected != enum_type:
                raise SemanticError(
                    f"enum member {enum_type.name}.{expression.field} has type "
                    f"{enum_type}, expected exact {expected}"
                )
            type_resolution.record_enum_member_definition(
                context, expression, enum_type, context.environment.type_resolver
            )
            return ir_expr.Constant(code, enum_type)
        union_type = context.environment.type_resolver.tagged_union_type(owner)
        if union_type is not None:
            variant = union_type.variant(expression.field)
            if variant is None:
                raise SemanticError(
                    f"tagged union '{union_type.name}' has no variant "
                    f"'{expression.field}'"
                )
            if variant.fields:
                raise SemanticError(
                    f"tagged-union constructor {union_type.name}.{variant.name} "
                    "requires a named field body"
                )
            if expected is not None and expected != union_type:
                raise SemanticError(
                    f"constructor {union_type.name}.{variant.name} has type "
                    f"{union_type}, expected exact {expected}"
                )
            return ir_expr.UnionConstruct(
                variant.name,
                (),
                union_type,
                origin=expression.origin,
            )
    path_parts: list[str] = []
    cursor = expression
    while isinstance(cursor, ast.FieldExpr):
        path_parts.append(cursor.field)
        cursor = cursor.expression
    def csr_projection_parts(
        value: ast.Expression,
    ) -> tuple[str, tuple[str, ...]] | None:
        if isinstance(value, ast.NameExpr):
            return value.name, ()
        if isinstance(value, ast.FieldExpr):
            parent = csr_projection_parts(value.expression)
            if parent is None:
                return None
            return parent[0], (*parent[1], value.field)
        if isinstance(value, ast.IndexExpr):
            parent = csr_projection_parts(value.expression)
            if parent is None:
                return None
            label = parent[1][-1] if parent[1] else parent[0]
            index = _try_resolve_instance_array_index(
                value.index, context, array=label
            )
            if index is None:
                return None
            if parent[1]:
                return parent[0], (*parent[1][:-1], f"{parent[1][-1]}[{index}]")
            return f"{parent[0]}[{index}]", ()
        return None
    decomposed_csr = csr_projection_parts(expression)
    csr_instance: str | None = None
    named_csr_path: tuple[str, ...] = ()
    if decomposed_csr is not None:
        csr_instance, named_csr_path = decomposed_csr
        if "[" in csr_instance:
            array_name = csr_instance.split("[", 1)[0]
            if array_name in context.scope.instance_arrays:
                index = int(csr_instance.split("[", 1)[1].rstrip("]"))
                length = context.scope.instance_arrays[array_name]
                if index < 0 or index >= length:
                    raise SemanticError(
                        f"instance array '{array_name}' index {index} is out of range "
                        f"0..{length - 1}"
                    )
    if csr_instance is not None:
        projection = context.scope.instance_csr_state_paths.get(
            (csr_instance, *named_csr_path)
        )
        if projection is not None:
            port_name, type_, domain = projection
            if expected is not None and expected != type_:
                raise SemanticError(
                    f"CSR field projection '{csr_instance}."
                    f"{'.'.join(named_csr_path)}' has type {type_}, expected "
                    f"exact {expected}"
                )
            return ir_expr.InstanceOutputRef(
                csr_instance,
                port_name,
                type_,
                domain=domain,
            )
        if any(
            key[0] == csr_instance and key[1] == named_csr_path[0]
            for key in context.scope.instance_csr_state_paths
        ):
            raise SemanticError(
                f"CSR child '{csr_instance}' has no stored field projection "
                f"'{'.'.join(named_csr_path)}'"
            )
    if isinstance(cursor, ast.NameExpr):
        full_path = ".".join((cursor.name, *reversed(path_parts)))
        for prefix in sorted(context.scope.aggregate_paths, key=len, reverse=True):
            if full_path == prefix or full_path.startswith(prefix + "."):
                synthetic = context.scope.aggregate_paths[prefix]
                remainder = full_path[len(prefix):].lstrip(".")
                rewritten: ast.Expression = ast.NameExpr(synthetic)
                for field_name in filter(None, remainder.split(".")):
                    rewritten = ast.FieldExpr(rewritten, field_name)
                return context.expressions.check_untraced(rewritten, inputs, expected, context)
    if isinstance(expression, ast.NameExpr):
        if expression.name in context.scope.union_binders:
            projection = context.scope.union_binders[expression.name]
            if expected is not None and projection.type != expected:
                raise SemanticError(
                    f"tagged-union binder '{expression.name}' has type "
                    f"{projection.type}, expected exact {expected}"
                )
            return projection
        if expression.name in context.scope.functional_symbolic_values:
            symbolic = context.scope.functional_symbolic_values[expression.name]
            type_ = expected or context.scope.index_types.get(expression.name)
            if not isinstance(type_, (ir_types.UIntType, ir_types.BitsType)):
                raise SemanticError(
                    f"symbolic functional value '{expression.name}' requires "
                    "an exact unsigned integral type"
                )
            try:
                return ir_expr.FunctionalValue(symbolic, type_)
            except ValueError as error:
                raise SemanticError(str(error)) from error
        if expression.name in context.scope.index_bindings:
            value = context.scope.index_bindings[expression.name]
            type_ = expected or context.scope.index_types.get(
                expression.name, ir_types.UIntType(max(1, value.bit_length()))
            )
            if not isinstance(type_, (ir_types.UIntType, ir_types.BitsType)):
                raise SemanticError(
                    f"compile-time range index '{expression.name}' cannot produce {type_}"
                )
            return ir_expr.Constant(value, type_)
        if expression.name in context.environment.parameters:
            value = context.environment.parameters[expression.name]
            type_ = expected or ir_types.UIntType(max(1, value.bit_length()))
            if not isinstance(type_, (ir_types.UIntType, ir_types.BitsType)):
                raise SemanticError(
                    f"compile-time parameter '{expression.name}' cannot produce {type_}"
                )
            if not expression_operators.constant_fits(value, type_):
                raise SemanticError(
                    f"compile-time parameter '{expression.name}' value {value} does not fit {type_}"
                )
            return ir_expr.Constant(value, type_)
        if expression.name in context.scope.compile_time_constants:
            value = context.scope.compile_time_constants[expression.name]
            if expected is not None and value.type != expected:
                raise SemanticError(
                    f"compile-time constant parameter '{expression.name}' has "
                    f"type {value.type}, expected exact {expected}"
                )
            return value
        symbol = inputs.get(expression.name)
        if symbol is None:
            output = context.scope.write_only_outputs.get(expression.name)
            if (
                output is not None
                and expression.name in context.scope.readable_cdc_outputs
            ):
                return ir_expr.InputRef(expression.name, output.type)
            if output is not None and not context.scope.allow_output_reads:
                raise SemanticError(
                    f"module output '{expression.name}' cannot be read internally; "
                    "drive it from an immutable local and read that local instead",
                    code="ZL-SEMANTIC-OUTPUT-READ",
                    primary=expression_origins.semantic_origin(expression, context),
                    fixes=(
                        "bind the driving expression to an immutable local and use "
                        "that local both internally and for the output assignment",
                    ),
                )
            raise SemanticError(f"unknown input '{expression.name}'")
        if (
            isinstance(symbol, ir_module.Port)
            and symbol.direction is ir_module.PortDirection.OUTPUT
            and symbol.protocol is ir_interfaces.InterfaceProtocol.WIRE
            and not context.scope.allow_output_reads
            and expression.name not in context.scope.readable_cdc_outputs
        ):
            raise SemanticError(
                f"module output '{expression.name}' cannot be read internally; "
                "drive it from an immutable local and read that local instead",
                code="ZL-SEMANTIC-OUTPUT-READ",
                primary=expression_origins.semantic_origin(expression, context),
                fixes=(
                    "bind the driving expression to an immutable local and use "
                    "that local both internally and for the output assignment",
                ),
            )
        observations.record_definition(
            context,
            expression_origins.semantic_origin(expression, context),
            context.services.tooling.definition_targets.get(id(symbol)),
            name=expression.name,
            kind=observations.definition_kind(symbol),
        )
        if isinstance(symbol, ir_expr.Expression):
            return symbol
        if isinstance(symbol, (FifoSymbol, MemorySymbol, RomSymbol)):
            raise SemanticError(
                f"storage resource '{symbol.name}' is not a value; select a field"
            )
        if isinstance(symbol, ir_module.FunctionParameter):
            return ir_expr.ParameterRef(symbol.name, symbol.type)
        if isinstance(symbol, ir_module.Register):
            return ir_expr.RegisterRef(symbol.name, symbol.type)
        if isinstance(symbol, ir_module.LocalValue):
            if symbol.compile_time and isinstance(symbol.expression, ir_expr.Constant):
                return ir_expr.Constant(symbol.expression.value, expected or symbol.type)
            return ir_expr.InputRef(symbol.name, symbol.type)
        if isinstance(symbol, ir_module.RequestResponseInterface):
            raise SemanticError(
                f"request/response interface '{symbol.name}' is not a value; "
                "select request or response and then a protocol field"
            )
        if symbol.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
            raise SemanticError(
                f"ready/valid interface '{symbol.name}' is not a value; "
                "select payload, valid, ready, or transfer"
            )
        if symbol.protocol is ir_interfaces.InterfaceProtocol.CREDIT:
            raise SemanticError(
                f"credit interface '{symbol.name}' is not a value; select "
                "payload, send, return, transfer, or credits"
            )
        if symbol.protocol is ir_interfaces.InterfaceProtocol.PACKET:
            raise SemanticError(
                f"packet interface '{symbol.name}' is not a value; select "
                "payload, valid, ready, last, or transfer"
            )
        if symbol.protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT:
            raise SemanticError(
                f"virtual-channel credit interface '{symbol.name}' is not a "
                "value; select a protocol field"
            )
        return ir_expr.InputRef(symbol.name, symbol.type)
    return None

def _check_protocol_field(
    expression: ast.FieldExpr,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    field_path: list[str] = []
    field_root: ast.Expression = expression
    while isinstance(field_root, ast.FieldExpr):
        field_path.append(field_root.field)
        field_root = field_root.expression
    if isinstance(field_root, ast.NameExpr):
        field_path.append(field_root.name)
        field_path.reverse()
        if len(field_path) >= 3:
            transfer = context.scope.instance_protocol_transfers.get(
                (field_path[0], ".".join(field_path[1:]))
            )
            if transfer is not None:
                return transfer
            projection = context.scope.instance_protocol_outputs.get(
                (field_path[0], ".".join(field_path[1:]))
            )
            if projection is not None:
                scalar_name, signal_type, signal_domain = projection
                return ir_expr.InstanceOutputRef(
                    field_path[0], scalar_name, signal_type,
                    domain=signal_domain,
                )
    if (
        isinstance(expression.expression, ast.FieldExpr)
        and isinstance(expression.expression.expression, ast.NameExpr)
    ):
        interface_name = expression.expression.expression.name
        symbol = inputs.get(interface_name)
        if isinstance(symbol, MemorySymbol) and symbol.ports:
            port_name = expression.expression.field
            port = next(
                (item for item in symbol.ports if item.name == port_name),
                None,
            )
            if port is None:
                raise SemanticError(
                    f"memory '{symbol.name}' has no port '{port_name}'"
                )
            if expression.field not in {"data", "read_data"}:
                raise SemanticError(
                    f"memory port field '{symbol.name}.{port_name}."
                    f"{expression.field}' is write-only or unknown"
                )
            if port.kind not in {
                ast.MemoryPortKind.READ,
                ast.MemoryPortKind.READ_WRITE,
            }:
                raise SemanticError(
                    f"memory port '{symbol.name}.{port_name}' is not readable"
                )
            return ir_expr.MemoryRef(
                symbol.name,
                ir_storage.MemorySignal.READ_DATA,
                symbol.element_type,
                port.name,
            )
        if isinstance(symbol, ir_module.RequestResponseInterface):
            try:
                channel = ir_interfaces.RequestResponseChannel(
                    expression.expression.field
                )
            except ValueError as error:
                raise SemanticError(
                    f"request/response interface '{symbol.name}' has no "
                    f"channel '{expression.expression.field}'"
                ) from error
            try:
                signal = ir_interfaces.ReadyValidSignal(expression.field)
            except ValueError as error:
                raise SemanticError(
                    f"request/response channel '{symbol.name}.{channel.value}' "
                    f"has no field '{expression.field}'"
                ) from error
            if signal is ir_interfaces.ReadyValidSignal.PAYLOAD:
                type_ = (
                    symbol.request_type
                    if channel is ir_interfaces.RequestResponseChannel.REQUEST
                    else symbol.response_type
                )
            else:
                type_ = ir_types.BitType()
            return ir_expr.RequestResponseRef(
                symbol.name, channel, signal, type_
            )
    if isinstance(expression.expression, ast.NameExpr):
        symbol = inputs.get(expression.expression.name)
        if isinstance(symbol, FifoSymbol):
            try:
                signal = ir_storage.FifoSignal(expression.field)
            except ValueError as error:
                raise SemanticError(
                    f"FIFO '{symbol.name}' has no field '{expression.field}'"
                ) from error
            if signal in {
                ir_storage.FifoSignal.DATA,
                ir_storage.FifoSignal.PUSH,
                ir_storage.FifoSignal.POP,
            }:
                raise SemanticError(
                    f"FIFO field '{symbol.name}.{signal.value}' is write-only"
                )
            if signal is ir_storage.FifoSignal.FRONT:
                type_ = symbol.element_type
            elif signal is ir_storage.FifoSignal.COUNT:
                type_ = ir_types.UIntType(symbol.count_width)
            else:
                type_ = ir_types.BitType()
            return ir_expr.FifoRef(symbol.name, signal, type_)
        if isinstance(symbol, MemorySymbol):
            try:
                signal = ir_storage.MemorySignal(expression.field)
            except ValueError as error:
                raise SemanticError(
                    f"memory '{symbol.name}' has no field '{expression.field}'"
                ) from error
            if signal is not ir_storage.MemorySignal.READ_DATA:
                raise SemanticError(
                    f"memory field '{symbol.name}.{signal.value}' is write-only"
                )
            return ir_expr.MemoryRef(
                symbol.name, signal, symbol.element_type
            )
        if isinstance(symbol, RomSymbol):
            try:
                signal = ir_storage.RomSignal(expression.field)
            except ValueError as error:
                raise SemanticError(
                    f"ROM '{symbol.name}' has no field '{expression.field}'"
                ) from error
            if signal is not ir_storage.RomSignal.READ_DATA:
                raise SemanticError(
                    f"ROM field '{symbol.name}.{signal.value}' is write-only"
                )
            return ir_expr.RomRef(symbol.name, signal, symbol.element_type)
        if isinstance(symbol, ir_module.RequestResponseInterface):
            raise SemanticError(
                f"request/response channel '{symbol.name}.{expression.field}' "
                "is not a value; select payload, valid, ready, or transfer"
            )
        if isinstance(symbol, ir_module.Port):
            observations.record_definition(
                context,
                expression_origins.semantic_origin(expression.expression, context),
                context.services.tooling.definition_targets.get(id(symbol)),
                name=symbol.name,
                kind="port",
            )
            if symbol.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
                try:
                    signal = ir_interfaces.ReadyValidSignal(expression.field)
                except ValueError as error:
                    raise SemanticError(
                        f"ready/valid interface '{symbol.name}' has no field "
                        f"'{expression.field}'"
                    ) from error
                type_ = (
                    symbol.type
                    if signal is ir_interfaces.ReadyValidSignal.PAYLOAD
                    else ir_types.BitType()
                )
                return ir_expr.ReadyValidRef(symbol.name, signal, type_)
            if symbol.protocol is ir_interfaces.InterfaceProtocol.CREDIT:
                try:
                    signal = ir_interfaces.CreditSignal(expression.field)
                except ValueError as error:
                    raise SemanticError(
                        f"credit interface '{symbol.name}' has no field "
                        f"'{expression.field}'"
                    ) from error
                if (
                    signal is ir_interfaces.CreditSignal.CREDITS
                    and symbol.direction is ir_module.PortDirection.INPUT
                ):
                    raise SemanticError(
                        f"credit receiver interface '{symbol.name}' does not "
                        "own a sender credit count"
                    )
                if signal is ir_interfaces.CreditSignal.PAYLOAD:
                    type_ = symbol.type
                elif signal is ir_interfaces.CreditSignal.CREDITS:
                    if symbol.capacity is None:
                        raise SemanticError(
                            f"credit interface '{symbol.name}' has no capacity"
                        )
                    type_ = ir_types.UIntType(max(1, symbol.capacity.bit_length()))
                else:
                    type_ = ir_types.BitType()
                return ir_expr.CreditRef(symbol.name, signal, type_)
            if symbol.protocol is ir_interfaces.InterfaceProtocol.PACKET:
                try:
                    signal = ir_interfaces.PacketSignal(expression.field)
                except ValueError as error:
                    raise SemanticError(
                        f"packet interface '{symbol.name}' has no field "
                        f"'{expression.field}'"
                    ) from error
                type_ = (
                    symbol.type
                    if signal is ir_interfaces.PacketSignal.PAYLOAD
                    else ir_types.BitType()
                )
                return ir_expr.PacketRef(symbol.name, signal, type_)
            if symbol.protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT:
                try:
                    signal = ir_interfaces.VirtualChannelCreditSignal(expression.field)
                except ValueError as error:
                    raise SemanticError(
                        f"virtual-channel credit interface '{symbol.name}' "
                        f"has no field '{expression.field}'"
                    ) from error
                if symbol.virtual_channels is None or symbol.capacity is None:
                    raise SemanticError(
                        f"virtual-channel credit interface '{symbol.name}' "
                        "has incomplete bounds"
                    )
                if (
                    signal is ir_interfaces.VirtualChannelCreditSignal.CREDITS
                    and symbol.direction is ir_module.PortDirection.INPUT
                ):
                    raise SemanticError(
                        f"virtual-channel credit receiver '{symbol.name}' "
                        "does not own sender credit counts"
                    )
                vc_type = ir_types.UIntType(
                    max(1, (symbol.virtual_channels - 1).bit_length())
                )
                if signal is ir_interfaces.VirtualChannelCreditSignal.PAYLOAD:
                    type_ = symbol.type
                elif signal in {
                    ir_interfaces.VirtualChannelCreditSignal.VC,
                    ir_interfaces.VirtualChannelCreditSignal.RETURN_VC,
                }:
                    type_ = vc_type
                elif signal is ir_interfaces.VirtualChannelCreditSignal.CREDITS:
                    type_ = ir_types.VecType(
                        symbol.virtual_channels,
                        ir_types.UIntType(max(1, symbol.capacity.bit_length())),
                    )
                else:
                    type_ = ir_types.BitType()
                return ir_expr.VirtualChannelCreditRef(
                    symbol.name, signal, type_
                )
    return None

def _check_instance_field(
    expression: ast.FieldExpr,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    if (
        isinstance(expression.expression, ast.IndexExpr)
        and isinstance(expression.expression.expression, ast.NameExpr)
        and expression.expression.expression.name in context.scope.instance_arrays
    ):
        array = expression.expression.expression.name
        length = context.scope.instance_arrays[array]
        index_syntax = expression.expression.index
        index = _try_resolve_instance_array_index(
            index_syntax, context, array=array
        )
        if index is not None:
            if index < 0 or index >= length:
                raise SemanticError(
                    f"instance array '{array}' index {index} is out of range "
                    f"0..{length - 1}"
                )
            physical = f"{array}[{index}]"
            instance_type = context.scope.instance_outputs.get(
                (physical, expression.field)
            )
            if instance_type is None:
                raise SemanticError(
                    f"instance '{physical}' has no output port "
                    f"'{expression.field}'"
                )
            return ir_expr.InstanceOutputRef(
                physical,
                expression.field,
                instance_type,
                domain=context.scope.instance_output_domains.get(
                    (physical, expression.field)
                ),
            )

        physical_names = tuple(f"{array}[{item}]" for item in range(length))
        output_types = tuple(
            context.scope.instance_outputs.get((physical, expression.field))
            for physical in physical_names
        )
        if any(type_ is None for type_ in output_types):
            raise SemanticError(
                f"instance array '{array}' has no common output port "
                f"'{expression.field}'"
            )
        instance_type = output_types[0]
        assert instance_type is not None
        if any(type_ != instance_type for type_ in output_types[1:]):
            raise SemanticError(
                f"instance array '{array}' output '{expression.field}' "
                "does not have one exact type across physical children"
            )
        protocols = tuple(
            context.scope.instance_output_protocols.get(
                (physical, expression.field), ir_interfaces.InterfaceProtocol.WIRE
            )
            for physical in physical_names
        )
        if any(protocol is not ir_interfaces.InterfaceProtocol.WIRE for protocol in protocols):
            raise SemanticError(
                f"runtime instance selection supports only scalar wire outputs; "
                f"'{array}[...].{expression.field}' is a protocol endpoint"
            )
        if not context.scope.allow_runtime_instance_projection:
            raise SemanticError(
                "runtime instance-array output selection is supported only "
                "while driving a module wire output; use an explicit "
                "generate plus runtime index for an internal value"
            )
        if not ir_packing.is_bit_packable(instance_type):
            raise SemanticError(
                f"runtime instance selection requires a bit-packable non-enum "
                f"wire output, got {instance_type}"
            )
        assert not isinstance(index_syntax, int)
        typed_index = context.expressions.check(index_syntax, inputs, None, context)
        typed_index = _expand_immutable_locals(
            typed_index, inputs, work_budget=context.services
        )
        typed_index = semantic_callables._expand_analysis_calls(
            typed_index, context, purpose="runtime instance-array selector"
        )
        if isinstance(typed_index, ir_expr.Constant):
            # Compile-time folding can discover a constant after the
            # syntax-level resolver. Preserve the direct physical ref.
            if typed_index.value < 0 or typed_index.value >= length:
                raise SemanticError(
                    f"instance array '{array}' index {typed_index.value} is "
                    f"out of range 0..{length - 1}"
                )
            return ir_expr.InstanceOutputRef(
                f"{array}[{typed_index.value}]",
                expression.field,
                instance_type,
                domain=context.scope.instance_output_domains.get(
                    (f"{array}[{typed_index.value}]", expression.field)
                ),
            )
        if not isinstance(typed_index.type, (ir_types.UIntType, ir_types.BitsType)):
            raise SemanticError(
                "runtime instance-array selector must be an unsigned integral "
                f"expression; got {typed_index.type}"
            )
        value_range = expression_ranges.static_value_range(
            typed_index, context.scope.range_refinements
        )
        if value_range is None:
            raise SemanticError(
                "runtime instance-array selector has no statically provable "
                f"unsigned range; got {typed_index.type}, required 0..{length - 1}"
            )
        if value_range.minimum < 0 or value_range.maximum >= length:
            raise SemanticError(
                f"runtime instance-array selector range {value_range.minimum}.."
                f"{value_range.maximum} is not provably within array length "
                f"{length} (required 0..{length - 1})"
            )
        generated = ir_expr.Generate(
            "i",
            0,
            length,
            tuple(
                ir_expr.InstanceOutputRef(
                    physical,
                    expression.field,
                    instance_type,
                    domain=context.scope.instance_output_domains.get(
                        (physical, expression.field)
                    ),
                )
                for physical in physical_names
            ),
            ir_types.VecType(length, instance_type),
        )
        return ir_expr.RuntimeIndex(
            generated,
            typed_index,
            length,
            value_range,
            instance_type,
        )
    if isinstance(expression.expression, ast.NameExpr):
        if expression.expression.name in context.scope.instance_arrays:
            raise SemanticError(
                f"instance array '{expression.expression.name}' requires "
                "a compile-time index before selecting an output port"
            )
        instance_type = context.scope.instance_outputs.get(
            (expression.expression.name, expression.field)
        )
        if instance_type is not None:
            return ir_expr.InstanceOutputRef(
                expression.expression.name,
                expression.field,
                instance_type,
                domain=context.scope.instance_output_domains.get(
                    (expression.expression.name, expression.field)
                ),
            )
    return None

def _check_struct_field(
    expression: ast.FieldExpr,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    aggregate = context.expressions.check(expression.expression, inputs, None, context)
    if not isinstance(aggregate.type, ir_types.StructType):
        raise SemanticError(
            f"field access requires a struct, got {aggregate.type}"
        )
    field = aggregate.type.field(expression.field)
    if field is None:
        raise SemanticError(
            f"struct '{aggregate.type.name}' has no field '{expression.field}'"
        )
    return ir_expr.FieldAccess(aggregate, field.name, field.type)
