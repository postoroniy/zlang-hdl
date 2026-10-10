"""Parse ZLang source into a syntax-only AST."""

from __future__ import annotations

import hashlib
from functools import cache
from io import BytesIO
from importlib.resources import files
from threading import Lock

import lark
from lark import Lark, Transformer, UnexpectedInput
from lark.exceptions import VisitError

from zlang.ast.nodes import Module
from zlang.source import SourceOrigin, SourceSpan
from zlang.parser.aggregate_module_rules import AggregateCallableModuleRules
from zlang.parser.errors import ParseError
from zlang.parser.expression_rules import ExpressionRules
from zlang.parser.protocol_csr_rules import ProtocolAndCsrRules
from zlang.parser.rules_support import SourceRuleMixin
from zlang.parser.state_rules import StateRules
from zlang.parser.target_generic_rules import TargetAndGenericRules
from zlang.parser.type_rules import TypeRules


class _AstBuilder(
    Transformer,
    TargetAndGenericRules,
    AggregateCallableModuleRules,
    StateRules,
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


_GRAMMAR = files("zlang.parser").joinpath("grammar.lark").read_text()
_PARSER: Lark | None = None
_PARSER_LOCK = Lock()
_PARSER_TABLE_LARK_VERSION = "1.3.1"
_PARSER_TABLE_GRAMMAR_SHA256 = "f074e150ba2723c883e08b08f1c97e181cad43b9cea1da05f64b560b374eadd3"
_PARSER_TABLE_SHA256 = "faf684c891da8f1a98d42901501a65ab5581d5eb1c0b6c93155320b00d8df3e2"


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
            raise error.orig_exc from error
        raise
    if not isinstance(result, Module):  # Defensive check at the parser boundary.
        raise ParseError("source did not produce a module")
    return result
