# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned current-cycle storage dependency validation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
from typing import TYPE_CHECKING
from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import packing as ir_packing
from zlang.ir import storage as ir_storage
from zlang.ir import types as ir_types
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.traversal import expression_children
from zlang.source import SourceOrigin

from .errors import SemanticError
from . import actions as semantic_actions
from . import expression_domains
from .module_validation import reject_dependency_cycles
from .storage_symbols import FifoSymbol, MemoryPortSymbol, MemorySymbol, RomSymbol

if TYPE_CHECKING:
    from . import context as semantic_context
    from . import module_pipeline


@dataclass(frozen=True)
class StorageDeclarationProduct:
    resource_symbols: dict[str, object]
    fifo_declarations: dict[str, ast.FifoDecl]
    memory_declarations: dict[str, ast.MemoryDecl]
    rom_declarations: dict[str, ast.RomDecl]
    scheduled_memory_names: frozenset[str]
    scheduled_masked_memory_names: frozenset[str]


class StorageDeclarationAnalyzer:
    """Own storage symbols, domain contracts, and scheduled ownership."""

    def analyze(
        self,
        preparation: module_pipeline.DeclarationPreparationProduct,
        hardware: module_pipeline.HardwareInterfaceProduct,
        symbols: dict[str, ir_module.Port],
        ports: list[ir_module.Port],
    ) -> StorageDeclarationProduct:
        module = preparation.module
        state_domains = hardware.state_domains
        type_resolver = preparation.type_resolver
        request_response_symbols = hardware.request_response_symbols
        csr_names = hardware.csr_names
        timing_names = hardware.timing_names
        resource_symbols: dict[str, FifoSymbol | MemorySymbol | RomSymbol] = {}
        fifo_declarations: dict[str, ast.FifoDecl] = {}
        memory_declarations: dict[str, ast.MemoryDecl] = {}
        rom_declarations: dict[str, ast.RomDecl] = {}
        for declaration in module.fifos:
            storage_domain = state_domains.resolve(
                "FIFO", declaration.name, declaration.domain
            )
            if declaration.name in (
                symbols.keys()
                | request_response_symbols.keys()
                | csr_names
                | resource_symbols.keys()
            ):
                raise SemanticError(
                    f"duplicate storage, interface, CSR, or port name "
                    f"'{declaration.name}'"
                )
            if declaration.name in timing_names:
                raise SemanticError(
                    f"FIFO name '{declaration.name}' conflicts with clock or reset"
                )
            depth = type_resolver._eval_storage_depth(
                declaration.depth, kind="FIFO", name=declaration.name
            )
            symbol = FifoSymbol(
                declaration.name,
                type_resolver.resolve(declaration.element_type),
                depth,
                storage_domain,
            )
            resource_symbols[symbol.name] = symbol
            fifo_declarations[symbol.name] = declaration

        for declaration in module.memories:
            if declaration.async_memory and declaration.domain is not None:
                raise SemanticError(
                    f"async memory '{declaration.name}' declares domains on its ports, "
                    "not on the memory"
                )
            if declaration.name in (
                symbols.keys()
                | request_response_symbols.keys()
                | csr_names
                | resource_symbols.keys()
            ):
                raise SemanticError(
                    f"duplicate storage, interface, CSR, or port name "
                    f"'{declaration.name}'"
                )
            if declaration.name in timing_names:
                raise SemanticError(
                    f"memory name '{declaration.name}' conflicts with clock or reset"
                )
            depth = type_resolver._eval_storage_depth(
                declaration.depth, kind="memory", name=declaration.name
            )
            if depth < 2 or depth & (depth - 1):
                raise SemanticError(
                    f"memory '{declaration.name}' depth must be a power of two "
                    "and at least 2"
                )
            if not 0 <= declaration.read_latency <= 16:
                raise SemanticError(
                    f"memory '{declaration.name}' read_latency must be in 0..16"
                )
            element_type = type_resolver.resolve(declaration.element_type)
            if not ir_packing.is_bit_packable(element_type):
                raise SemanticError(
                    f"memory '{declaration.name}' requires a recursively "
                    "bit-packable non-enum element type"
                )
            port_symbols: tuple[MemoryPortSymbol, ...] = ()
            if declaration.ports:
                if len(declaration.ports) > 8:
                    raise SemanticError(
                        f"memory '{declaration.name}' supports at most 8 logical ports"
                    )
                port_names = tuple(port.name for port in declaration.ports)
                if len(port_names) != len(set(port_names)):
                    raise SemanticError(
                        f"memory '{declaration.name}' has duplicate port names"
                    )
                if declaration.async_memory:
                    if declaration.read_latency < 1:
                        raise SemanticError(
                            f"async memory '{declaration.name}' requires read_latency >= 1; "
                            "latency-zero asynchronous BRAM reads are not supported"
                        )
                    kinds = tuple(port.kind for port in declaration.ports)
                    if (
                        len(kinds) != 2
                        or kinds.count(ast.MemoryPortKind.WRITE) != 1
                        or kinds.count(ast.MemoryPortKind.READ) != 1
                    ):
                        raise SemanticError(
                            f"async memory '{declaration.name}' requires exactly one "
                            "write_port and one read_port"
                        )
                    resolved_ports = []
                    for port in declaration.ports:
                        if port.domain is None:
                            raise SemanticError(
                                f"async memory port '{declaration.name}.{port.name}' "
                                "requires an explicit clock domain"
                            )
                        resolved_ports.append(MemoryPortSymbol(
                            port.name,
                            port.kind,
                            state_domains.resolve(
                                "memory port",
                                f"{declaration.name}.{port.name}",
                                port.domain,
                            ),
                        ))
                    if len({port.domain for port in resolved_ports}) != 2:
                        raise SemanticError(
                            f"async memory '{declaration.name}' ports must use "
                            "different clock domains"
                        )
                    port_symbols = tuple(resolved_ports)
                    storage_domain = next(
                        port.domain for port in port_symbols
                        if port.kind is ast.MemoryPortKind.WRITE
                    )
                else:
                    explicit_domains = tuple(
                        port.domain for port in declaration.ports
                        if port.domain is not None
                    )
                    storage_domain = state_domains.resolve(
                        "memory", declaration.name, declaration.domain,
                        explicit_domains,
                    )
                    port_symbols = tuple(
                        MemoryPortSymbol(
                            port.name,
                            port.kind,
                            state_domains.resolve(
                                "memory port",
                                f"{declaration.name}.{port.name}",
                                port.domain or storage_domain,
                            ),
                        )
                        for port in declaration.ports
                    )
                    if any(port.domain != storage_domain for port in port_symbols):
                        raise SemanticError(
                            f"ordinary memory '{declaration.name}' ports must share "
                            f"domain '{storage_domain}'"
                        )
                writable_names = tuple(
                    port.name for port in port_symbols
                    if port.kind in {
                        ast.MemoryPortKind.WRITE,
                        ast.MemoryPortKind.READ_WRITE,
                    }
                )
                if len(writable_names) > 1 and (
                    len(declaration.write_priority) != len(writable_names)
                    or set(declaration.write_priority) != set(writable_names)
                ):
                    raise SemanticError(
                        f"multi-writer memory '{declaration.name}' requires a complete "
                        "write_priority containing every writable port exactly once"
                    )
                if len(declaration.write_priority) != len(set(declaration.write_priority)):
                    raise SemanticError(
                        f"memory '{declaration.name}' write_priority contains duplicates"
                    )
            else:
                if declaration.async_memory:
                    raise SemanticError(
                        f"async memory '{declaration.name}' requires named ports"
                    )
                storage_domain = state_domains.resolve(
                    "memory", declaration.name, declaration.domain
                )
            symbol = MemorySymbol(
                declaration.name,
                element_type,
                depth,
                storage_domain,
                port_symbols,
                declaration.async_memory,
            )
            resource_symbols[symbol.name] = symbol
            memory_declarations[symbol.name] = declaration

        for declaration in module.roms:
            storage_domain = state_domains.resolve(
                "ROM", declaration.name, declaration.domain
            )
            if declaration.name in (
                symbols.keys()
                | request_response_symbols.keys()
                | csr_names
                | resource_symbols.keys()
            ):
                raise SemanticError(
                    f"duplicate storage, interface, CSR, or port name "
                    f"'{declaration.name}'"
                )
            if declaration.name in timing_names:
                raise SemanticError(
                    f"ROM name '{declaration.name}' conflicts with clock or reset"
                )
            depth = type_resolver._eval_storage_depth(
                declaration.depth, kind="ROM", name=declaration.name
            )
            if declaration.read_latency != 1:
                raise SemanticError(
                    f"ROM '{declaration.name}' requires exactly read_latency 1"
                )
            element_type = type_resolver.resolve(declaration.element_type)
            try:
                ir_packing.packed_width(element_type)
            except ir_packing.PackingError as error:
                raise SemanticError(
                    f"ROM '{declaration.name}' element type {element_type} is not "
                    f"recursively bit-packable and non-enum: {error}"
                ) from error
            symbol = RomSymbol(
                declaration.name, element_type, depth, storage_domain
            )
            resource_symbols[symbol.name] = symbol
            rom_declarations[symbol.name] = declaration

        if resource_symbols and (
            hardware.csr_blocks
            or hardware.request_responses
            or hardware.connections
            or any(port.protocol is ir_interfaces.InterfaceProtocol.CREDIT for port in ports)
        ):
            raise SemanticError(
                "storage resources cannot yet be mixed with CSR, credit, "
                "request/response, or connection backends"
            )
        scheduled_memory_names = {
            leaf.action.resource
            for rule in module.rules
            for leaf in semantic_actions.conditional_action_leaves(rule.actions)
            if isinstance(leaf.action, ast.ResourceAction)
            and leaf.action.resource in memory_declarations
        }
        scheduled_masked_memory_names = {
            leaf.action.resource
            for rule in module.rules
            for leaf in semantic_actions.conditional_action_leaves(rule.actions)
            if isinstance(leaf.action, ast.ResourceAction)
            and leaf.action.resource in memory_declarations
            and leaf.action.operation == "write"
            and len(leaf.action.operands) == 3
        }
        if len(scheduled_memory_names) > 1:
            raise SemanticError("a module currently supports at most one rule-owned memory")
        scheduled_zero_latency = tuple(
            name
            for name in sorted(scheduled_memory_names)
            if memory_declarations[name].read_latency == 0
        )
        if scheduled_zero_latency:
            name = scheduled_zero_latency[0]
            raise SemanticError(
                f"rule-owned memory '{name}' requires read_latency 1; "
                "read_latency 0 is supported only for globally controlled memories"
            )
        if scheduled_memory_names and len(memory_declarations) != 1:
            raise SemanticError(
                "global and rule-owned memory resources cannot be mixed in one module"
            )
        if memory_declarations and not scheduled_memory_names and (
            module.registers or module.next_assignments or module.rules
        ):
            raise SemanticError(
                "memory resources cannot yet be mixed with user registers or rules"
            )

        return StorageDeclarationProduct(
            resource_symbols,
            fifo_declarations,
            memory_declarations,
            rom_declarations,
            frozenset(scheduled_memory_names),
            frozenset(scheduled_masked_memory_names),
        )


@dataclass(frozen=True)
class StorageAnalysisProduct:
    fifos: tuple[ir_storage.Fifo, ...]
    memories: tuple[ir_storage.Memory, ...]
    roms: tuple[ir_storage.Rom, ...]
    scheduled_fifo_names: frozenset[str]
    transition_prefix: str


class StorageAnalyzer:
    """Own typed storage construction and storage-domain validation."""

    def analyze(
        self,
        context: semantic_context.AnalysisContext,
        preparation: module_pipeline.DeclarationPreparationProduct,
        state_storage: module_pipeline.StateStoragePreparationProduct,
        *,
        evaluator_schema: str,
    ) -> StorageAnalysisProduct:
        from . import callables as semantic_callables
        from . import expression_ranges
        from . import expression_support
        module = preparation.module
        declarations = state_storage.storage_declarations
        resource_symbols = declarations.resource_symbols
        fifo_declarations = declarations.fifo_declarations
        resource_controls = state_storage.resource_controls
        scheduled_memory_names = declarations.scheduled_memory_names
        value_symbols = state_storage.value_symbols
        module_context = state_storage.expression_context
        effective_source_unit = preparation.source_unit
        effective_source_digest = preparation.source_digest
        fifos: list[ir_storage.Fifo] = []
        scheduled_fifo_names = {
            leaf.action.resource
            for rule in module.rules
            for leaf in semantic_actions.conditional_action_leaves(rule.actions)
            if isinstance(leaf.action, ast.ResourceAction)
            and leaf.action.resource in fifo_declarations
        }
        identity_payload = f"{module.name}|{tuple((p.name, p.kind, p.default) for p in module.parameters)}"
        transition_prefix = hashlib.sha256(identity_payload.encode()).hexdigest()[:16]
        for name, declaration in fifo_declarations.items():
            symbol = resource_symbols[name]
            assert isinstance(symbol, FifoSymbol)
            controls = resource_controls[name]
            required = {"data", "push", "pop"}
            scheduled = name in scheduled_fifo_names
            if controls and scheduled:
                raise SemanticError(
                    f"FIFO '{name}' cannot mix global controls with rule-local actions"
                )
            missing = required - controls.keys()
            if controls and missing:
                raise SemanticError(
                    f"FIFO '{name}' has no '{sorted(missing)[0]}' control assignment"
                )
            if not controls and not scheduled:
                raise SemanticError(
                    f"FIFO '{name}' requires global controls or rule-local push/pop actions"
                )
            data = (
                module_context.expressions.check_typed_boundary(
                    controls["data"], value_symbols, symbol.element_type, module_context
                )
                if controls else None
            )
            push = (
                module_context.expressions.check(controls["push"], value_symbols, ir_types.BitType(), module_context)
                if controls else None
            )
            pop = (
                module_context.expressions.check(controls["pop"], value_symbols, ir_types.BitType(), module_context)
                if controls else None
            )
            if data is not None and data.type != symbol.element_type:
                raise SemanticError(
                    f"FIFO '{name}.data' has type {data.type}, expected "
                    f"{symbol.element_type}"
                )
            if push is not None and (push.type != ir_types.BitType() or pop is None or pop.type != ir_types.BitType()):
                raise SemanticError(f"FIFO '{name}' push and pop controls must be bit")
            fifos.append(
                ir_storage.Fifo(
                    name, symbol.element_type, symbol.depth, data, push, pop,
                    SourceOrigin(
                        declaration.origin,
                        f"FIFO {name}",
                        effective_source_unit,
                        effective_source_digest,
                    )
                    if getattr(declaration, "origin", None) is not None else None,
                    symbol.domain,
                )
            )

        memories: list[ir_storage.Memory] = []
        for name, declaration in declarations.memory_declarations.items():
            symbol = resource_symbols[name]
            assert isinstance(symbol, MemorySymbol)
            controls = resource_controls[name]
            initial_value = None
            if declaration.initializer is not None:
                initial_value = module_context.expressions.check_typed_boundary(
                    declaration.initializer,
                    value_symbols,
                    symbol.element_type,
                    module_context,
                )
                initial_value = semantic_callables._expand_analysis_calls(
                    initial_value,
                    module_context,
                    purpose=f"memory '{name}' initializer",
                )
                try:
                    constant_runtime_value(initial_value)
                except ConstantExpressionError as error:
                    raise SemanticError(
                        f"memory '{name}' init value must be a compile-time "
                        f"constant of exact type {symbol.element_type}: {error}"
                    ) from error
            if symbol.ports:
                if name in scheduled_memory_names:
                    raise SemanticError(
                        f"ported memory '{name}' does not accept rule-local memory actions"
                    )
                address_type = ir_types.UIntType(symbol.address_width)
                write_mask_width = (
                    ir_storage.memory_byte_mask_width(symbol.element_type.width)
                    if any(key.endswith(".mask") or key.endswith(".write_mask") for key in controls)
                    else None
                )
                typed_ports: list[ir_storage.MemoryPort] = []
                declaration_ports = {port.name: port for port in declaration.ports}
                for port_symbol in symbol.ports:
                    prefix = f"{port_symbol.name}."
                    port_controls = {
                        key[len(prefix):]: value
                        for key, value in controls.items()
                        if key.startswith(prefix)
                    }
                    readable = port_symbol.kind in {
                        ast.MemoryPortKind.READ,
                        ast.MemoryPortKind.READ_WRITE,
                    }
                    writable = port_symbol.kind in {
                        ast.MemoryPortKind.WRITE,
                        ast.MemoryPortKind.READ_WRITE,
                    }
                    required = {"address"}
                    if writable:
                        required |= (
                            {"enable", "data"}
                            if port_symbol.kind is ast.MemoryPortKind.WRITE
                            else {"write_enable", "write_data"}
                        )
                    missing = required - port_controls.keys()
                    if missing:
                        raise SemanticError(
                            f"memory port '{name}.{port_symbol.name}' has no "
                            f"'{sorted(missing)[0]}' control assignment"
                        )
                    address = module_context.expressions.check(
                        port_controls["address"], value_symbols,
                        address_type, module_context,
                    )
                    read_enable = None
                    if readable:
                        source = port_controls.get("read_enable")
                        read_enable = (
                            module_context.expressions.check(
                                source, value_symbols, ir_types.BitType(), module_context
                            )
                            if source is not None else ir_expr.Constant(1, ir_types.BitType())
                        )
                    write_enable = write_data = write_mask = None
                    if writable:
                        enable_name = (
                            "enable"
                            if port_symbol.kind is ast.MemoryPortKind.WRITE
                            else "write_enable"
                        )
                        data_name = (
                            "data"
                            if port_symbol.kind is ast.MemoryPortKind.WRITE
                            else "write_data"
                        )
                        mask_name = (
                            "mask"
                            if port_symbol.kind is ast.MemoryPortKind.WRITE
                            else "write_mask"
                        )
                        write_enable = module_context.expressions.check(
                            port_controls[enable_name], value_symbols,
                            ir_types.BitType(), module_context,
                        )
                        write_data = module_context.expressions.check_typed_boundary(
                            port_controls[data_name], value_symbols,
                            symbol.element_type, module_context,
                        )
                        if mask_name in port_controls:
                            lane_count = ir_storage.memory_byte_mask_width(
                                symbol.element_type.width
                            )
                            write_mask = module_context.expressions.check(
                                port_controls[mask_name], value_symbols,
                                ir_types.BitsType(lane_count), module_context,
                            )
                    expected_fields = [("address", address.type, address_type)]
                    if read_enable is not None:
                        expected_fields.append(
                            ("read_enable", read_enable.type, ir_types.BitType())
                        )
                    if write_enable is not None:
                        expected_fields.append(
                            ("write_enable", write_enable.type, ir_types.BitType())
                        )
                    if write_data is not None:
                        expected_fields.append(
                            ("write_data", write_data.type, symbol.element_type)
                        )
                    for label, actual, expected in expected_fields:
                        if actual != expected:
                            raise SemanticError(
                                f"memory port '{name}.{port_symbol.name}.{label}' "
                                f"has type {actual}, expected {expected}"
                            )
                    port_decl = declaration_ports[port_symbol.name]
                    port_origin = (
                        SourceOrigin(
                            port_decl.origin,
                            f"memory port {name}.{port_symbol.name}",
                            effective_source_unit,
                            effective_source_digest,
                        )
                        if port_decl.origin is not None else None
                    )
                    typed_ports.append(ir_storage.MemoryPort(
                        port_symbol.name,
                        f"state:{transition_prefix}:memory:{name}:port:{port_symbol.name}",
                        ir_storage.MemoryPortKind(port_symbol.kind.value),
                        port_symbol.domain,
                        address,
                        read_enable,
                        write_enable,
                        write_data,
                        write_mask,
                        port_origin,
                    ))
                origin = (
                    SourceOrigin(
                        declaration.origin, f"memory {name}", effective_source_unit,
                        effective_source_digest,
                    )
                    if declaration.origin is not None else None
                )
                memories.append(ir_storage.Memory(
                    name=name,
                    semantic_id=f"state:{transition_prefix}:memory:{name}",
                    element_type=symbol.element_type,
                    depth=symbol.depth,
                    read_latency=declaration.read_latency,
                    collision=ir_storage.MemoryCollision(declaration.collision.value),
                    read_address=None,
                    write_enable=None,
                    write_address=None,
                    write_data=None,
                    source_origin=origin,
                    write_mask_width=write_mask_width,
                    write_mask=None,
                    contents_reset=ir_storage.MemoryResetPolicy(
                        declaration.contents_reset.value
                    ),
                    read_data_reset=ir_storage.MemoryResetPolicy(
                        declaration.read_data_reset.value
                    ),
                    domain=symbol.domain,
                    ports=tuple(typed_ports),
                    async_memory=declaration.async_memory,
                    write_priority=declaration.write_priority,
                    initial_value=initial_value,
                ))
                continue
            required = {"read_address", "write_enable", "write_address", "write_data"}
            scheduled = name in scheduled_memory_names
            if controls and scheduled:
                raise SemanticError(
                    f"memory '{name}' cannot mix global controls with rule-local actions"
                )
            missing = required - controls.keys()
            if controls and missing:
                raise SemanticError(
                    f"memory '{name}' has no '{sorted(missing)[0]}' control assignment"
                )
            if not controls and not scheduled:
                raise SemanticError(
                    f"memory '{name}' requires global controls or rule-local read/write actions"
                )
            address_type = ir_types.UIntType(symbol.address_width)
            masked = (
                "write_mask" in controls
                or name in declarations.scheduled_masked_memory_names
            )
            write_mask_width = (
                ir_storage.memory_byte_mask_width(symbol.element_type.width)
                if masked else None
            )
            read_address = write_enable = write_address = write_data = write_mask = None
            if controls:
                read_address = module_context.expressions.check(
                    controls["read_address"], value_symbols, address_type, module_context
                )
                write_enable = module_context.expressions.check(
                    controls["write_enable"], value_symbols, ir_types.BitType(), module_context
                )
                write_address = module_context.expressions.check(
                    controls["write_address"], value_symbols, address_type, module_context
                )
                write_data = module_context.expressions.check_typed_boundary(
                    controls["write_data"], value_symbols, symbol.element_type,
                    module_context,
                )
                if masked:
                    assert write_mask_width is not None
                    write_mask = module_context.expressions.check(
                        controls["write_mask"], value_symbols,
                        ir_types.BitsType(write_mask_width), module_context,
                    )
                expected_types = (
                    ("read_address", read_address.type, address_type),
                    ("write_enable", write_enable.type, ir_types.BitType()),
                    ("write_address", write_address.type, address_type),
                    ("write_data", write_data.type, symbol.element_type),
                    *(
                        (("write_mask", write_mask.type, ir_types.BitsType(write_mask_width)),)
                        if write_mask is not None and write_mask_width is not None else ()
                    ),
                )
                for field, actual, expected_type in expected_types:
                    if actual != expected_type:
                        raise SemanticError(
                            f"memory '{name}.{field}' has type {actual}, expected "
                            f"{expected_type}"
                        )
            origin = (
                SourceOrigin(
                    declaration.origin, f"memory {name}", effective_source_unit,
                    effective_source_digest,
                )
                if declaration.origin is not None else None
            )
            memories.append(
                ir_storage.Memory(
                    name=name,
                    semantic_id=f"state:{transition_prefix}:memory:{name}",
                    element_type=symbol.element_type,
                    depth=symbol.depth,
                    read_latency=declaration.read_latency,
                    collision=ir_storage.MemoryCollision(declaration.collision.value),
                    read_address=read_address,
                    write_enable=write_enable,
                    write_address=write_address,
                    write_data=write_data,
                    source_origin=origin,
                    write_mask_width=write_mask_width,
                    write_mask=write_mask,
                    contents_reset=ir_storage.MemoryResetPolicy(
                        declaration.contents_reset.value
                    ),
                    read_data_reset=ir_storage.MemoryResetPolicy(
                        declaration.read_data_reset.value
                    ),
                    domain=symbol.domain,
                    ports=(),
                    async_memory=False,
                    write_priority=(),
                    initial_value=initial_value,
                )
            )

        roms: list[ir_storage.Rom] = []
        for name, declaration in declarations.rom_declarations.items():
            symbol = resource_symbols[name]
            assert isinstance(symbol, RomSymbol)
            controls = resource_controls[name]
            missing = {"read_address"} - controls.keys()
            if missing:
                raise SemanticError(f"ROM '{name}' has no 'read_address' assignment")
            address_type = ir_types.UIntType(symbol.address_width)
            read_address = module_context.expressions.check(
                controls["read_address"], value_symbols, address_type, module_context
            )
            if read_address.type != address_type:
                raise SemanticError(
                    f"ROM '{name}.read_address' has type {read_address.type}, "
                    f"expected exact {address_type}"
                )
            address_range = expression_ranges.static_value_range(read_address)
            if address_range is None or address_range.minimum < 0 or address_range.maximum >= symbol.depth:
                described = (
                    "unknown"
                    if address_range is None
                    else f"{address_range.minimum}..{address_range.maximum}"
                )
                raise SemanticError(
                    f"ROM '{name}.read_address' static range {described} is not "
                    f"within 0..{symbol.depth - 1}"
                )

            initializer_type = ir_types.VecType(symbol.depth, symbol.element_type)
            initializer = module_context.expressions.check(
                declaration.initializer, value_symbols, initializer_type, module_context
            )
            if initializer.type != initializer_type:
                raise SemanticError(
                    f"ROM '{name}' initializer has type {initializer.type}, expected "
                    f"exact {initializer_type}"
                )
            initializer_analysis = semantic_callables._expand_analysis_calls(
                initializer,
                module_context,
                purpose=f"ROM '{name}' initializer",
            )
            if not isinstance(
                initializer_analysis,
                (ir_expr.Generate, ir_expr.Map, ir_expr.FunctionalRegion),
            ):
                raise SemanticError(
                    f"ROM '{name}' initializer must specialize to a concrete "
                    f"{initializer_type} vector"
                )
            try:
                runtime_contents = constant_runtime_value(initializer_analysis)
            except ConstantExpressionError as error:
                raise SemanticError(
                    f"ROM '{name}' initializer must contain compile-time constants "
                    f"only: {error}"
                ) from error
            assert isinstance(runtime_contents, tuple)
            contents = (
                initializer_analysis.elements
                if isinstance(initializer_analysis, (ir_expr.Generate, ir_expr.Map))
                else tuple(
                    semantic_callables._rom_constant_expression(
                        symbol.element_type,
                        word,
                        initializer_analysis.template.origin,
                    )
                    for word in runtime_contents
                )
            )
            canonical_contents = semantic_callables._canonical_rom_runtime_values(
                initializer_type, runtime_contents
            )
            content_hash = hashlib.sha256(
                repr((str(initializer_type), canonical_contents)).encode("utf-8")
            ).hexdigest()
            identity_payload = (
                effective_source_unit or module.source_identity or module.name,
                module.name,
                tuple(sorted(preparation.parameter_values.items())),
                tuple(sorted(
                    (
                        name,
                        str(value.type),
                        hashlib.sha256(
                            repr((str(value.type), constant_runtime_value(value))).encode(
                                "utf-8"
                            )
                        ).hexdigest(),
                    )
                    for name, value in (
                        context.specialization.constant_bindings or {}
                    ).items()
                )),
                tuple(sorted(
                    (name, binding.callee_identity)
                    for name, binding in (
                        context.specialization.callable_bindings or {}
                    ).items()
                )),
                name,
                str(symbol.element_type),
                symbol.depth,
            )
            semantic_id = "rom:" + hashlib.sha256(
                repr(identity_payload).encode("utf-8")
            ).hexdigest()[:24]
            origin = (
                SourceOrigin(
                    declaration.origin,
                    f"ROM {name}",
                    effective_source_unit,
                    effective_source_digest,
                )
                if declaration.origin is not None else None
            )
            roms.append(ir_storage.Rom(
                name=name,
                semantic_id=semantic_id,
                element_type=symbol.element_type,
                depth=symbol.depth,
                address_type=address_type,
                read_latency=declaration.read_latency,
                contents=contents,
                read_address=read_address,
                initialization_identity=expression_semantic_identity(initializer),
                dependency_identity=(
                    module_context.environment.generic_dependency_identity
                ),
                evaluator_schema=evaluator_schema,
                content_hash=content_hash,
                source_origin=origin,
                domain=symbol.domain,
            ))

        def validate_resource_domain(
            kind: str,
            name: str,
            domain: str,
            expressions: Iterable[ir_expr.Expression | None],
        ) -> None:
            """Reject global storage controls that bypass explicit CDC checking."""

            for value in expressions:
                if value is None:
                    continue
                source_domains = {
                    item
                    for item in expression_domains.expression_domains(
                        expression_support._expand_immutable_locals(
                            value, value_symbols, work_budget=module_context.services
                        ),
                        {**state_storage.symbols, **resource_symbols},
                        state_storage.register_symbols,
                    )
                    if item is not None
                }
                foreign = source_domains - {domain}
                if foreign:
                    raise SemanticError(
                        f"clock-domain mismatch in {kind} '{name}': resource "
                        f"domain is '{domain}', control reads "
                        f"'{sorted(foreign)[0]}'",
                        code="ZL-DOMAIN-CROSSING",
                        primary=value.origin,
                        fixes=(
                            "insert an explicit supported clock-domain crossing",
                        ),
                    )

        for fifo in fifos:
            validate_resource_domain(
                "FIFO", fifo.name, fifo.domain,
                (fifo.data, fifo.push, fifo.pop),
            )
        for memory in memories:
            if memory.ports:
                for port in memory.ports:
                    validate_resource_domain(
                        "memory port",
                        f"{memory.name}.{port.name}",
                        port.domain,
                        (
                            port.address,
                            port.read_enable,
                            port.write_enable,
                            port.write_data,
                            port.write_mask,
                        ),
                    )
            else:
                validate_resource_domain(
                    "memory", memory.name, memory.domain,
                    (
                        memory.read_address,
                        memory.write_enable,
                        memory.write_address,
                        memory.write_data,
                        memory.write_mask,
                    ),
                )
        for rom in roms:
            validate_resource_domain(
                "ROM", rom.name, rom.domain, (rom.read_address,),
            )

        return StorageAnalysisProduct(
            tuple(fifos),
            tuple(memories),
            tuple(roms),
            frozenset(scheduled_fifo_names),
            transition_prefix,
        )


class MemoryDependencyValidator:
    """Reject current-cycle feedback through combinational memory reads."""

    def validate(
        self,
        memories: tuple[ir_storage.Memory, ...],
        locals_: tuple[ir_module.LocalValue, ...],
    ) -> None:
        """Reject current-cycle feedback through combinational memory reads.

        A latency-one memory terminates a combinational path.  A latency-zero
        memory does not: its read address always affects ``read_data``, while a
        ``write_first`` profile also observes the active write controls and the
        fully merged write word.  Model those typed dependencies before either
        backend can publish a combinational loop.  Read-first write-data feedback
        remains legal because it only changes the cell state at the next edge.
        """

        combinational = {
            memory.name: memory
            for memory in memories
            if memory.read_latency == 0
        }
        if not combinational:
            return
        local_values = {item.name: item.expression for item in locals_}

        def referenced_memories(
            expression: ir_expr.Expression,
            *,
            active_locals: frozenset[str] = frozenset(),
        ) -> set[str]:
            referenced: set[str] = set()
            boundaries = (
                ir_expr.RegisterRef,
                ir_expr.FifoRef,
                ir_expr.RomRef,
                ir_expr.Delay,
                ir_expr.Pipeline,
                ir_expr.Constant,
                ir_expr.ParameterRef,
                ir_expr.FunctionalCaptureRef,
                ir_expr.FunctionalValue,
                ir_expr.FunctionalTableLookup,
                ir_expr.InstanceOutputRef,
            )

            def visit(value: ir_expr.Expression, active: frozenset[str]) -> None:
                if isinstance(value, ir_expr.InputRef):
                    replacement = local_values.get(value.name)
                    if replacement is not None and value.name in active:
                        raise SemanticError(
                            f"cyclic immutable local '{value.name}' in memory "
                            "dependency analysis"
                        )
                    if replacement is not None:
                        visit(replacement, active | {value.name})
                    return
                if isinstance(value, ir_expr.MemoryRef):
                    if value.signal is ir_storage.MemorySignal.READ_DATA:
                        referenced.add(value.memory)
                    return
                if isinstance(value, boundaries):
                    return
                for child in expression_children(value):
                    visit(child, active)

            visit(expression, active_locals)
            return referenced & combinational.keys()

        graph: dict[str, set[str]] = {}
        for name, memory in combinational.items():
            controls = [memory.read_address]
            if memory.collision is ir_storage.MemoryCollision.WRITE_FIRST:
                controls.extend((
                    memory.write_enable,
                    memory.write_address,
                    memory.write_data,
                    memory.write_mask,
                ))
            graph[name] = set().union(*(
                referenced_memories(control)
                for control in controls
                if control is not None
            ))

        reject_dependency_cycles(
            graph,
            render_node=lambda name: f"{name}.read_data",
            description="memory",
        )
