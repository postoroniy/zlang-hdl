"""Parse ZLang source into a syntax-only AST."""

from __future__ import annotations

import hashlib
from functools import cache
from io import BytesIO
from importlib.resources import files
from threading import Lock

import lark
from lark import Lark, Token, Transformer, UnexpectedInput
from lark.exceptions import VisitError

from zlang.ast.nodes import Module, SpecializationArgument, TypeName
from zlang.diagnostics import attach_diagnostic_origin
from zlang.source import SourceOrigin, SourceSpan
from zlang.parser.aggregate_module_rules import AggregateCallableModuleRules
from zlang.parser.errors import ParseError
from zlang.parser.expression_rules import ExpressionRules
from zlang.parser.protocol_csr_rules import ProtocolAndCsrRules
from zlang.parser.rules_support import SourceRuleMixin
from zlang.parser.state_rules import StateRules
from zlang.parser.simulation_rules import SimulationRules
from zlang.parser.target_generic_rules import TargetAndGenericRules
from zlang.parser.type_rules import TypeRules


class _AstBuilder(
    Transformer,
    TargetAndGenericRules,
    AggregateCallableModuleRules,
    StateRules,
    SimulationRules,
    ProtocolAndCsrRules,
    TypeRules,
    ExpressionRules,
    SourceRuleMixin,
):
    """Compose stateless rule domains around one source-coordinate owner."""

    def __init__(self, source: str) -> None:
        super().__init__()
        self._initialize_source_context(source)

    @staticmethod
    def _ordinary_binding_name_is_valid(name: str) -> bool:
        return _ordinary_binding_name_is_valid(name)

    def _parse_concise_specializations(
        self, type_name: TypeName,
    ) -> tuple[SpecializationArgument, ...]:
        """Reuse explicit argument grammar with the original source coordinates."""

        origin = type_name.origin
        if origin is None:
            raise ParseError("concise callable arguments require a source range")
        start = self._source_offset(origin.start_line, origin.start_column)
        end = self._source_offset(origin.end_line, origin.end_column)
        spelling = self._source_context.source[start:end]
        opening = spelling.index("<")
        arguments = spelling[opening:]
        prefix = "module __ConciseProbe { inst __child:__Generic"
        primary = SourceOrigin(origin, "concise specialization arguments")
        try:
            tree = _get_parser().parse(prefix + arguments + " }")
        except UnexpectedInput as error:
            raise ParseError(
                "invalid callable reference in concise declaration", primary=primary,
            ) from error
        fragment = next((
            node for node in tree.find_data("specialization_arguments")
            if node.meta.start_pos == len(prefix)
            and node.meta.end_pos == len(prefix) + len(arguments)
        ), None)
        if fragment is None:
            raise ParseError("invalid concise specialization arguments", primary=primary)

        # The probe is syntax-only. Rebase its selected argument subtree before
        # invoking the same callbacks on this builder's original source context.
        delta = start + opening - len(prefix)
        for node in fragment.iter_subtrees():
            positioned = [node.meta, *(child for child in node.children if isinstance(child, Token))]
            for item in positioned:
                item.start_pos += delta
                item.end_pos += delta
                item.line, item.column = self._source_context.position(item.start_pos)
                item.end_line, item.end_column = self._source_context.position(item.end_pos)
        result = self.transform(fragment)
        if not isinstance(result, tuple) or not all(isinstance(x, SpecializationArgument) for x in result):
            raise ParseError("invalid concise specialization arguments", primary=primary)
        return result


_GRAMMAR = files("zlang.parser").joinpath("grammar.lark").read_text()
_PARSER: Lark | None = None
_PARSER_LOCK = Lock()
_PARSER_TABLE_LARK_VERSION = "1.3.1"
_PARSER_TABLE_GRAMMAR_SHA256 = "4ad1c652f6bd08e747cb1459eedbcd8d262b68d966e7a7c1355208a3f51a3e22"
_PARSER_TABLE_SHA256 = "4b5219412855fa3ad8e2afcbd47c21ee483d06e7951d435ad9ca88d6d733abb2"


def _load_packaged_parser() -> Lark | None:
    """Load only the compiler-shipped, hash-pinned table, never user cache data."""

    if (
        Lark is not lark.Lark
        or lark.__version__ != _PARSER_TABLE_LARK_VERSION
        or hashlib.sha256(_GRAMMAR.encode("utf-8")).hexdigest()
        != _PARSER_TABLE_GRAMMAR_SHA256
    ):
        return None
    try:
        payload = files("zlang.parser").joinpath("lalr-1.3.1.larkbin").read_bytes()
        if hashlib.sha256(payload).hexdigest() != _PARSER_TABLE_SHA256:
            return None
        return Lark.load(BytesIO(payload))
    except (OSError, ValueError, EOFError):
        return None


def _get_parser() -> Lark:
    """Construct the shared parser once; a failed construction can be retried."""

    global _PARSER
    with _PARSER_LOCK:
        if _PARSER is None:
            _PARSER = _load_packaged_parser()
            if _PARSER is None:
                _PARSER = Lark(
                    _GRAMMAR,
                    parser="lalr",
                    propagate_positions=True,
                )
        return _PARSER


@cache
def _ordinary_binding_name_is_valid(name: str) -> bool:
    """Apply the parser's existing immutable-binding name policy exactly."""

    probe = (
        f"fn __tuple_binding_probe(x:u8){{{name}=x {name}}} "
        "module __TupleBindingProbe{out y:u1 y=0}"
    )
    try:
        _get_parser().parse(probe)
    except UnexpectedInput:
        return False
    return True


def is_valid_identifier(name: str) -> bool:
    """Validate a rename candidate with the parser's ordinary-name rules."""

    return isinstance(name, str) and bool(name) and _ordinary_binding_name_is_valid(name)


def significant_tokens(source: str) -> tuple[tuple[str, str, int, int, int, int], ...]:
    """Parser-owned lexical tokens, excluding whitespace and comments.

    Callers must also compare successfully parsed ASTs before treating two
    sources as syntax-equivalent; lexical equality alone is not a semantic
    equivalence check in a contextual grammar.
    """

    return tuple(
        (item.type, item.value, item.line, item.column,
         item.end_line, item.end_column)
        for item in _get_parser().lex(source)
    )


def parse(source: str) -> Module:
    """Parse one ZLang module."""

    try:
        tree = _get_parser().parse(source)
    except UnexpectedInput as error:
        context = error.get_context(source).strip()
        start_line = max(1, error.line)
        start_column = max(1, error.column)
        source_lines = source.splitlines()
        selected_line = (
            source_lines[start_line - 1]
            if start_line <= len(source_lines)
            else ""
        )
        end_column = (
            start_column + 1
            if start_column <= len(selected_line)
            else start_column
        )
        raise ParseError(
            f"syntax error at line {error.line}, column {error.column}: {context}",
            primary=SourceOrigin(
                SourceSpan(
                    start_line,
                    start_column,
                    start_line,
                    end_column,
                ),
                "syntax error",
            ),
        ) from error
    try:
        result = _AstBuilder(source).transform(tree)
    except VisitError as error:
        if isinstance(error.orig_exc, ParseError):
            # Callback validation has the same lexer-owned coordinates as syntax
            # errors. Keep narrower locations explicitly supplied by the callback.
            meta = error.obj.meta
            if not meta.empty:
                attach_diagnostic_origin(error.orig_exc, SourceOrigin(
                    SourceSpan(meta.line, meta.column, meta.end_line, meta.end_column),
                    str(error.rule),
                ))
            raise error.orig_exc from error
        raise
    if not isinstance(result, Module):  # Defensive check at the parser boundary.
        raise ParseError("source did not produce a module")
    return result
