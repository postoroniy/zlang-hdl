# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bounded stateless Lark callbacks owned by TypeRules."""

from __future__ import annotations

import re

from lark import v_args

from zlang.ast import nodes as ast_nodes
from zlang.parser.errors import ParseError


class TypeRules:
    """Stateless grammar callbacks for one bounded parser domain."""

    @v_args(meta=True)
    def builtin_type(self, meta: object, items: list[object]) -> ast_nodes.TypeName:
        return ast_nodes.TypeName(str(items[0]), origin=self._span(meta))

    @v_args(meta=True)
    def generic_fixed_type_value(self, meta: object, items: list[object]) -> ast_nodes.TypeName:
        return ast_nodes.TypeName(str(items[0]), origin=self._span(meta))

    @v_args(meta=True)
    def generic_type_value(self, meta: object, items: list[object]) -> ast_nodes.TypeName:
        origin = self._span(meta)
        return ast_nodes.TypeName(
            str(items[0]),
            origin=origin,
            named_origins=self._type_name_components(origin),
        )

    @v_args(meta=True)
    def alias_type(self, meta: object, items: list[object]) -> ast_nodes.TypeName:
        return ast_nodes.TypeName(str(items[0]), origin=self._span(meta))

    @v_args(meta=True)
    def generic_type_ref(self, meta: object, items: list[object]) -> ast_nodes.TypeName:
        origin = self._span(meta)
        if len(items) == 1:
            text = str(items[0])
            match = re.fullmatch(r"(uint|sint|bits)<([0-9]+)>", text)
            if match is not None and int(match.group(2)) < 1:
                raise ParseError("generic hardware type width must be positive")
            return ast_nodes.TypeName(
                text,
                origin=origin,
                named_origins=self._type_name_components(origin),
            )
        if str(items[0]) in {"uint", "sint", "bits"} and str(items[1]).isdigit() and int(str(items[1])) < 1:
            raise ParseError("generic hardware type width must be positive")
        return ast_nodes.TypeName(
            f"{items[0]}<{','.join(str(item) for item in items[1:])}>",
            origin=origin,
            named_origins=self._type_name_components(origin),
        )

    @v_args(meta=True)
    def specialization_type_ref(self, meta: object, items: list[object]) -> ast_nodes.TypeName:
        return ast_nodes.TypeName(str(items[0]), origin=self._span(meta))

    def type_ref(self, items: list[object]) -> str:
        return f"{items[0]}<{','.join(str(item) for item in items[1:])}>"

    def qualified_type_ref(self, items: list[object]) -> str:
        return f"{items[0]}<{','.join(str(item) for item in items[1:])}>"

    def type_argument(self, items: list[object]) -> int | str:
        return items[0]

    def type_value_argument(self, items: list[object]) -> str:
        return str(items[0])

    def width_expression(self, items: list[object]) -> str:
        return "".join(str(item) for item in items)

    def intrinsic_width(self, items: list[object]) -> str:
        return f"{items[0]}({items[1]})"

    def intrinsic_param_value(self, items: list[object]) -> str:
        return f"{items[0]}({items[1]})"

    def parenthesized_width(self, items: list[object]) -> str:
        return f"({items[0]})"

    def signed_width(self, items: list[object]) -> str:
        return f"{items[0]}{items[1]}"

    @staticmethod
    def _storage_depth_value(value: object) -> int | str:
        text = str(value)
        try:
            return int(text, 0)
        except ValueError:
            return text

    def storage_depth_atom(self, items: list[object]) -> int | str:
        return self._storage_depth_value(items[0])

    def storage_depth_signed(self, items: list[object]) -> str:
        return f"{items[0]}{items[1]}"

    def storage_depth_parenthesized(self, items: list[object]) -> str:
        return f"({items[0]})"

    def storage_depth_expression(self, items: list[object]) -> str:
        return str(items[0])

    @v_args(meta=True)
    def vector_type(self, meta: object, items: list[object]) -> ast_nodes.VectorTypeName:
        length = str(items[0])
        return ast_nodes.VectorTypeName(
            int(length) if length.isdigit() else length,
            items[1],
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def string_type(self, meta: object, items: list[object]) -> ast_nodes.VectorTypeName:
        length = str(items[0])
        return ast_nodes.VectorTypeName(
            int(length) if length.isdigit() else length,
            ast_nodes.TypeName("char"),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def tuple_type(self, meta: object, items: list[object]) -> ast_nodes.TupleTypeName:
        if not 2 <= len(items) <= 8:
            raise ParseError("a tuple type requires between 2 and 8 components")
        return ast_nodes.TupleTypeName(tuple(items), origin=self._span(meta))

    def tuple_type_passthrough(self, items: list[object]) -> ast_nodes.TupleTypeName:
        return items[0]

    @v_args(meta=True)
    def tuple_literal_expr(
        self, meta: object, items: list[object]
    ) -> ast_nodes.TupleLiteralExpr:
        if not 2 <= len(items) <= 8:
            raise ParseError("a tuple literal requires between 2 and 8 elements")
        return ast_nodes.TupleLiteralExpr(tuple(items), origin=self._span(meta))

    @v_args(meta=True)
    def assignment(self, meta: object, items: list[object]) -> ast_nodes.Assignment:
        target = str(items[0])
        return ast_nodes.Assignment(
            target,
            items[1],
            name_origin=self._name_span_from_meta(meta, target.split(".", 1)[0]),
        )

    @v_args(meta=True)
    def typed_assignment(self, meta: object, items: list[object]) -> ast_nodes.Assignment:
        target = str(items[0])
        return ast_nodes.Assignment(
            target,
            items[2],
            items[1],
            name_origin=self._name_span_from_meta(meta, target.split(".", 1)[0]),
        )

    def assignment_target(self, items: list[object]) -> str:
        return ".".join(str(item) for item in items)


__all__ = ["TypeRules"]
