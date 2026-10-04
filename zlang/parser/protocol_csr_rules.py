# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bounded stateless Lark callbacks owned by ProtocolAndCsrRules."""

from __future__ import annotations

from lark import v_args

from zlang.ast import nodes as ast_nodes
from zlang.parser.errors import ParseError

from .rules_support import (
    _ParsedHierarchicalEndpoint,
    _tagged,
    _tagged_position,
)


class ProtocolAndCsrRules:
    """Stateless grammar callbacks for one bounded parser domain."""

    @v_args(meta=True)
    def port_decl(self, meta: object, items: list[object]) -> ast_nodes.PortDecl:
        names_item = next(item for item in items if _tagged(item, "port_names"))
        names = tuple(names_item[1])
        type_name = next(
            item for item in items
            if isinstance(item, (ast_nodes.TypeName, ast_nodes.VectorTypeName, ast_nodes.TupleTypeName, ast_nodes.InterfaceTypeName))
        )
        trailing = [
            item for item in items[2:]
            if item is not type_name and item is not None
        ]
        domain = next((str(item) for item in trailing if isinstance(item, str)), None)
        initializer = next((item for item in trailing if not isinstance(item, str)), None)
        return ast_nodes.PortDecl(
            ast_nodes.Direction(str(items[0])), names[0], type_name, domain,
            () if len(names) == 1 else names,
            initializer,
            self._span(meta),
            tuple(names_item[2]) if len(names_item) > 2 else (),
        )

    def wire_interface_type(self, items: list[object]) -> ast_nodes.InterfaceTypeName:
        return ast_nodes.InterfaceTypeName(ast_nodes.InterfaceKind.WIRE, items[0])

    def ready_valid_interface_type(self, items: list[object]) -> ast_nodes.InterfaceTypeName:
        return ast_nodes.InterfaceTypeName(ast_nodes.InterfaceKind.READY_VALID, items[0])

    def credit_interface_type(self, items: list[object]) -> ast_nodes.InterfaceTypeName:
        return ast_nodes.InterfaceTypeName(ast_nodes.InterfaceKind.CREDIT, items[0], int(str(items[1])))

    def packet_interface_type(self, items: list[object]) -> ast_nodes.InterfaceTypeName:
        return ast_nodes.InterfaceTypeName(ast_nodes.InterfaceKind.PACKET, items[0])

    def vc_credit_interface_type(self, items: list[object]) -> ast_nodes.InterfaceTypeName:
        return ast_nodes.InterfaceTypeName(
            ast_nodes.InterfaceKind.VC_CREDIT,
            items[0],
            int(str(items[2])),
            int(str(items[1])),
        )

    def interface_decl(self, items: list[object]) -> ast_nodes.RequestResponseDecl:
        name = str(items[0])
        request_type = items[1]
        response_type = items[2]
        max_outstanding = int(str(items[3]))
        ordering = ast_nodes.RequestResponseOrdering(str(items[4]))
        match_by = items[5] if len(items) == 6 else None
        return ast_nodes.RequestResponseDecl(
            name,
            request_type,
            response_type,
            max_outstanding,
            ordering,
            str(match_by) if match_by is not None else None,
        )

    request_response_interface_decl = interface_decl

    def connection_buffer(self, items: list[object]) -> tuple[str, object]:
        return ("buffer", int(str(items[0])))

    def connection_request_buffer(self, items: list[object]) -> tuple[str, object]:
        return ("request_buffer", int(str(items[0])))

    def connection_response_buffer(self, items: list[object]) -> tuple[str, object]:
        return ("response_buffer", int(str(items[0])))

    def connection_adapter(self, items: list[object]) -> tuple[str, object]:
        return ("adapter", ast_nodes.ConnectionAdapter(str(items[0])))

    def basic_crossing(self, items: list[object]) -> ast_nodes.Crossing:
        return ast_nodes.Crossing(ast_nodes.CrossingKind(str(items[0])))

    def async_fifo_crossing(self, items: list[object]) -> ast_nodes.Crossing:
        return ast_nodes.Crossing(ast_nodes.CrossingKind.ASYNC_FIFO, items[0])

    def connection_crossing(self, items: list[object]) -> tuple[str, object]:
        return ("crossing", items[0])

    def connection_transform(self, items: list[object]) -> tuple[str, object]:
        return ("transform", items[0])

    def connect_decl(self, items: list[object]) -> ast_nodes.ConnectionDecl:
        options = dict(item for item in items[2:] if item is not None)
        source = items[0]
        destination = items[1]
        assert isinstance(source, _ParsedHierarchicalEndpoint)
        assert isinstance(destination, _ParsedHierarchicalEndpoint)
        return ast_nodes.ConnectionDecl(
            source.text,
            destination.text,
            int(options.get("buffer", 0)),
            int(options.get("request_buffer", 0)),
            int(options.get("response_buffer", 0)),
            options.get("adapter"),
            options.get("crossing"),
            options.get("transform"),
            source.name_origins,
            destination.name_origins,
        )

    bare_connect_decl = connect_decl

    @v_args(meta=True)
    def connection_chain_decl(
        self, meta: object, items: list[object]
    ) -> ast_nodes.ConnectionChainDecl:
        endpoints = tuple(items)
        assert all(
            isinstance(item, _ParsedHierarchicalEndpoint) for item in endpoints
        )
        return ast_nodes.ConnectionChainDecl(
            tuple(item.text for item in endpoints),
            origin=self._span(meta),
            endpoint_name_origins=tuple(item.name_origins for item in endpoints),
        )

    def hierarchical_endpoint(
        self, items: list[object]
    ) -> _ParsedHierarchicalEndpoint:
        result = str(items[0])
        name_origins = [self._token_span(items[0])]
        for item in items[1:]:
            if item is None:
                continue
            if _tagged(item, "endpoint_index"):
                result += f"[{item[1]}]"
            else:
                result += "." + str(item)
                name_origins.append(self._token_span(item))
        return _ParsedHierarchicalEndpoint(result, tuple(name_origins))

    def endpoint_index(self, items: list[object]) -> tuple[str, object]:
        token = str(items[0])
        try:
            value: object = self._parse_number(token)
        except (ParseError, ValueError):
            value = token
        return ("endpoint_index", value)

    def arbiter_sources(self, items: list[object]) -> tuple[str, ...]:
        return tuple(str(item) for item in items)

    def arbiter_decl(self, items: list[object]) -> ast_nodes.ArbiterDecl:
        return ast_nodes.ArbiterDecl(
            items[0],
            str(items[1]),
            ast_nodes.ArbitrationPolicy(str(items[2])),
            ast_nodes.GrantScope(str(items[3])),
        )

    def contract_decl(self, items: list[object]) -> ast_nodes.ContractDecl:
        return ast_nodes.ContractDecl(
            ast_nodes.ContractKind(str(items[0])),
            str(items[1]),
            str(items[2]),
            str(items[3]),
            items[4],
        )

    @v_args(meta=True)
    def verification_goal_decl(
        self, meta: object, items: list[object]
    ) -> ast_nodes.VerificationGoalDecl:
        return ast_nodes.VerificationGoalDecl(
            ast_nodes.VerificationGoalKind(str(items[0])),
            str(items[1]),
            items[3],
            str(items[2]) if items[2] is not None else None,
            self._span(meta),
        )

    @v_args(meta=True)
    def verification_requirement_decl(
        self, meta: object, items: list[object]
    ) -> ast_nodes.VerificationRequirementDecl:
        return ast_nodes.VerificationRequirementDecl(
            str(items[0]), items[1], self._span(meta)
        )

    @v_args(meta=True)
    def verification_scoped_goal_decl(
        self, meta: object, items: list[object]
    ) -> ast_nodes.VerificationGoalDecl:
        return ast_nodes.VerificationGoalDecl(
            ast_nodes.VerificationGoalKind(str(items[0])),
            str(items[1]),
            items[2],
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def verification_scope_decl(
        self, meta: object, items: list[object]
    ) -> ast_nodes.VerificationScopeDecl:
        return ast_nodes.VerificationScopeDecl(
            str(items[0]),
            tuple(
                item for item in items[2:]
                if isinstance(item, ast_nodes.VerificationRequirementDecl)
            ),
            tuple(
                item for item in items[2:]
                if isinstance(item, ast_nodes.VerificationGoalDecl)
            ),
            str(items[1]) if items[1] is not None else None,
            self._span(meta),
        )

    def csr_position(self, items: list[object]) -> tuple[str, int, int]:
        numbers = [item for item in items if item is not None]
        msb = self._parse_number(numbers[0])
        lsb = self._parse_number(numbers[1]) if len(numbers) == 2 else msb
        return ("position", msb, lsb)

    @v_args(meta=True)
    def csr_field(self, meta: object, items: list[object]) -> ast_nodes.CsrFieldDecl:
        name = str(items[0])
        type_name = items[1]
        position = next(
            (item for item in items[2:] if _tagged_position(item)), None
        )
        access_index = next(
            index
            for index, item in enumerate(items[2:], start=2)
            if str(item) in {access.value for access in ast_nodes.CsrAccess}
        )
        trailing = [
            item for item in items[access_index + 1 :] if item is not None
        ]
        binding = next(
            (item for item in trailing if isinstance(item, ast_nodes.CsrBinding)), None
        )
        reset_item = next(
            (item for item in trailing if not isinstance(item, ast_nodes.CsrBinding)), None
        )
        reset = self._parse_number(reset_item) if reset_item is not None else None
        msb = position[1] if position is not None else None
        lsb = position[2] if position is not None else None
        return ast_nodes.CsrFieldDecl(
            name,
            type_name,
            ast_nodes.CsrAccess(str(items[access_index])),
            msb,
            lsb,
            reset,
            binding,
            self._span(meta),
        )

    def signal_ref(self, items: list[object]) -> str:
        return ".".join(str(item) for item in items)

    def csr_status_binding(self, items: list[object]) -> ast_nodes.CsrBinding:
        return ast_nodes.CsrBinding(ast_nodes.CsrBindingKind.STATUS, str(items[0]))

    def csr_sticky_binding(self, items: list[object]) -> ast_nodes.CsrBinding:
        values = [item for item in items if item is not None]
        priority = (
            ast_nodes.CsrPriority(str(values[1]))
            if len(values) == 2
            else ast_nodes.CsrPriority.HARDWARE
        )
        return ast_nodes.CsrBinding(ast_nodes.CsrBindingKind.STICKY, str(values[0]), priority)

    def csr_command_binding(self, items: list[object]) -> ast_nodes.CsrBinding:
        return ast_nodes.CsrBinding(ast_nodes.CsrBindingKind.COMMAND, str(items[0]))

    @v_args(meta=True)
    def csr_event(self, meta: object, items: list[object]) -> ast_nodes.CsrEventDecl:
        position = next(
            (item for item in items[2:] if _tagged_position(item)), None
        )
        kind_index = next(
            index for index, item in enumerate(items[2:], start=2)
            if str(item) in {kind.value for kind in ast_nodes.CsrEventKind}
        )
        return ast_nodes.CsrEventDecl(
            str(items[0]),
            items[1],
            ast_nodes.CsrEventKind(str(items[kind_index])),
            str(items[kind_index + 1]),
            position[1] if position is not None else None,
            position[2] if position is not None else None,
            self._span(meta),
        )

    @v_args(meta=True)
    def csr_register(self, meta: object, items: list[object]) -> ast_nodes.CsrRegisterDecl:
        return ast_nodes.CsrRegisterDecl(
            str(items[0]),
            self._parse_number(items[1]),
            tuple(item for item in items[2:] if isinstance(item, ast_nodes.CsrFieldDecl)),
            tuple(item for item in items[2:] if isinstance(item, ast_nodes.CsrEventDecl)),
            self._span(meta),
        )

    @v_args(meta=True)
    def csr_group_decl(self, meta: object, items: list[object]) -> ast_nodes.CsrGroupDecl:
        return ast_nodes.CsrGroupDecl(
            str(items[0]),
            tuple(item for item in items[1:] if isinstance(item, ast_nodes.CsrRegisterDecl)),
            tuple(
                item for item in items[1:]
                if isinstance(item, ast_nodes.CsrSplitRegisterDecl)
            ),
            self._span(meta),
        )

    @v_args(meta=True)
    def csr_group_use(self, meta: object, items: list[object]) -> ast_nodes.CsrGroupUseDecl:
        return ast_nodes.CsrGroupUseDecl(
            str(items[0]), str(items[1]), items[2], items[3], items[4],
            self._span(meta),
        )

    @v_args(meta=True)
    def csr_split_register(
        self, meta: object, items: list[object]
    ) -> ast_nodes.CsrSplitRegisterDecl:
        trailing = [item for item in items[6:] if item is not None]
        reset = self._parse_number(trailing[0]) if len(trailing) == 2 else 0
        order = trailing[-1]
        return ast_nodes.CsrSplitRegisterDecl(
            str(items[0]), self._parse_number(items[1]), int(str(items[2])),
            str(items[3]), items[4], ast_nodes.CsrAccess(str(items[5])), reset,
            ast_nodes.CsrSplitOrder(str(order)), self._span(meta),
        )

    @v_args(meta=True)
    def csr_decl(self, meta: object, items: list[object]) -> ast_nodes.CsrBlockDecl:
        trailing = items[2:]
        domain = next(
            (
                str(item)
                for item in trailing
                if item is not None and not isinstance(
                    item, (ast_nodes.CsrRegisterDecl, ast_nodes.CsrGroupUseDecl, ast_nodes.CsrSplitRegisterDecl)
                )
            ),
            None,
        )
        return ast_nodes.CsrBlockDecl(
            str(items[0]),
            items[1],
            tuple(
                item for item in trailing if isinstance(item, ast_nodes.CsrRegisterDecl)
            ),
            tuple(
                item for item in trailing if isinstance(item, ast_nodes.CsrGroupUseDecl)
            ),
            tuple(
                item for item in trailing if isinstance(item, ast_nodes.CsrSplitRegisterDecl)
            ),
            domain,
            self._span(meta),
        )


__all__ = ["ProtocolAndCsrRules"]
