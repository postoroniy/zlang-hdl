# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bounded stateless Lark callbacks owned by AggregateCallableModuleRules."""

from __future__ import annotations

import re

from lark import v_args

from zlang.ast import nodes as ast_nodes
from zlang.parser.errors import ParseError
from zlang.source import SourceSpan

from .rules_support import _tagged


class AggregateCallableModuleRules:
    """Stateless grammar callbacks for one bounded parser domain."""

    @v_args(meta=True)
    def type_condition_expr(self, meta: object, items: list[object]) -> ast_nodes.BinaryExpr:
        match = re.fullmatch(
            r"([A-Z][A-Za-z0-9_]*)[ \t]*(==|!=)[ \t]*"
            r"([A-Z][A-Za-z0-9_]*(?:<[^{}\n]+>)?)",
            str(items[0]),
        )
        if match is None:
            raise ValueError("malformed nominal type condition")
        left = ast_nodes.TypeValueExpr(ast_nodes.TypeName(match.group(1)), origin=self._span(meta))
        right = ast_nodes.TypeValueExpr(ast_nodes.TypeName(match.group(3)), origin=self._span(meta))
        return ast_nodes.BinaryExpr(
            ast_nodes.BinaryOperator(match.group(2)), left, right, origin=self._span(meta)
        )

    def explicit_struct_field_value(self, items: list[object]) -> ast_nodes.StructFieldValue:
        return ast_nodes.StructFieldValue(str(items[0]), items[1])

    def shorthand_struct_field_value(self, items: list[object]) -> ast_nodes.StructFieldValue:
        return ast_nodes.StructFieldValue(str(items[0]), None)

    @v_args(meta=True)
    def struct_construct_expr(self, meta: object, items: list[object]) -> ast_nodes.StructConstructExpr:
        return ast_nodes.StructConstructExpr(
            str(items[0]),
            tuple(items[1:]),
            origin=self._span(meta),
            name_origin=self._token_span(items[0]),
        )

    @v_args(meta=True)
    def qualified_struct_construct_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.StructConstructExpr:
        qualified_name = str(items[0])
        return ast_nodes.StructConstructExpr(
            qualified_name,
            tuple(items[1:]),
            origin=self._span(meta),
            name_origin=self._token_span(items[0]),
        )

    @v_args(meta=True)
    def struct_update_expr(self, meta: object, items: list[object]) -> ast_nodes.StructUpdateExpr:
        return ast_nodes.StructUpdateExpr(items[0], tuple(items[1:]), origin=self._span(meta))

    @v_args(meta=True)
    def vector_literal_expr(self, meta: object, items: list[object]) -> ast_nodes.VectorLiteralExpr:
        elements = items[0] if items and isinstance(items[0], tuple) else tuple(items)
        return ast_nodes.VectorLiteralExpr(tuple(elements), origin=self._span(meta))

    @staticmethod
    def _decode_byte_literal(token: object) -> tuple[int, ...]:
        """Decode one already lexically validated ASCII/byte literal."""

        text = str(token)
        body = text[1:-1]
        values: list[int] = []
        index = 0
        escapes = {
            "0": 0,
            "n": 0x0A,
            "r": 0x0D,
            "t": 0x09,
            "\\": 0x5C,
            "'": 0x27,
            '"': 0x22,
        }
        while index < len(body):
            character = body[index]
            if character != "\\":
                values.append(ord(character))
                index += 1
                continue
            escape = body[index + 1]
            if escape == "x":
                values.append(int(body[index + 2 : index + 4], 16))
                index += 4
                continue
            values.append(escapes[escape])
            index += 2
        return tuple(values)

    @v_args(meta=True)
    def char_literal_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.CharLiteralExpr:
        values = self._decode_byte_literal(items[0])
        if len(values) != 1:  # Defensive: the token grammar enforces this.
            raise ParseError("character literal must contain exactly one byte")
        return ast_nodes.CharLiteralExpr(values[0], origin=self._span(meta))

    @v_args(meta=True)
    def string_literal_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.StringLiteralExpr:
        return ast_nodes.StringLiteralExpr(
            self._decode_byte_literal(items[0]), origin=self._span(meta)
        )

    def destructure_field_list(self, items: list[object]) -> tuple[str, ...]:
        return tuple(str(item) for item in items)

    @v_args(meta=True)
    def struct_destructure_decl(
        self,
        meta: object,
        items: list[object],
    ) -> ast_nodes.StructDestructureDecl:
        return ast_nodes.StructDestructureDecl(
            items[0], tuple(items[1]), items[2], origin=self._span(meta)
        )

    @v_args(meta=True)
    def tuple_destructure_decl(
        self, meta: object, items: list[object]
    ) -> ast_nodes.TupleDestructureDecl:
        names = tuple(items[1])
        if not 2 <= len(names) <= 8:
            raise ParseError("tuple destructuring requires between 2 and 8 names")
        if "_" in names:
            raise ParseError(
                "tuple wildcard '_' is not supported; bind every component "
                "to a fresh name"
            )
        invalid = next(
            (
                name
                for name in names
                if not self._ordinary_binding_name_is_valid(name)
            ),
            None,
        )
        if invalid is not None:
            raise ParseError(
                f"tuple binding '{invalid}' is not a legal immutable binding name"
            )
        return ast_nodes.TupleDestructureDecl(names, items[2], origin=self._span(meta))

    def tuple_destructure_names(self, items: list[object]) -> tuple[str, ...]:
        return tuple(str(item) for item in items)

    @v_args(meta=True)
    def tuple_destructure_name_rhs(
        self, meta: object, items: list[object]
    ) -> ast_nodes.NameExpr:
        return ast_nodes.NameExpr(str(items[0]), origin=self._span(meta))

    @v_args(meta=True)
    def type_alias(self, meta: object, items: list[object]) -> ast_nodes.TypeAlias:
        return ast_nodes.TypeAlias(
            str(items[0]),
            items[1],
            origin=self._span(meta),
            name_origin=self._token_span(items[0]),
        )

    @v_args(meta=True)
    def enum_member(self, _meta: object, items: list[object]) -> tuple[str, int | None, SourceSpan | None]:
        return (
            str(items[0]),
            self._parse_number(items[1])
            if len(items) > 1 and items[1] is not None
            else None,
            self._token_span(items[0]),
        )

    @v_args(meta=True)
    def enum_decl(self, meta: object, items: list[object]) -> ast_nodes.EnumDecl:
        backing_type = next(
            (item for item in items[1:] if isinstance(item, (ast_nodes.TypeName, ast_nodes.VectorTypeName, ast_nodes.TupleTypeName))),
            None,
        )
        members = tuple(
            item for item in items[1:]
            if isinstance(item, tuple)
            and len(item) >= 2
            and isinstance(item[0], str)
            and (item[1] is None or isinstance(item[1], int))
        )
        return ast_nodes.EnumDecl(
            str(items[0]), tuple(item[0] for item in members),
            origin=self._span(meta),
            backing_type=backing_type,
            encodings=tuple(item[1] for item in members),
            name_origin=self._token_span(items[0]),
            member_origins=tuple(item[2] for item in members),
        )

    def tagged_union_field(self, items: list[object]) -> ast_nodes.TaggedUnionFieldDecl:
        return ast_nodes.TaggedUnionFieldDecl(str(items[0]), items[1])

    def tagged_union_variant(self, items: list[object]) -> ast_nodes.TaggedUnionVariantDecl:
        return ast_nodes.TaggedUnionVariantDecl(
            str(items[0]),
            tuple(item for item in items[1:] if isinstance(item, ast_nodes.TaggedUnionFieldDecl)),
        )

    @v_args(meta=True)
    def tagged_union_decl(
        self, meta: object, items: list[object]
    ) -> ast_nodes.TaggedUnionDecl:
        return ast_nodes.TaggedUnionDecl(
            str(items[0]),
            tuple(item for item in items[1:] if isinstance(item, ast_nodes.TaggedUnionVariantDecl)),
            origin=self._span(meta),
        )

    def struct_field(self, items: list[object]) -> ast_nodes.StructFieldDecl:
        return ast_nodes.StructFieldDecl(str(items[0]), items[1])

    @v_args(meta=True)
    def struct_decl(self, meta: object, items: list[object]) -> ast_nodes.StructDecl:
        parameters = next(
            (item for item in items[1:] if isinstance(item, tuple) and all(isinstance(p, ast_nodes.ModuleParameter) for p in item)),
            (),
        )
        fields = tuple(item for item in items[1:] if isinstance(item, ast_nodes.StructFieldDecl))
        return ast_nodes.StructDecl(
            str(items[0]), fields, parameters,
            origin=self._span(meta),
            name_origin=self._token_span(items[0]),
        )

    def protocol_endpoint_ref(self, items: list[object]) -> tuple[str, tuple[ast_nodes.SpecializationArgument, ...], str, str | None]:
        arguments = next(
            (item for item in items[1:] if isinstance(item, tuple) and all(isinstance(arg, ast_nodes.SpecializationArgument) for arg in item)),
            (),
        )
        names = [str(item) for item in items[1:] if isinstance(item, str)]
        role = names[0]
        domain = names[1] if len(names) > 1 else None
        return (str(items[0]), arguments, role, domain)

    def aggregate_interface_decl(self, items: list[object]) -> ast_nodes.AggregateInterfaceDecl:
        protocol, arguments, role, domain = items[1]
        return ast_nodes.AggregateInterfaceDecl(str(items[0]), protocol, arguments, role, domain)

    @v_args(meta=True)
    def parameter(self, _meta: object, items: list[object]) -> ast_nodes.Parameter:
        return ast_nodes.Parameter(
            str(items[0]),
            items[1],
            name_origin=self._token_span(items[0]),
        )

    def parameter_list(self, items: list[object]) -> tuple[ast_nodes.Parameter, ...]:
        return tuple(items)

    @v_args(meta=True)
    def function_decl(self, meta: object, items: list[object]) -> ast_nodes.FunctionDecl:
        return self._callable_decl(ast_nodes.FunctionDecl, meta, items)

    @v_args(meta=True)
    def operator_decl(self, meta: object, items: list[object]) -> ast_nodes.OperatorDecl:
        symbol = str(items[0])
        return self._callable_decl(ast_nodes.OperatorDecl, meta, [symbol, *items[1:]])

    @v_args(meta=True)
    def callable_binding(self, meta: object, items: list[object]) -> ast_nodes.Assignment:
        target = str(items[0])
        return ast_nodes.Assignment(
            target,
            items[1],
            origin=self._span(meta),
            name_origin=self._token_span(items[0]),
        )

    def callable_body(
        self, items: list[object]
    ) -> tuple[tuple[ast_nodes.Assignment | ast_nodes.TupleDestructureDecl, ...], object]:
        binding_types = (ast_nodes.Assignment, ast_nodes.TupleDestructureDecl)
        if not items or isinstance(items[-1], binding_types):
            raise ValueError("callable body requires one final result expression")
        if any(not isinstance(item, binding_types) for item in items[:-1]):
            raise ValueError(
                "only immutable inferred bindings may precede a callable result"
            )
        return tuple(items[:-1]), items[-1]

    def _callable_decl(self, cls: type, meta: object, items: list[object]) -> object:
        name = str(items[0]) if cls is ast_nodes.FunctionDecl else items[0]
        generic_parameters = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and all(isinstance(p, ast_nodes.ModuleParameter) for p in item)
            ),
            (),
        )
        parameters = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and all(isinstance(p, ast_nodes.Parameter) for p in item)
            ),
            (),
        )
        bindings, body = items[-1]
        return_type = next(
            (
                item for item in items[1:-1]
                if isinstance(item, (ast_nodes.TypeName, ast_nodes.VectorTypeName, ast_nodes.TupleTypeName, ast_nodes.InterfaceTypeName))
            ),
            None,
        )
        return cls(
            name,
            parameters,
            return_type,
            body,
            generic_parameters,
            bindings,
            origin=self._span(meta),
            name_origin=self._token_span(items[0]),
        )

    def pattern_function_call(self, items: list[object]) -> ast_nodes.CallExpr:
        return ast_nodes.CallExpr(str(items[0]), ())

    def equiv_decl(self, items: list[object]) -> ast_nodes.EquivDecl:
        guard = items[3] if len(items) == 4 else None
        return ast_nodes.EquivDecl(str(items[0]), items[1], items[2], guard)

    def guard_expr(self, items: list[object]) -> ast_nodes.EquivGuard:
        return ast_nodes.EquivGuard(tuple(str(item) for item in items))

    def pattern_constant_argument(self, items: list[object]) -> str:
        return "".join(str(item) for item in items)

    @v_args(meta=True)
    def pattern_constant_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.PatternConstantExpr:
        return ast_nodes.PatternConstantExpr(
            ast_nodes.PatternConstantKind(str(items[0])),
            str(items[1]),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def module_interface_ref(
        self, meta: object, items: list[object]
    ) -> ast_nodes.ModuleInterfaceRef:
        arguments = next(
            (
                item for item in items[1:]
                if isinstance(item, tuple)
                and all(isinstance(arg, ast_nodes.SpecializationArgument) for arg in item)
            ),
            (),
        )
        return ast_nodes.ModuleInterfaceRef(str(items[0]), arguments, self._span(meta))

    @v_args(meta=True)
    def module_interface_decl(
        self, meta: object, items: list[object]
    ) -> ast_nodes.ModuleInterfaceDecl:
        name = str(items[0])
        body = items[1:]
        parameters = next(
            (
                item for item in body
                if isinstance(item, tuple)
                and all(isinstance(p, ast_nodes.ModuleParameter) for p in item)
            ),
            (),
        )
        timings = tuple(item for item in body if isinstance(item, ast_nodes.ModuleTimingDecl))
        if len(timings) > 1:
            raise ParseError(
                f"module interface '{name}' accepts at most one timing block"
            )
        return ast_nodes.ModuleInterfaceDecl(
            name=name,
            parameters=parameters,
            ports=tuple(item for item in body if isinstance(item, ast_nodes.PortDecl)),
            clocks=tuple(item[1] for item in body if _tagged(item, "clock")),
            resets=tuple(item[1] for item in body if _tagged(item, "reset")),
            reset_domains=tuple(
                (item[1], item[2]) for item in body if _tagged(item, "reset")
            ),
            clock_physical=tuple(
                item[2] for item in body if _tagged(item, "clock")
            ),
            reset_physical=tuple(
                item[3] for item in body if _tagged(item, "reset")
            ),
            request_responses=tuple(
                item for item in body if isinstance(item, ast_nodes.RequestResponseDecl)
            ),
            aggregate_interfaces=tuple(
                item for item in body if isinstance(item, ast_nodes.AggregateInterfaceDecl)
            ),
            timing=timings[0] if timings else None,
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def module(self, _meta: object, items: list[object]) -> ast_nodes.Module:
        name = str(items[0])
        body: list[object] = []
        for item in items[1:]:
            if isinstance(item, ast_nodes.RulePriorityChain):
                body.extend(
                    ast_nodes.RulePriority(higher, lower)
                    for higher, lower in zip(item.names, item.names[1:])
                )
            else:
                body.append(item)
        parameters = next((item for item in body if isinstance(item, tuple) and all(isinstance(p, ast_nodes.ModuleParameter) for p in item)), ())
        timings = tuple(item for item in body if isinstance(item, ast_nodes.ModuleTimingDecl))
        interface_ref = next(
            (item for item in body if isinstance(item, ast_nodes.ModuleInterfaceRef)), None
        )
        parameter_constraint = next(
            (item[1] for item in body if _tagged(item, "module_where")), None
        )

        if len(timings) > 1:
            raise ParseError(f"module '{name}' accepts at most one timing block")
        return ast_nodes.Module(
            name=name,
            ports=tuple(item for item in body if isinstance(item, ast_nodes.PortDecl)),
            assignments=tuple(
                item for item in body if isinstance(item, ast_nodes.Assignment)
            ),
            clocks=tuple(item[1] for item in body if _tagged(item, "clock")),
            resets=tuple(item[1] for item in body if _tagged(item, "reset")),
            reset_domains=tuple(
                (item[1], item[2])
                for item in body
                if _tagged(item, "reset")
            ),
            clock_physical=tuple(
                item[2] for item in body if _tagged(item, "clock")
            ),
            reset_physical=tuple(
                item[3] for item in body if _tagged(item, "reset")
            ),
            registers=tuple(
                item for item in body if isinstance(item, ast_nodes.RegisterDecl)
            ),
            next_assignments=tuple(
                item for item in body if isinstance(item, ast_nodes.NextAssignment)
            ),
            request_responses=tuple(
                item for item in body if isinstance(item, ast_nodes.RequestResponseDecl)
            ),
            connections=tuple(
                item for item in body if isinstance(item, ast_nodes.ConnectionDecl)
            ),
            connection_chains=tuple(
                item for item in body if isinstance(item, ast_nodes.ConnectionChainDecl)
            ),
            csr_blocks=tuple(
                item for item in body if isinstance(item, ast_nodes.CsrBlockDecl)
            ),
            csr_groups=tuple(
                item for item in body if isinstance(item, ast_nodes.CsrGroupDecl)
            ),
            rules=tuple(
                item for item in body
                if isinstance(item, (ast_nodes.RuleDecl, ast_nodes.AnonymousRuleDecl))
            ),
            rule_priorities=tuple(
                item for item in body if isinstance(item, ast_nodes.RulePriority)
            ),
            fsms=tuple(item for item in body if isinstance(item, ast_nodes.FsmDecl)),
            fifos=tuple(item for item in body if isinstance(item, ast_nodes.FifoDecl)),
            memories=tuple(item for item in body if isinstance(item, ast_nodes.MemoryDecl)),
            roms=tuple(item for item in body if isinstance(item, ast_nodes.RomDecl)),
            arbiters=tuple(item for item in body if isinstance(item, ast_nodes.ArbiterDecl)),
            contracts=tuple(item for item in body if isinstance(item, ast_nodes.ContractDecl)),
            verification_goals=tuple(
                item for item in body if isinstance(item, ast_nodes.VerificationGoalDecl)
            ),
            verification_scopes=tuple(
                item for item in body if isinstance(item, ast_nodes.VerificationScopeDecl)
            ),
            parameters=parameters,
            declared_parameters=parameters,
            instances=tuple(item for item in body if isinstance(item, ast_nodes.InstanceDecl)),
            aggregate_interfaces=tuple(item for item in body if isinstance(item, ast_nodes.AggregateInterfaceDecl)),
            generic_declarations=tuple(
                item for item in body if isinstance(item, ast_nodes.GenericDeclaration)
            ),
            compile_time_ifs=tuple(
                item for item in body if isinstance(item, ast_nodes.CompileTimeIfDecl)
            ),
            generate_blocks=tuple(
                item for item in body if isinstance(item, ast_nodes.GenerateBlock)
            ),
            timing=timings[0] if timings else None,
            conforms_to=interface_ref,
            ordered_items=tuple(
                item for item in body
                if item is not parameters
                and item is not interface_ref
                and not _tagged(item, "module_where")
                and item is not None
            ),
            parameter_constraint=parameter_constraint,
            name_origin=self._token_span(items[0]),
        )

    def module_where(self, items: list[object]) -> tuple[str, object]:
        return ("module_where", items[0])


__all__ = ["AggregateCallableModuleRules"]
