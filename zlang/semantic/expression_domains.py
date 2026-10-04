# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Clock-domain provenance traversal for typed expressions."""

from __future__ import annotations

from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import traversal as ir_traversal

from .storage_symbols import MemorySymbol


def expression_domains(
    expression: ir_expr.Expression,
    ports: dict[str, object],
    registers: dict[str, ir_module.Register],
) -> set[str | None]:
    domains: set[str | None] = set()
    pending = [expression]
    visited: set[int] = set()
    while pending:
        value = pending.pop()
        if id(value) in visited:
            continue
        visited.add(id(value))
        if isinstance(value, ir_expr.InputRef):
            port = ports.get(value.name)
            domains.add(port.domain if port is not None else None)
        elif isinstance(value, ir_expr.RegisterRef):
            register = registers.get(value.name)
            domains.add(register.domain if register is not None else None)
        elif isinstance(
            value,
            (
                ir_expr.ReadyValidRef,
                ir_expr.CreditRef,
                ir_expr.PacketRef,
                ir_expr.VirtualChannelCreditRef,
            ),
        ):
            port = ports.get(value.interface)
            domains.add(port.domain if port is not None else None)
        elif isinstance(
            value,
            (
                ir_expr.ParameterRef,
                ir_expr.FunctionalCaptureRef,
                ir_expr.FunctionalValue,
                ir_expr.FunctionalTableLookup,
                ir_expr.RequestResponseRef,
                ir_expr.Constant,
            ),
        ):
            domains.add(None)
        elif isinstance(value, ir_expr.InstanceOutputRef):
            domains.add(value.domain)
        elif isinstance(value, ir_expr.FifoRef):
            domains.add(getattr(ports.get(value.fifo), "domain", None))
        elif isinstance(value, ir_expr.MemoryRef):
            resource = ports.get(value.memory)
            if isinstance(resource, MemorySymbol) and value.port is not None:
                port = next(
                    (item for item in resource.ports if item.name == value.port),
                    None,
                )
                domains.add(port.domain if port is not None else None)
            else:
                domains.add(getattr(resource, "domain", None))
        elif isinstance(value, ir_expr.RomRef):
            domains.add(getattr(ports.get(value.rom), "domain", None))
        elif isinstance(value, ir_expr.Pipeline) and value.domain is not None:
            domains.add(value.domain)

        if isinstance(value, ir_expr.FunctionalRegion):
            children = (
                *(item for table in value.tables for item in table.values),
                *(item for _, item in value.captures),
            )
            if not children:
                domains.add(None)
        elif isinstance(value, ir_expr.Dot):
            children = (value.left, value.right)
        elif isinstance(value, ir_expr.Reduce):
            children = (value.collection,)
        else:
            children = ir_traversal.expression_children(value)
            if not children and isinstance(
                value, (ir_expr.TupleConstruct, ir_expr.UnionConstruct)
            ):
                domains.add(None)
        pending.extend(reversed(children))
    return domains
